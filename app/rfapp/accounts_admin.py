"""Profile administration without importing Selenium into the window app."""

import ctypes
import base64
import hashlib
import io
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import warnings
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from file_replace import read_file, replace_file
from rfapp import paths, prefs, settings, signin
from rfapp.envfile import EnvFile


_logger = logging.getLogger("rfapp")
_ACTIVE_STATES = ("starting", "waiting", "verifying")
_LOCK_NAMES = (".rewards-farmer.lock", ".rewards-farmer.lock.json")
_LEGACY_KEYS = ("REWARDS_ACCOUNTS", "QUERY_SOURCE", "LLM_PROVIDER", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_BASE_URL", "LOCAL_LLM_BASE_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LLM_REQUEST_TIMEOUT_SECONDS", "REWARDS_HEADLESS", "USE_VIRTUAL_DESKTOP", "SWITCH_BACK_TO_MAIN_DESKTOP", "SWITCH_BACK_DELAY_SECONDS", "CLEANUP_VIRTUAL_DESKTOP", "EDGE_BINARY", "MSEDGEDRIVER_PATH", "REWARDS_FARMER_LOG_LEVEL")
_LOCAL_STATE_LIMIT = 4 * 1024 * 1024
_identity_cache = {}
_identity_lock = threading.RLock()
_PICTURE_LIMIT = 2 * 1024 * 1024
_PICTURE_PIXELS = 16 * 1024 * 1024
_picture_cache = {}
_picture_lock = threading.RLock()
_picture_warnings = set()


def _picture_unreadable(path: Path, stamp=None) -> None:
	if stamp is None:
		try:
			stat = path.stat()
			stamp = (stat.st_mtime_ns, stat.st_size)
		except OSError:
			stamp = (None, None)
	key = (os.path.normcase(str(path.absolute())), stamp)
	with _picture_lock:
		if key in _picture_warnings:
			return
		_picture_warnings.add(key)
		# Repeated account listings must not flood logs or expose profile data.
		_logger.warning("account_picture_unreadable")


def _open_shared(path: Path):
	if os.name != "nt":
		return path.open("rb")
	import msvcrt
	kernel = ctypes.WinDLL("kernel32", use_last_error=True)
	kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
	kernel.CreateFileW.restype = wintypes.HANDLE
	kernel.CloseHandle.argtypes = [wintypes.HANDLE]
	# Edge replaces Local State while running; readers must share delete as
	# well as read/write so displaying identity never blocks that replacement.
	handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
	if handle == ctypes.c_void_p(-1).value:
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
	except BaseException:
		kernel.CloseHandle(handle)
		raise
	try:
		return os.fdopen(fd, "rb")
	except BaseException:
		os.close(fd)
		raise


def _profile_metadata(account_id: str) -> dict:
	import accounts
	path = _profile_path(account_id) / "Local State"
	key = os.path.normcase(str(path.absolute()))
	unknown = {"email": None, "name": None, "picture_name": None, "picture_enabled": False, "picture_readable": False}
	with _identity_lock:
		stamp = None
		try:
			stat = path.stat()
			stamp = (stat.st_mtime_ns, stat.st_size)
			if path.is_symlink() or not 0 < stat.st_size <= _LOCAL_STATE_LIMIT:
				_identity_cache.pop(key, None)
				_picture_unreadable(path, stamp)
				return unknown
			cached = _identity_cache.get(key)
			if cached is not None and cached[:2] == stamp:
				return dict(cached[2])
			with _open_shared(path) as stream:
				raw = stream.read(_LOCAL_STATE_LIMIT + 1)
			if len(raw) > _LOCAL_STATE_LIMIT:
				_identity_cache.pop(key, None)
				_picture_unreadable(path, stamp)
				return unknown
			try:
				profile = json.loads(raw).get("profile", {})
				info_cache = profile.get("info_cache", {})
				if not isinstance(info_cache, dict):
					raise ValueError("Invalid profile metadata")
				if accounts.PROFILE_NAME not in info_cache:
					identity = unknown
				else:
					info = info_cache[accounts.PROFILE_NAME]
					email, name = info.get("user_name"), info.get("gaia_name")
					identity = {"email": email if isinstance(email, str) and "@" in email else None, "name": name if isinstance(name, str) and name else None, "picture_name": info.get("gaia_picture_file_name"), "picture_enabled": info.get("use_gaia_picture") is True and info.get("is_using_default_avatar") is not True, "picture_readable": True}
			except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
				_picture_unreadable(path, stamp)
				identity = unknown
			if len(_identity_cache) >= 128 and key not in _identity_cache:
				_identity_cache.pop(next(iter(_identity_cache)))
			_identity_cache[key] = (*stamp, dict(identity))
			return identity
		except FileNotFoundError:
			# Edge has not created this profile's Local State before sign-in.
			_identity_cache.pop(key, None)
			return unknown
		except OSError:
			# Identity is optional and must never leak through an exception log.
			_identity_cache.pop(key, None)
			if stamp is not None:
				_picture_unreadable(path, stamp)
			return unknown


def _profile_identity(account_id: str) -> dict:
	metadata = _profile_metadata(account_id)
	return {key: metadata[key] for key in ("email", "name")}


def _plain_picture_name(name) -> bool:
	return isinstance(name, str) and bool(name) and name != "." and ".." not in name and not any(char in name for char in '/\\:\x00') and not any(ord(char) < 32 for char in name)


def _picture_source(account_id: str):
	import accounts
	metadata = _profile_metadata(account_id)
	if not metadata["picture_readable"]:
		return None
	if not metadata["picture_enabled"]:
		return None
	path = _profile_path(account_id) / "Local State"
	stamp = None
	try:
		name = metadata["picture_name"]
		if not _plain_picture_name(name):
			raise ValueError("Invalid picture name")
		root = _profile_path(account_id)
		candidate = root / accounts.PROFILE_NAME / name
		if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
			raise ValueError("Picture escapes profile")
		path = candidate
		stat = path.stat()
		if not path.is_file() or not 0 < stat.st_size <= _PICTURE_LIMIT:
			raise ValueError("Invalid picture size")
		stamp = (stat.st_mtime_ns, stat.st_size)
		rev = hashlib.sha256(f"{stamp[0]}:{stamp[1]}".encode("ascii")).hexdigest()[:16]
		return path, stamp, rev
	except (OSError, ValueError):
		# Never include an exception, path or profile metadata in this record.
		_picture_unreadable(path, stamp)
		return None


def _picture_signature(raw: bytes) -> bool:
	return raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"


def _decode_picture(raw: bytes) -> str:
	from PIL import Image, ImageOps
	with warnings.catch_warnings():
		warnings.simplefilter("error", Image.DecompressionBombWarning)
		with Image.open(io.BytesIO(raw)) as header:
			width, height = header.size
			if width <= 0 or height <= 0 or width * height > _PICTURE_PIXELS:
				raise ValueError("Picture exceeds pixel limit")
			header.verify()
		with Image.open(io.BytesIO(raw)) as original:
			oriented = ImageOps.exif_transpose(original)
			width, height = oriented.size
			side = min(width, height)
			left, top = (width - side) // 2, (height - side) // 2
			picture = oriented.crop((left, top, left + side, top + side)).convert("RGBA")
			picture.thumbnail((128, 128), Image.Resampling.LANCZOS)
			# Re-encode only pixels; source EXIF and text never leave the reader.
			picture.info.clear()
			output = io.BytesIO()
			picture.save(output, format="PNG")
	return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def get_account_picture(account_id: str) -> dict:
	_known_account(account_id)
	empty = {"account_id": account_id, "rev": None, "data_url": None}
	source = _picture_source(account_id)
	if source is None:
		return empty
	path, stamp, rev = source
	key = os.path.normcase(str(path.absolute()))
	with _picture_lock:
		cached = _picture_cache.get(key)
		if cached is not None and cached[:2] == stamp:
			return {**cached[2], "account_id": account_id}
		try:
			with _open_shared(path) as stream:
				state = os.fstat(stream.fileno())
				stamp = (state.st_mtime_ns, state.st_size)
				if not 0 < state.st_size <= _PICTURE_LIMIT:
					raise ValueError("Invalid picture size")
				raw = stream.read(_PICTURE_LIMIT + 1)
			if len(raw) > _PICTURE_LIMIT or not _picture_signature(raw):
				raise ValueError("Unsupported picture format")
			rev = hashlib.sha256(f"{stamp[0]}:{stamp[1]}".encode("ascii")).hexdigest()[:16]
			result = {"account_id": account_id, "rev": rev, "data_url": _decode_picture(raw)}
		except Exception:
			_picture_unreadable(path, stamp)
			# A temporary sharing failure must not hide an unchanged valid
			# picture for the rest of this process's lifetime.
			return empty
		if len(_picture_cache) >= 128 and key not in _picture_cache:
			_picture_cache.pop(next(iter(_picture_cache)))
		_picture_cache[key] = (*stamp, dict(result))
		return result


class _Error(ValueError):
	def __init__(self, code: str, *, fields=None, params=None, detail=None):
		super().__init__(detail or code)
		self.code, self.fields, self.params, self.detail = code, fields, params or {}, detail


def _error(code, detail=None, fields=None, params=None):
	return _Error(code, detail=detail, fields=fields, params=params)


def _profile_path(account_id: str) -> Path:
	root = settings.user_data_dir()
	if account_id == "@root":
		return root
	import accounts
	if not isinstance(account_id, str) or not accounts.directory_name_is_valid(account_id):
		raise _error("accounts.unknown")
	profile = root / account_id
	if not profile.resolve().is_relative_to(root.resolve()) or profile.resolve() == root.resolve():
		raise _error("accounts.unknown")
	return profile


def _free_ids(count=1, exclude=()) -> list[str]:
	root = settings.user_data_dir()
	used = {entry.casefold() for entry in [*settings.active_accounts(), *settings.paused_accounts(), *exclude]}
	import identity
	used.update(str(entry).casefold() for entry in identity.load(paths.data_home())["used_ids"])
	used.update(str(alias.get("to", "")).casefold() for alias in prefs.load().get("history_aliases", []) if isinstance(alias, dict))
	if root.is_dir():
		used.update(entry.name.casefold() for entry in root.iterdir())
	result = []
	number = 1
	while len(result) < count:
		candidate = f"account-{number}"
		if candidate.casefold() not in used:
			result.append(candidate)
		number += 1
	return result


def _root_entries(exclude=()) -> list[Path]:
	root = settings.user_data_dir()
	return [entry for entry in root.iterdir() if entry.name not in (*_LOCK_NAMES, *exclude) and not (entry.is_dir() and (entry / "Local State").is_file())] if root.is_dir() else []


def _recycle(entries: list[Path]) -> bool:
	if not entries:
		return True
	class FileOperation(ctypes.Structure):
		_fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
	try:
		buffer = ctypes.create_unicode_buffer("\x00".join(str(path.absolute()) for path in entries) + "\x00\x00")
		operation = FileOperation(None, 3, ctypes.cast(buffer, wintypes.LPCWSTR), None, 0x40 | 0x10 | 0x04 | 0x400, False, None, None)
		function = ctypes.windll.shell32.SHFileOperationW
		function.argtypes, function.restype = [ctypes.POINTER(FileOperation)], ctypes.c_int
		return function(ctypes.byref(operation)) == 0 and not operation.fAnyOperationsAborted
	except OSError:
		_logger.exception("Could not recycle profile data")
		return False


def _pid_alive(pid: int) -> bool:
	if not isinstance(pid, int) or pid <= 0:
		return False
	if os.name != "nt":
		try:
			os.kill(pid, 0)
			return True
		except ProcessLookupError:
			return False
		except PermissionError:
			return True
	kernel = ctypes.WinDLL("kernel32", use_last_error=True)
	kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
	kernel.GetExitCodeProcess.argtypes, kernel.GetExitCodeProcess.restype = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL
	kernel.CloseHandle.argtypes = [wintypes.HANDLE]
	handle = kernel.OpenProcess(0x1000, False, pid)
	if not handle:
		return ctypes.get_last_error() == 5
	try:
		code = wintypes.DWORD()
		return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
	finally:
		kernel.CloseHandle(handle)


def _ensure_idle() -> None:
	import run_lock
	if _signin_active() is not None:
		raise _error("accounts.signin_active")
	if run_lock.status(directory=str(settings.user_data_dir())) is not None:
		raise _error("run.busy")


def _signin_status(account_id: str) -> dict:
	_profile_path(account_id)
	path = signin._status_path(account_id)
	try:
		value = json.loads(read_file(path))
	except (OSError, ValueError):
		raise _error("accounts.unknown")
	if value.get("account_id") != account_id:
		raise _error("accounts.unknown")
	if value.get("state") == "awaiting_confirmation":
		import identity
		try:
			if identity.pending(account_id, paths.data_home()) is None: value.update(state="signed_in")
		except identity.IdentityError:
			value.update(state="failed", error_code="enrollment_expired", message_key="accounts.signin.failed")
	if value.get("state") in _ACTIVE_STATES:
		try:
			age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["at"].replace("Z", "+00:00"))).total_seconds()
		except (KeyError, ValueError, TypeError):
			age = 31
		if not (value["state"] == "starting" and age < 30) and not _pid_alive(value.get("pid")):
			value.update(state="failed", error_code="crashed", message_key="accounts.signin.failed", detail="The sign-in process exited")
	return {key: value.get(key) for key in ("account_id", "state", "error_code", "message_key", "detail", "at")}


