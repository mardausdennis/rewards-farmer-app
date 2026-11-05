"""Sign-in owns its lock and browsers until the session is saved."""

import json
import io
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from file_replace import read_file, replace_file
from rfapp import paths, settings
from rfapp.envfile import EnvFile


_logger = logging.getLogger("rfapp")


def _status_path(account_id: str) -> Path:
	return paths.runs_dir() / f"signin-{account_id.replace('@', '')}.json"


def _cancel_path(account_id: str) -> Path:
	return paths.runs_dir() / f"signin-{account_id.replace('@', '')}.cancel"


def _write_status(account_id: str, state: str, error_code: str | None = None, detail: str | None = None, pid: int | None = None, invitation=None) -> dict:
	message = f"errors.browser.{error_code}" if state == "failed" and error_code not in (None, "crashed") else f"accounts.signin.{state}"
	if error_code == "terms_of_use":
		message = "errors.state.terms_of_use"
	value = {"account_id": account_id, "state": state, "error_code": error_code, "message_key": message, "detail": detail, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "pid": os.getpid() if pid is None else pid}
	if invitation is not None:
		value["invitation"] = {key: invitation.get(key) for key in ("operation_id", "phase", "source_account_id", "target_account_id", "continue_token", "referral_status", "can_resume", "can_discard")}
		if state in ("invite_waiting", "invite_interrupted"):
			value["message_key"] = "accounts.invite." + ("waiting" if state == "invite_waiting" else "interrupted")
		if isinstance(error_code, str) and error_code.startswith("accounts.invite_"):
			value["message_key"] = "errors.app." + error_code.replace(".", "_")
	path = _status_path(account_id)
	name = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".signin-", delete=False) as stream:
			name = stream.name
			json.dump(value, stream, ensure_ascii=False)
			stream.flush()
			os.fsync(stream.fileno())
		replace_file(name, path)
	finally:
		if name is not None:
			Path(name).unlink(missing_ok=True)
	return value


def _quit(driver) -> None:
	if driver is not None:
		try:
			driver.quit()
		except Exception:
			_logger.warning("Could not close the sign-in browser")


def _run_invitation(account_id, op_id):
	"""Only explicit continuation permits dashboard/identity reads in this branch."""
	import accounts
	import browser
	import identity
	import referrals
	import run_lock
	from selenium import webdriver
	from rfapp import accounts_admin, invite_onboarding as invite
	lock = run_lock.acquire("signin", account=account_id, directory=str(settings.user_data_dir()))
	if lock is None:
		invite.interrupt(op_id, "invite_busy")
		_write_status(account_id, "busy", invitation=invite.read(op_id))
		return 3
	visible = None
	state, code = "invite_interrupted", None
	try:
		owned = invite.child_begin(op_id, account_id)
		account = accounts.Account(account_id, str(owned["profile"]), "Default")
		with referrals._private_transport():
			# Do not inherit the ordinary branch's opt-in verbose driver log: it
			# records navigation commands containing this private invitation URL.
			service = webdriver.EdgeService(executable_path=os.environ.get("MSEDGEDRIVER_PATH") or None, service_args=["--silent"], log_output=os.devnull)
			visible = webdriver.Edge(options=browser.build_options(account), service=service)
			visible.set_page_load_timeout(60)
			# This is the first navigation. No dashboard, balance or identity probe
			# can run until the matching per-operation continue command is consumed.
			visible.get(owned["link"])
			status = invite.child_phase(op_id, "invite_open")
			_write_status(account_id, "invite_waiting", invitation=status)
			deadline = time.monotonic() + 900
			while time.monotonic() < deadline:
				if _cancel_path(account_id).exists():
					invite.interrupt(op_id, cancelled=True)
					state = "cancelled"
					break
				operation = invite.read(op_id)
				if operation["phase"] not in ("invite_open", "continue_requested"):
					break
				try:
					if not visible.window_handles: break
				except Exception:
					break
				if operation["phase"] == "continue_requested":
					invite.child_phase(op_id, "verifying")
					_write_status(account_id, "verifying", invitation=invite.read(op_id))
					visible.get("https://rewards.bing.com/")
					candidate = accounts_admin._profile_identity(account_id).get("name")
					matched, _ = identity.read_dashboard_identity(visible, candidate)
					if matched != "match":
						code = "identity_unknown" if matched == "unknown" else "accounts.identity_mismatch"
						break
					invite.stage_confirmation(op_id, candidate)
					state = "awaiting_confirmation"
					break
				time.sleep(.25)
	except Exception as exc:
		# Do not emit exception messages: Selenium may include the invitation URL.
		code = exc.code if getattr(exc, "code", "").startswith("accounts.invite_") else "accounts.invite_unknown"
	finally:
		with referrals._private_transport(): _quit(visible)
		try:
			if state not in ("awaiting_confirmation", "cancelled"):
				invite.interrupt(op_id)
			_write_status(account_id, state, code, invitation=invite.read(op_id))
			_cancel_path(account_id).unlink(missing_ok=True)
		finally: lock.release()
	return 0 if state == "awaiting_confirmation" else 1


