"""Confirmed dashboard-name binding, not proof of a unique Microsoft identity.

Two people with the same normalized display name cannot be distinguished.
Bing search crediting is not independently observed by this dashboard check.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlsplit

from file_replace import read_file, replace_file


TARGET = "https://rewards.bing.com/"
SAME_NAME_LIMIT = True


class IdentityError(ValueError):
	def __init__(self, code="accounts.identity_unreadable"):
		self.code = code
		super().__init__(code)


def normalized(name):
	return " ".join(name.casefold().split()) if isinstance(name, str) else ""


def name_hash(name):
	value = normalized(name)
	return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


@contextmanager
def _private_transport():
	loggers = [logging.getLogger(key) for key in ("selenium.webdriver.remote.remote_connection", "urllib3.connectionpool")]
	previous = [logger.disabled for logger in loggers]
	try:
		for logger in loggers: logger.disabled = True
		yield
	finally:
		for logger, disabled in zip(loggers, previous): logger.disabled = disabled


def _dashboard(url):
	try:
		p = urlsplit(url)
		return p.scheme == "https" and p.hostname == "rewards.bing.com" and p.path in ("/dashboard", "/dashboard/") and not p.username and not p.password and p.port in (None, 443)
	except (ValueError, TypeError):
		return False


def _dashboard_greeting(driver):
	with _private_transport():
		if not _dashboard(driver.current_url):
			driver.get(TARGET)
		if not _dashboard(driver.current_url):
			return None
		visible = [node for node in driver.find_elements("css selector", "p.pii") if node.is_displayed()]
		if len(visible) != 1:
			return None
		text = visible[0].text
		if not isinstance(text, str) or not text.strip() or len(text) > 512 or not _dashboard(driver.current_url):
			return None
		return " ".join(text.split())


def _matching_span(text, digests):
	text = normalized(text)
	for start in range(len(text)):
		for end in range(start + 1, len(text) + 1):
			candidate = text[start:end]
			if candidate == candidate.strip() and name_hash(candidate) in digests:
				return candidate
	return None


def _contains_hash(text, digests):
	return _matching_span(text, digests) is not None


def read_dashboard_identity(driver, expected_name=None, *, expected_name_hash=None):
	"""Return (match|mismatch|unknown, matched-name hash or None), never raw PII.

	A hash-only binding is matched against contiguous normalized greeting spans.
	Mismatch cannot identify a different name without guessing greeting grammar.
	"""
	expected = normalized(expected_name)
	digest = name_hash(expected) if expected else expected_name_hash
	if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
		return "unknown", None
	try:
		text = normalized(_dashboard_greeting(driver))
		if not text or len(text) > 512:
			return "unknown", None
		if expected:
			return ("match", digest) if expected in text else ("mismatch", None)
		return ("match", digest) if _contains_hash(text, {digest}) else ("mismatch", None)
	except Exception:
		return "unknown", None


def require_new_identity(account_id, observation, directory=None):
	value = load(directory)
	digests = {binding(other, directory)["expected_name_hash"] for other in value["bindings"] if other != account_id}
	if digests and _contains_hash(observation, digests):
		raise IdentityError("accounts.account_already_added")


def observe_enrollment(driver, expected_name=None, *, account_id, directory=None):
	"""Transient raw observation for the existing human confirmation only.

	Runtime read_dashboard_identity stays hash-only and never discovers a name.
	Without matching metadata, show the whole bounded p.pii value for confirmation;
	guessing a localized greeting's grammar could silently bind the wrong substring.
	"""
	try:
		text = _dashboard_greeting(driver)
	except Exception:
		return "unknown", None
	if text is None:
		return "unknown", None
	try:
		require_new_identity(account_id, text, directory)
	except IdentityError as exc:
		if exc.code == "accounts.account_already_added":
			return "already_added", None
		raise
	candidate = " ".join(expected_name.split()) if isinstance(expected_name, str) else ""
	old = binding(account_id, directory)
	if old is not None:
		# Reconfirm an existing hash without silently changing it to the whole
		# greeting when Edge has lost its optional profile-name metadata.
		matched = _matching_span(text, {old["expected_name_hash"]})
		if matched is not None and (not candidate or name_hash(candidate) != old["expected_name_hash"]):
			candidate = matched
	observation = candidate if candidate and normalized(candidate) in normalized(text) else text
	return ("observed", observation) if len(observation) <= 256 else ("unknown", None)


def home(directory=None):
	return Path(directory if directory is not None else os.environ.get("REWARDS_FARMER_DATA_HOME") or Path(__file__).resolve().parent.parent)


def utc():
	return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_document(path, value):
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	name = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".identity-", delete=False) as stream:
			name = stream.name
			json.dump(value, stream, ensure_ascii=False)
			stream.write("\n")
			stream.flush()
			os.fsync(stream.fileno())
		replace_file(name, path)
	finally:
		if name is not None: Path(name).unlink(missing_ok=True)


def load(directory=None):
	try:
		value = json.loads(read_file(home(directory) / "account-identities.json"))
	except FileNotFoundError:
		return {"v": 1, "mode": "legacy", "bindings": {}, "pending": {}, "used_ids": []}
	except (ValueError, OSError):
		raise IdentityError() from None
	if not isinstance(value, dict) or value.get("v") != 1 or value.get("mode") not in ("legacy", "managed") or not isinstance(value.get("bindings"), dict) or not isinstance(value.get("pending"), dict) or not isinstance(value.get("used_ids"), list):
		raise IdentityError()
	return value


def save(value, directory=None):
	write_document(home(directory) / "account-identities.json", value)


def binding(account_id, directory=None):
	value = load(directory)["bindings"].get(account_id)
	if value is None: return None
	try:
		valid = value["account_id"] == account_id and re.fullmatch(r"[0-9a-f]{64}", value["expected_name_hash"]) and datetime.fromisoformat(value["confirmed_at"].replace("Z", "+00:00")).tzinfo is not None
	except (KeyError, ValueError, TypeError, AttributeError):
		valid = False
	if not valid: raise IdentityError()
	source = value.get("invited_by")
	if source is not None and (not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", source) or source.casefold() == account_id.casefold()):
		raise IdentityError()
	return dict(value)


def managed(directory=None):
	try:
		if load(directory)["mode"] == "managed": return True
	except IdentityError:
		return True
	try:
		return json.loads(read_file(home(directory) / "app-prefs.json")).get("identity_managed") is True
	except FileNotFoundError:
		return False
	except (ValueError, OSError, AttributeError):
		# A broken preferences file alone does not enroll a legacy installation.
		return False


def check_account(driver, account_id, directory=None):
	try:
		value = binding(account_id, directory)
	except IdentityError:
		return "unknown", "binding_unreadable"
	if value is None: return "unknown", "binding_missing"
	status, _ = read_dashboard_identity(driver, expected_name_hash=value["expected_name_hash"])
	return status, "confirmed_name" if status == "match" else "name_mismatch" if status == "mismatch" else "dashboard_unknown"


def stage_enrollment(account_id, observed_name, *, new, directory=None):
	value = load(directory)
	digest = name_hash(observed_name)
	if digest is None or len(observed_name) > 256: raise IdentityError()
	old = binding(account_id, directory)
	if old is not None and old["expected_name_hash"] != digest:
		raise IdentityError("accounts.identity_mismatch")
	require_new_identity(account_id, observed_name, directory)
	value["pending"][account_id] = {"account_id": account_id, "observed_name": observed_name, "observed_hash": digest, "observed_at": utc(), "token": secrets.token_hex(16), "new": bool(new)}
	if account_id not in value["used_ids"]: value["used_ids"].append(account_id)
	save(value, directory)
	return value["pending"][account_id]


def pending(account_id, directory=None):
	value = load(directory)["pending"].get(account_id)
	if value is None: return None
	try:
		age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))).total_seconds()
		if not 0 <= age <= 900 or value["account_id"] != account_id or name_hash(value["observed_name"]) != value["observed_hash"] or not re.fullmatch(r"[0-9a-f]{32}", value["token"]):
			raise IdentityError("accounts.enrollment_expired")
	except (KeyError, ValueError, TypeError, AttributeError):
		raise IdentityError("accounts.enrollment_expired") from None
	return dict(value)