def _signin_active() -> dict | None:
	for path in paths.runs_dir().glob("signin-*.json"):
		try:
			account_id = json.loads(read_file(path))["account_id"]
			status = _signin_status(account_id)
			if status["state"] in _ACTIVE_STATES:
				return status
		except (ValueError, OSError, KeyError, TypeError):
			_logger.warning("Unreadable sign-in status file %s", path.name)
	return None


def _list_accounts() -> list[dict]:
	from rfapp import i18n
	from run_record import list_run_ids, read_events
	active, paused = settings.active_accounts(), settings.paused_accounts()
	labels = prefs.load()["account_labels"]
	observed = {}
	# Listing must not create state before the unsupported-add check refuses.
	runs = paths.data_home() / "runs"
	for run_id in list_run_ids(directory=str(runs)):
		events = read_events(str(runs / (run_id + ".jsonl")))
		end = next((event for event in reversed(events) if event.get("type") == "run_end"), {})
		ids = {}
		for event in events:
			if event.get("type") == "account_start" and isinstance(event.get("account"), str):
				profile = event.get("profile")
				ids[event["account"]] = "@root" if profile and os.path.normcase(os.path.abspath(profile)) == os.path.normcase(os.path.abspath(settings.user_data_dir())) else Path(profile).name if profile else event["account"]
		for event in reversed(events):
			name = event.get("account")
			if not isinstance(name, str):
				continue
			account_id = ids.get(name, "@root" if name == "default" else name)
			row = observed.setdefault(account_id, {})
			if event.get("type") == "account_start" and "last_run_at" not in row:
				row["last_run_at"], row["last_result"] = event.get("ts"), end.get("result")
			if event.get("type") == "state":
				if "signed_in" not in row:
					code = event.get("unreadable")
					row["signed_in"] = None if code == "page_blank" else code != "signed_out"
				if event.get("balance") is not None and "balance" not in row:
					row["balance"], row["balance_at"] = event["balance"], event.get("ts")
			if event.get("type") == "identity_check" and "identity_last_status" not in row:
				row["identity_last_status"], row["identity_checked_at"] = event.get("status"), event.get("ts")
	result = []
	import identity
	for number, account_id in enumerate([*active, *paused], 1):
		row = observed.get(account_id, {})
		if "signed_in" not in row and (runs / f"signin-{account_id.replace('@', '')}.json").is_file():
			try:
				if _signin_status(account_id)["state"] == "signed_in":
					row["signed_in"] = True
			except ValueError:
				pass
		label = labels.get(account_id) or i18n.current().t("accounts.default_label", number=number)
		picture = _picture_source(account_id)
		try:
			bound = identity.binding(account_id, paths.data_home())
			identity_status = "confirmed" if bound else "unconfirmed"
		except identity.IdentityError:
			bound, identity_status = None, "unknown"
		result.append({"id": account_id, "label": label, "dir": str(_profile_path(account_id)), "paused": account_id in paused, "is_root": account_id == "@root", **{key: row.get(key) for key in ("signed_in", "balance", "balance_at", "last_run_at", "last_result")}, **_profile_identity(account_id), "picture_rev": picture[2] if picture is not None else None,
			"identity_status": identity_status, "identity_confirmed_at": bound["confirmed_at"] if bound else None,
			"identity_last_status": row.get("identity_last_status"), "identity_checked_at": row.get("identity_checked_at")})
	return result