def run(account_id, new, enroll=False, invitation_id=None) -> int:
	import dotenv
	try:
		text = read_file(paths.env_path())
	except FileNotFoundError:
		text = ""
	dotenv.load_dotenv(stream=io.StringIO(text))
	os.environ["REWARDS_HEADLESS"] = "0"
	os.environ["USE_VIRTUAL_DESKTOP"] = "0"
	# browser fixes HEADLESS at import, so it belongs after these overrides.
	import accounts
	import browser
	import element_selectors
	import rewards_state
	import run_lock
	from selenium import webdriver
	from selenium.common.exceptions import WebDriverException
	from rfapp import accounts_admin, schedule
	import identity

	if not isinstance(account_id, str) or new and account_id == "@root" or account_id != "@root" and not accounts.directory_name_is_valid(account_id):
		raise ValueError("Invalid profile id")
	if invitation_id is not None:
		from rfapp import invite_onboarding
		invite_onboarding.operation_id(invitation_id)
		if not new or not enroll: return 2
		return _run_invitation(account_id, invitation_id)
	from rfapp import invite_onboarding
	if account_id not in [*settings.active_accounts(), *settings.paused_accounts()] and (paths.data_home() / "invite-onboarding.json").exists() and (operation := invite_onboarding.for_target(account_id)) is not None:
		_write_status(account_id, "invite_interrupted", invitation=operation)
		return 2
	if enroll and not new and account_id not in [*settings.active_accounts(), *settings.paused_accounts()]:
		_write_status(account_id, "failed", "enrollment_required")
		return 1
	if enroll and new and not re.fullmatch(r"account-[1-9][0-9]*", account_id):
		_write_status(account_id, "failed", "enrollment_required")
		return 1
	if new and settings.active_accounts() and not enroll:
		_write_status(account_id, "failed", "enrollment_required")
		return 1
	if new and enroll and not (paths.data_home() / "account-migration.json").exists() and any(identity.binding(account, paths.data_home()) is None for account in [*settings.active_accounts(), *settings.paused_accounts()]):
		_write_status(account_id, "failed", "enrollment_required")
		return 1
	root = settings.user_data_dir()
	if account_id != "@root":
		profile = root / account_id
		if not profile.resolve().is_relative_to(root.resolve()) or profile.resolve() == root.resolve():
			raise ValueError("Profile resolves outside the data directory")
	else:
		profile = root
	lock = run_lock.acquire("signin", account=account_id, directory=str(root))
	if lock is None:
		_write_status(account_id, "busy")
		return 3
	visible = None
	verification = None
	state = "failed"
	session_saved = False
	error_code = None
	detail = None
	observed_name = None
	try:
		_write_status(account_id, "starting")
		if new and ((root / "Local State").is_file() and "@root" in settings.active_accounts() or (paths.data_home() / "account-migration.json").exists()):
			accounts_admin.migrate_root_profile(account_id)
		account = accounts.get_default_account() if account_id == "@root" else accounts.Account(account_id, str(profile), "Default")
		try:
			visible = webdriver.Edge(options=browser.build_options(account), service=browser.build_service())
			visible.set_page_load_timeout(60)
			visible.get("https://rewards.bing.com/")
		except WebDriverException as exc:
			error_code, detail = browser.start_failure_code(exc), type(exc).__name__
		else:
			_write_status(account_id, "waiting")
			deadline = time.monotonic() + 15 * 60
			closed = False
			verify = True
			while time.monotonic() < deadline:
				if _cancel_path(account_id).exists():
					state, verify = "cancelled", False
					break
				try:
					url = visible.current_url
				except Exception:
					closed = True
					break
				if urlsplit(url).hostname == "rewards.bing.com":
					balance = rewards_state.read_balance(visible, element_selectors.ElementSelectionUtils(visible))
					if balance is not None:
						if enroll:
							candidate = accounts_admin._profile_identity(account_id).get("name")
							matched, _ = identity.read_dashboard_identity(visible, candidate)
							observed_name = candidate if matched == "match" else None
						time.sleep(3)
						state, verify = "signed_in", False
						break
				time.sleep(2)
			_quit(visible)
			visible = None
			if verify:
				_write_status(account_id, "verifying")
				try:
					options = browser.build_options(account)
					options.add_argument("--headless=new")
					verification = webdriver.Edge(options=options, service=browser.build_service())
					verification.set_page_load_timeout(60)
					snapshot = rewards_state.read_state(verification, element_selectors.ElementSelectionUtils(verification))
					if snapshot.unreadable == "signed_out":
						state = "cancelled" if closed else "timed_out"
					else:
						state = "signed_in"
						error_code = "terms_of_use" if snapshot.unreadable == "terms_of_use" else None
						if enroll:
							candidate = accounts_admin._profile_identity(account_id).get("name")
							matched, _ = identity.read_dashboard_identity(verification, candidate)
							observed_name = candidate if matched == "match" else None
				except WebDriverException as exc:
					state, error_code, detail = "failed", browser.start_failure_code(exc), type(exc).__name__
				finally:
					_quit(verification)
					verification = None
		if enroll and state == "signed_in":
			session_saved = True
			if observed_name is None:
				state, error_code = "failed", "identity_unknown"
			else:
				identity.stage_enrollment(account_id, observed_name, new=new, directory=paths.data_home())
				state = "awaiting_confirmation"
		elif new and state == "signed_in":
			session_saved = True
			editor = EnvFile.load(paths.env_path())
			active = [item for item in settings.active_accounts() if item != "@root"]
			if account_id.casefold() not in [item.casefold() for item in active]:
				active.append(account_id)
			editor.set("REWARDS_ACCOUNTS", ",".join(active))
			editor.save()
			schedule.ensure()
	except Exception as exc:
		_logger.error("Sign-in failed for profile %s (%s)", account_id, type(exc).__name__)
		state = "failed"
		error_code = getattr(exc, "code", "unknown")
		detail = type(exc).__name__
	finally:
		_quit(visible)
		_quit(verification)
		try:
			if new and not session_saved and state in ("failed", "cancelled", "timed_out") and profile.exists():
				if not accounts_admin._recycle([profile]):
					_logger.warning("Sign-in profile could not be recycled: %s", profile)
					detail = (detail + "; " if detail else "") + f"Profile data could not be recycled: {profile}"
			_write_status(account_id, state, error_code, detail)
			_cancel_path(account_id).unlink(missing_ok=True)
		finally:
			lock.release()
	return 0 if state in ("signed_in", "awaiting_confirmation") else 1