def _known_account(account_id: str) -> dict:
	return next((row for row in _list_accounts() if row["id"] == account_id), None) or _raise_unknown()


def _raise_unknown():
	raise _error("accounts.unknown")


def _start_signin(account_id: str, new: bool, enroll=False) -> dict:
	_profile_path(account_id)
	signin._cancel_path(account_id).unlink(missing_ok=True)
	starting = signin._write_status(account_id, "starting", pid=0)
	# Until the child rewrites the file, its 30-second grace period matters
	# more than the window process's pid.
	args = ["--signin", account_id, *( ["--new"] if new else [])]
	if enroll: args.append("--enroll")
	flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
	options = {"cwd": paths.data_home(), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
	try:
		try:
			subprocess.Popen(paths.launcher_command(args), creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), **options)
		except OSError:
			subprocess.Popen(paths.launcher_command(args), creationflags=flags, **options)
	except OSError as exc:
		_logger.exception("Could not start the sign-in process")
		starting = signin._write_status(account_id, "failed", "unknown", str(exc))
	return {key: starting[key] for key in ("account_id", "state", "error_code", "message_key", "detail", "at")}


def _add_account(confirm_migration=False) -> dict:
	import identity
	journal_path = paths.data_home() / "account-migration.json"
	if journal_path.exists():
		_ensure_idle()
		journal = json.loads(read_file(journal_path))
		if journal.get("root") != str(settings.user_data_dir().resolve()): raise _error("accounts.migration_pending")
		account_id = journal.get("requested")
		_profile_path(account_id)
		return {"account_id": account_id, "signin": _start_signin(account_id, True, enroll=True)}
	registered = [*settings.active_accounts(), *settings.paused_accounts()]
	if registered:
		missing = [account for account in registered if identity.binding(account, paths.data_home()) is None]
		if missing:
			raise _error("accounts.enrollment_required", params={"account_ids": missing})
		if identity.load(paths.data_home())["pending"]:
			raise _error("accounts.enrollment_pending")
	_ensure_idle()
	migration = "@root" in settings.active_accounts()
	if migration and not confirm_migration:
		raise _error("accounts.migration_needed")
	account_id = _free_ids(2 if migration else 1)[-1]
	return {"account_id": account_id, "signin": _start_signin(account_id, True, enroll=True) if registered else _start_signin(account_id, True)}


def _migrate_root_profile(new_account_id: str) -> str:
	import run_lock
	import identity
	root = settings.user_data_dir()
	info = run_lock.status(directory=str(root))
	if info is None or info.owner != "signin" or info.pid != os.getpid():
		raise _error("run.busy")
	_profile_path(new_account_id)
	journal_path = paths.data_home() / "account-migration.json"
	if journal_path.exists():
		journal = json.loads(read_file(journal_path))
		if journal.get("root") != str(root.resolve()) or journal.get("requested") != new_account_id or not isinstance(journal.get("entries"), list):
			raise _error("accounts.migration_pending")
		target_id = journal["target"]
		_profile_path(target_id)
		if any(not isinstance(name, str) or name != Path(name).name or name in (".", "..") for name in journal["entries"]):
			raise _error("accounts.migration_pending")
	else:
		aliases = prefs.load().get("history_aliases", [])
		if not (root / "Local State").exists() and aliases:
			alias = aliases[-1]
			if alias.get("from") == "@root" and (_profile_path(alias["to"]) / "Local State").exists(): return alias["to"]
		target_id = _free_ids(exclude=(new_account_id,))[0]
		entries = _root_entries(exclude=(target_id,))
		documents = (paths.env_path(), paths.prefs_path(), paths.data_home() / "account-identities.json")
		journal = {"v": 1, "root": str(root.resolve()), "requested": new_account_id, "target": target_id, "entries": [entry.name for entry in entries],
			"before": {path.name: base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else None for path in documents}}
		identity.write_document(journal_path, journal)
	target = root / target_id
	target.mkdir(exist_ok=True)
	try:
		for name in journal["entries"]:
			entry, destination = root / name, target / name
			if entry.exists() and not destination.exists(): os.rename(entry, destination)
			elif entry.exists() or not destination.exists(): raise OSError("Ambiguous migration entry")
		editor = EnvFile.load(paths.env_path())
		editor.set("REWARDS_ACCOUNTS", target_id)
		editor.save()
		bindings = identity.load(paths.data_home())
		if "@root" in bindings["bindings"]:
			bindings["bindings"][target_id] = {**bindings["bindings"].pop("@root"), "account_id": target_id}
		if target_id not in bindings["used_ids"]: bindings["used_ids"].append(target_id)
		identity.save(bindings, paths.data_home())
		state = prefs.load()
		changes = {}
		for key in ("account_labels", "goals"):
			values = dict(state[key])
			if "@root" in values:
				values[target_id] = values.pop("@root")
				changes[key] = values
		aliases = list(state.get("history_aliases", []))
		if not any(item.get("from") == "@root" and item.get("to") == target_id for item in aliases):
			aliases.append({"from": "@root", "to": target_id, "before": identity.utc()})
		changes["history_aliases"] = aliases
		prefs.save(changes)
	except Exception as exc:
		try:
			for name in reversed(journal["entries"]):
				if (target / name).exists() and not (root / name).exists(): os.rename(target / name, root / name)
			target.rmdir()
			for name, content in journal["before"].items():
				path = paths.data_home() / name
				if content is None: path.unlink(missing_ok=True)
				else: _restore_document(path, base64.b64decode(content))
			journal_path.unlink()
		except OSError as rollback:
			_logger.error("Profile migration rollback incomplete (%s)", type(rollback).__name__)
			raise _error("accounts.migration_pending") from None
		raise _error("profile_in_use", type(exc).__name__) from None
	journal_path.unlink(missing_ok=True)
	return target_id


def _restore_document(path, content):
	name = None
	try:
		with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".account-restore-", delete=False) as stream:
			name = stream.name
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		replace_file(name, path)
	finally:
		if name is not None: Path(name).unlink(missing_ok=True)


def _rename_account(account_id: str, label: str) -> dict:
	_known_account(account_id)
	if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40 or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in label):
		raise _error("bad_request")
	labels = prefs.load()["account_labels"]
	labels[account_id] = label.strip()
	prefs.save({"account_labels": labels})
	return _known_account(account_id)


def _write_account_lists(active, paused) -> None:
	editor = EnvFile.load(paths.env_path())
	editor.set("REWARDS_ACCOUNTS", ",".join(active))
	editor.set("REWARDS_PAUSED_ACCOUNTS", ",".join(paused))
	editor.save()


def _set_account_paused(account_id: str, paused: bool) -> dict:
	_known_account(account_id)
	if type(paused) is not bool:
		raise _error("bad_request")
	# The root fallback is an app-only id, never a persisted account entry.
	active = settings._entries(EnvFile.load(paths.env_path()).get("REWARDS_ACCOUNTS"))
	inactive = settings.paused_accounts()
	if account_id == "@root" or paused and account_id in active and len(active) == 1:
		raise _error("accounts.last_active")
	if paused and account_id in active:
		active.remove(account_id)
		inactive.append(account_id)
	elif not paused and account_id in inactive:
		inactive.remove(account_id)
		active.append(account_id)
	_write_account_lists(active, inactive)
	from rfapp import schedule
	schedule.ensure()
	return _known_account(account_id)


def _legacy_install() -> dict:
	if not paths.is_frozen():
		return {"found": False, "path": None, "accounts": []}
	root = Path(os.environ["LOCALAPPDATA"]) / "rewards-farmer"
	if not (root / ".env").is_file():
		return {"found": False, "path": None, "accounts": []}
	editor = EnvFile.load(root / ".env")
	accounts = settings._entries(editor.get("REWARDS_ACCOUNTS"))
	if not accounts and (root / "data-dir" / "Local State").is_file():
		accounts = ["@root"]
	return {"found": True, "path": str(root), "accounts": accounts}


def _import_legacy_install() -> list[dict]:
	_ensure_idle()
	legacy = _legacy_install()
	if not legacy["found"]:
		raise _error("accounts.legacy_not_found")
	root = Path(legacy["path"])
	source = EnvFile.load(root / ".env")
	editor = EnvFile.load(paths.env_path())
	for key in _LEGACY_KEYS:
		value = source.get(key)
		if value is not None:
			editor.set(key, value)
	editor.set("USER_DATA_DIR", str(root / "data-dir"))
	editor.save()
	return _list_accounts()


def _sign_in_again(account_id: str) -> dict:
	_known_account(account_id)
	_ensure_idle()
	return _start_signin(account_id, False)


def begin_enrollment(account_id):
	_known_account(account_id)
	_ensure_idle()
	return {"signin": _start_signin(account_id, False, enroll=True)}


def get_enrollment_status(account_id):
	import identity
	_profile_path(account_id)
	pending = identity.pending(account_id, paths.data_home())
	if pending is None:
		_known_account(account_id)
		return {"account_id": account_id, "state": "not_observed", "observed_name": None, "confirmation_token": None, "observed_at": None, "same_name_limit": True}
	return {"account_id": account_id, "state": "awaiting_confirmation", "observed_name": pending["observed_name"], "confirmation_token": pending["token"], "observed_at": pending["observed_at"], "same_name_limit": True}


def confirm_enrollment(account_id, confirmation_token, confirmed):
	import identity
	_ensure_idle()
	_profile_path(account_id)
	if confirmed is not True or not isinstance(confirmation_token, str):
		raise _error("accounts.enrollment_required")
	pending = identity.pending(account_id, paths.data_home())
	if pending is None or not secrets.compare_digest(pending["token"], confirmation_token):
		raise _error("accounts.enrollment_required")
	before = identity.load(paths.data_home())
	old = identity.binding(account_id, paths.data_home())
	if old is not None and old["expected_name_hash"] != pending["observed_hash"]:
		raise _error("accounts.identity_mismatch")
	value = json.loads(json.dumps(before))
	value["bindings"][account_id] = old or {"account_id": account_id, "expected_name_hash": pending["observed_hash"], "confirmed_at": identity.utc()}
	active, paused = settings.active_accounts(), settings.paused_accounts()
	if pending["new"] and any(identity.binding(account, paths.data_home()) is None for account in [*active, *paused]):
		raise _error("accounts.enrollment_required")
	if pending["new"] and account_id not in [*active, *paused]: active.append(account_id)
	registered = [*active, *paused]
	if len(registered) > 1 and all(account in value["bindings"] for account in registered): value["mode"] = "managed"
	# Keep a failed enrollment retryable; registration is last, after the binding.
	editor = EnvFile.load(paths.env_path())
	prefs_before = paths.prefs_path().read_bytes() if paths.prefs_path().exists() else None
	env_before = paths.env_path().read_bytes() if paths.env_path().exists() else None
	try:
		identity.save(value, paths.data_home())
		if value["mode"] == "managed": prefs.save({"identity_managed": True})
		if pending["new"]:
			editor.set("REWARDS_ACCOUNTS", ",".join(item for item in active if item != "@root"))
			editor.save()
		value["pending"].pop(account_id, None)
		identity.save(value, paths.data_home())
	except OSError:
		identity.save(before, paths.data_home())
		if prefs_before is None: paths.prefs_path().unlink(missing_ok=True)
		else: _restore_document(paths.prefs_path(), prefs_before)
		if env_before is None: paths.env_path().unlink(missing_ok=True)
		else: _restore_document(paths.env_path(), env_before)
		raise _error("accounts.enrollment_save_failed") from None
	if pending["new"]:
		from rfapp import schedule
		schedule.ensure()
	return {"account_id": account_id, "identity_status": "confirmed", "confirmed_at": value["bindings"][account_id]["confirmed_at"], "managed": value["mode"] == "managed", "same_name_limit": True}


def _cancel_signin(account_id: str) -> None:
	status = _signin_status(account_id)
	import identity
	value = identity.load(paths.data_home())
	if account_id in value["pending"]:
		value["pending"].pop(account_id, None)
		identity.save(value, paths.data_home())
		signin._write_status(account_id, "cancelled")
		return
	signin._cancel_path(account_id).write_text("", encoding="utf-8")


def _remove_account(account_id: str, delete_data: bool) -> dict:
	_known_account(account_id)
	if type(delete_data) is not bool:
		raise _error("bad_request")
	import run_lock
	from rfapp import schedule
	if run_lock.status(directory=str(settings.user_data_dir())) is not None:
		raise _error("run.busy")
	if account_id == "@root" and not delete_data:
		raise _error("accounts.root_needs_delete")
	warnings = []
	profile = _profile_path(account_id)
	if delete_data:
		if account_id == "@root":
			# A CLI data root may also contain files that do not belong to Edge.
			entries = [entry for entry in (profile / "Default", profile / "Local State") if entry.exists()]
			_recycle(entries)
			if (profile / "Local State").exists():
				raise _error("accounts.root_recycle_failed")
			remaining = profile / "Default" if (profile / "Default").exists() else None
		else:
			remaining = profile if not _recycle([profile] if profile.exists() else []) else None
		if remaining is not None:
			warnings.append({"code": "accounts.recycle_failed", "message_key": "errors.app.accounts_recycle_failed", "params": {"path": str(remaining)}})
	active = [entry for entry in settings.active_accounts() if entry != account_id and entry != "@root"]
	paused = [entry for entry in settings.paused_accounts() if entry != account_id]
	_write_account_lists(active, paused)
	import identity
	bindings = identity.load(paths.data_home())
	bindings["bindings"].pop(account_id, None)
	bindings["pending"].pop(account_id, None)
	if account_id not in bindings["used_ids"]: bindings["used_ids"].append(account_id)
	identity.save(bindings, paths.data_home())
	state = prefs.load()
	for key in ("account_labels", "goals"):
		state[key].pop(account_id, None)
	prefs.save({key: state[key] for key in ("account_labels", "goals")})
	rows = _list_accounts()
	if rows:
		schedule.ensure()
	else:
		schedule.remove()
	return {"accounts": rows, "warnings": warnings}


def list_accounts() -> dict:
	return {"accounts": _list_accounts()}


def get_legacy_install() -> dict:
	return _legacy_install()


def import_legacy_install() -> dict:
	return {"accounts": _import_legacy_install()}


def add_account(confirm_migration=False) -> dict:
	return _add_account(confirm_migration)


def sign_in_again(account_id) -> dict:
	return {"signin": _sign_in_again(account_id)}


def get_signin_status(account_id) -> dict:
	return _signin_status(account_id)


def cancel_signin(account_id) -> dict:
	_cancel_signin(account_id)
	return {}


def rename_account(account_id, label) -> dict:
	return {"account": _rename_account(account_id, label)}


def set_account_paused(account_id, paused) -> dict:
	return {"account": _set_account_paused(account_id, paused)}


def remove_account(account_id, delete_data) -> dict:
	return _remove_account(account_id, delete_data)


def migrate_root_profile(new_account_id: str) -> str:
	return _migrate_root_profile(new_account_id)
