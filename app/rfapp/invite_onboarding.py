"""Private, serialized invite-first operations. No Microsoft success is inferred."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from file_replace import read_file
from rfapp import paths, settings


_thread_lock = threading.RLock()
_PHASES = frozenset(("preview", "launching", "invite_open", "continue_requested", "verifying", "awaiting_confirmation", "admitting", "registered", "interrupted", "cancelled", "discarded"))
_ACTIVE = frozenset(("launching", "invite_open", "continue_requested", "verifying"))
_RESUMABLE = frozenset(("preview", "interrupted", "cancelled"))
_DISCARDABLE = _RESUMABLE | {"awaiting_confirmation"}
_TERMINAL = frozenset(("registered", "discarded"))
_CODES = frozenset(("invite_invalid", "invite_busy", "invite_stale", "invite_link_unavailable", "invite_root", "invite_expired", "invite_token", "invite_interrupted", "invite_ineligible", "invite_unknown", "invite_storage", "invite_registered"))
_logger = logging.getLogger("rfapp")


class InviteError(ValueError):
	def __init__(self, code="invite_invalid"):
		self.code = "accounts." + (code if code in _CODES else "invite_invalid")
		super().__init__(self.code)


def operation_id(value):
	if not isinstance(value, str) or re.fullmatch(r"invite-[0-9a-f]{32}", value) is None:
		raise InviteError()
	return value


def _target(value):
	if not isinstance(value, str) or re.fullmatch(r"account-[1-9][0-9]*", value) is None:
		raise InviteError()
	return value


def _safe(path):
	path = Path(path).absolute()
	if ".." in path.parts: raise InviteError()
	if any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
		raise InviteError()
	return path


def _utc():
	return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stamp(value):
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
		if parsed.tzinfo is not None: return parsed.astimezone(timezone.utc)
	except (TypeError, ValueError, AttributeError):
		pass
	raise InviteError()


def _digest(value):
	return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _token(value):
	return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _link(value):
	import referrals
	link = referrals._link(value)
	if link is None: raise InviteError("invite_link_unavailable")
	try:
		p = urlsplit(link)
		# Source is the local reader, never a caller-supplied URL. Bound query
		# parsing and reject malformed escapes rather than changing its contents.
		if re.search(r"%(?![0-9A-Fa-f]{2})", link): raise ValueError()
		pairs = parse_qsl(p.query, keep_blank_values=True, max_num_fields=32)
		if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key) or len(val) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in val) for key, val in pairs): raise ValueError()
	except ValueError:
		raise InviteError("invite_link_unavailable") from None
	return link


@contextmanager
def _locked():
	# Separate processes share a byte lock; replacement never changes the lock
	# inode. The file and operation payload are private and not diagnostic inputs.
	with _thread_lock:
		home = _safe(paths.data_home())
		home.mkdir(parents=True, exist_ok=True)
		lock_path = _safe(home / ".invite-onboarding.lock")
		with lock_path.open("a+b") as stream:
			if stream.tell() == 0:
				stream.write(b"0")
				stream.flush()
			deadline = time.monotonic() + 2
			while True:
				try:
					stream.seek(0)
					if os.name == "nt":
						import msvcrt
						msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
					else:
						import fcntl
						fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
					break
				except OSError:
					if time.monotonic() >= deadline: raise InviteError("invite_busy") from None
					time.sleep(.05)
			try: yield
			finally:
				stream.seek(0)
				if os.name == "nt": msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
				else: fcntl.flock(stream, fcntl.LOCK_UN)


def _load():
	path = _safe(paths.data_home() / "invite-onboarding.json")
	try:
		if path.stat().st_size > 2 * 1024 * 1024: raise InviteError("invite_storage")
		value = json.loads(read_file(path))
	except FileNotFoundError:
		return {"v": 1, "operations": {}}
	except (OSError, ValueError):
		raise InviteError("invite_storage") from None
	if not isinstance(value, dict) or value.get("v") != 1 or not isinstance(value.get("operations"), dict): raise InviteError("invite_storage")
	return value


def _save(value):
	import identity
	identity.write_document(_safe(paths.data_home() / "invite-onboarding.json"), value)


def _validate(op):
	try:
		operation_id(op["operation_id"])
		_target(op["target_account_id"])
		if not isinstance(op["source_account_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", op["source_account_id"]) or op["source_account_id"] in (".", "..") or op["source_account_id"].endswith("."): raise InviteError()
		if op["phase"] not in _PHASES or op["source_account_id"] == op["target_account_id"] or op["source_account_id"] == "@root": raise InviteError()
		if op["home"] != str(_safe(paths.data_home())) or op["profile_root"] != str(_safe(settings.user_data_dir())): raise InviteError("invite_stale")
		if not _token(op["preview_token"]) or not _token(op["continue_token"]) or not re.fullmatch(r"[0-9a-f]{64}", op["source_revision"]): raise InviteError()
		_stamp(op["preview_at"])
		_stamp(op["link_read_at"])
		if "launched_at" in op: _stamp(op["launched_at"])
		if type(op["profile_owned"]) is not bool or op.get("pid") is not None and (type(op["pid"]) is not int or op["pid"] < 0): raise InviteError()
		import referrals
		if op.get("unreadable") is not None and op["unreadable"] not in referrals.UNREADABLE_CODES: raise InviteError()
		if op.get("error_code") is not None and op["error_code"] not in {"accounts." + code for code in _CODES}: raise InviteError()
		_link(op["link"])
	except (KeyError, TypeError, ValueError) as exc:
		if isinstance(exc, InviteError): raise
		raise InviteError() from None
	return op


def _get(value, op_id):
	op = value["operations"].get(operation_id(op_id))
	if not isinstance(op, dict) or op.get("operation_id") != op_id: raise InviteError()
	return _validate(op)


def _terminal(op):
	return isinstance(op, dict) and isinstance(op.get("phase"), str) and op["phase"] in _TERMINAL


def _issue(code, op_id=None):
	# No exception body or stored field belongs in this causal record.
	_logger.warning("invite_recovery code=%s operation=%s", code, op_id if isinstance(op_id, str) and re.fullmatch(r"invite-[0-9a-f]{32}", op_id) else "unknown")


def _handle(key):
	try: return operation_id(key)
	except InviteError: return "invite-" + _digest(key)[:32]


def recovery_status(op_id, code="accounts.invite_storage", *, raw=None, can_discard=False):
	code = code if code in {"accounts." + item for item in _CODES} else "accounts.invite_storage"
	value = raw if isinstance(raw, dict) else {}
	def account(key):
		try: return _target(value.get(key))
		except InviteError: return None
	return {"operation_id": _handle(op_id), "source_account_id": account("source_account_id"), "target_account_id": account("target_account_id"),
		"phase": "interrupted", "link_read_at": None, "unreadable": None, "preview_at": None, "referral_status": "unknown", "continue_token": None,
		"can_resume": False, "can_discard": can_discard, "recovery_only": True, "error_code": code}


def _projection(key, raw):
	try:
		if not isinstance(raw, dict) or raw.get("operation_id") != key: raise InviteError()
		return _public(_validate(raw))
	except (InviteError, OSError) as exc:
		code = getattr(exc, "code", "accounts.invite_storage")
		return recovery_status(key, code, raw=raw, can_discard=True)


def _store_failure():
	# A corrupt envelope still has an explicit, revision-bound discard handle.
	# This never reads a profile and never returns any byte from the payload.
	try:
		path = _safe(paths.data_home() / "invite-onboarding.json")
		with path.open("rb") as stream:
			hash_value = hashlib.file_digest(stream, "sha256").hexdigest()
	except (OSError, InviteError):
		hash_value = _digest("unreadable-store")
		return recovery_status("invite-" + hash_value[:32])
	return recovery_status("invite-" + hash_value[:32], can_discard=True)


def _drop_pending(op_id, raw=None, *, all_tagged=False):
	import identity
	value = identity.load(paths.data_home())
	changed = False
	for target, pending in list(value["pending"].items()):
		if not isinstance(pending, dict) or pending.get("new") is not True: continue
		tagged = pending.get("invitation_operation_id")
		linked = tagged == op_id if not all_tagged else isinstance(tagged, str) and re.fullmatch(r"invite-[0-9a-f]{32}", tagged) is not None
		if not linked and isinstance(raw, dict) and raw.get("target_account_id") == target and _token(pending.get("token")):
			linked = secrets.compare_digest(str(raw.get("enrollment_token_hash") or ""), _digest(pending["token"]))
		if linked:
			value["pending"].pop(target)
			changed = True
	if changed: identity.save(value, paths.data_home())


def _remember_recovery(value, op_id, code):
	rows = value.get("recovery") if isinstance(value.get("recovery"), list) else []
	value["recovery"] = [*rows[-31:], {"operation_id": _handle(op_id), "code": code, "at": _utc()}]


def reserved_targets():
	with _locked():
		try: ops = _load()["operations"]
		except InviteError:
			_issue("accounts.invite_storage")
			return []
		return [row["target_account_id"] for key, op in ops.items()
			if isinstance(op, dict) and not _terminal(op) and not (row := _projection(key, op))["recovery_only"]]


def _source(source_id):
	import identity
	import referrals
	registered = [*settings.active_accounts(), *settings.paused_accounts()]
	if source_id not in registered: raise InviteError()
	if "@root" in registered or (settings.user_data_dir() / "Local State").exists() or (paths.data_home() / "account-migration.json").exists(): raise InviteError("invite_root")
	if any(identity.binding(account, paths.data_home()) is None for account in registered): raise InviteError("invite_ineligible")
	info = referrals.get_referral_info(source_id, directory=paths.data_home())
	link = _link(info.get("link"))
	if info.get("link_found") is not True or _stamp(info.get("read_at")) > datetime.now(timezone.utc): raise InviteError("invite_link_unavailable")
	bound = identity.binding(source_id, paths.data_home())
	packet = {"source_account_id": source_id, "link": link, "link_read_at": info["read_at"], "unreadable": info.get("unreadable"), "binding": bound}
	return packet, _digest(packet)


def _public(op, preview=False):
	result = {key: op.get(key) for key in ("operation_id", "source_account_id", "target_account_id", "phase", "link_read_at", "unreadable", "preview_at")}
	result.update(referral_status="unknown", continue_token=op["continue_token"] if op["phase"] == "invite_open" else None,
		can_resume=op["phase"] in _RESUMABLE, can_discard=op["phase"] in _DISCARDABLE, recovery_only=False, error_code=op.get("error_code"))
	if preview: result.update(link=op["link"], preview_token=op["preview_token"])
	return result


def prepare(source_id, choose_target, op_id=None):
	with _locked():
		packet, revision = _source(source_id)
		value = _load()
		current = [row for key, op in value["operations"].items() if not (row := _projection(key, op))["recovery_only"]]
		if any(row["phase"] in _ACTIVE | {"admitting", "awaiting_confirmation"} for row in current): raise InviteError("invite_busy")
		if op_id is None:
			if any(row["phase"] in _RESUMABLE for row in current): raise InviteError("invite_busy")
			target = _target(choose_target([row["target_account_id"] for row in current]))
			op_id = "invite-" + secrets.token_hex(16)
			op = {"operation_id": op_id, "target_account_id": target, "home": str(_safe(paths.data_home())), "profile_root": str(_safe(settings.user_data_dir())), "profile_owned": False}
			if (settings.user_data_dir() / target).exists(): raise InviteError()
		else:
			op = _get(value, op_id)
			if op["phase"] not in _RESUMABLE or op["source_account_id"] != source_id: raise InviteError()
		op.update(source_account_id=source_id, link=packet["link"], link_read_at=packet["link_read_at"], unreadable=packet["unreadable"], source_revision=revision,
			preview_token=secrets.token_hex(16), continue_token=secrets.token_hex(16), preview_at=_utc(), phase="preview", pid=None, error_code=None, enrollment_token_hash=None)
		value["operations"][op_id] = op
		_save(value)
		return _public(op, preview=True)


def launch(op_id, preview_token):
	with _locked():
		value = _load()
		op = _get(value, op_id)
		if op["phase"] != "preview" or not _token(preview_token) or not secrets.compare_digest(op["preview_token"], preview_token): raise InviteError("invite_token")
		if not 0 <= (datetime.now(timezone.utc) - _stamp(op["preview_at"])).total_seconds() <= 900: raise InviteError("invite_expired")
		_, revision = _source(op["source_account_id"])
		if revision != op["source_revision"]: raise InviteError("invite_stale")
		op.update(phase="launching", pid=0, launched_at=_utc(), continue_token=secrets.token_hex(16))
		_save(value)
		return _public(op)


def child_begin(op_id, target):
	with _locked():
		value = _load()
		op = _get(value, op_id)
		if op["phase"] != "launching" or op["pid"] != 0 or target != op["target_account_id"]: raise InviteError("invite_token")
		_, revision = _source(op["source_account_id"])
		if revision != op["source_revision"]: raise InviteError("invite_stale")
		profile = _safe(settings.user_data_dir() / target)
		marker = _safe(profile / ".invite-onboarding-owner.json")
		if target in [*settings.active_accounts(), *settings.paused_accounts()]: raise InviteError()
		if profile.exists():
			try:
				if not op["profile_owned"]: raise InviteError()
				if not marker.exists() and not any(profile.iterdir()):
					import identity
					identity.write_document(marker, {"operation_id": op_id})
				if json.loads(read_file(marker)) != {"operation_id": op_id}: raise InviteError()
			except (OSError, ValueError): raise InviteError() from None
		else:
			# Publish reservation before profile creation. After a crash an empty
			# owned directory can be reconciled, never an existing populated one.
			op["profile_owned"] = True
			_save(value)
			profile.mkdir(parents=True)
			import identity
			identity.write_document(marker, {"operation_id": op_id})
		op.update(profile_owned=True, pid=os.getpid())
		_save(value)
		return {"link": op["link"], "profile": profile}


def child_phase(op_id, phase, enrollment_token=None):
	with _locked():
		value = _load()
		op = _get(value, op_id)
		allowed = {"invite_open": "launching", "verifying": "continue_requested", "awaiting_confirmation": "verifying"}
		if op["pid"] != os.getpid() or allowed.get(phase) != op["phase"]: raise InviteError("invite_token")
		if phase == "awaiting_confirmation":
			if not _token(enrollment_token): raise InviteError()
			op["enrollment_token_hash"] = _digest(enrollment_token)
		op["phase"] = phase
		_save(value)
		return _public(op)


def stage_confirmation(op_id, observed_name):
	# Serialize cancellation with creation of pending identity; cancellation
	# cannot leave a later, unowned pending token behind.
	with _locked():
		value = _load()
		op = _get(value, op_id)
		if op["phase"] != "verifying" or op["pid"] != os.getpid(): raise InviteError("invite_token")
		import identity
		before = identity.load(paths.data_home())
		try:
			pending = identity.stage_enrollment(op["target_account_id"], observed_name, new=True, directory=paths.data_home())
			bindings = identity.load(paths.data_home())
			bindings["pending"][op["target_account_id"]]["invitation_operation_id"] = op_id
			identity.save(bindings, paths.data_home())
			op.update(enrollment_token_hash=_digest(pending["token"]), phase="awaiting_confirmation")
			_save(value)
		except BaseException:
			identity.save(before, paths.data_home())
			raise
		return _public(op)


def read(op_id):
	with _locked(): return _public(_get(_load(), op_id))


def status(op_id):
	"""Observer-only safe projection; invalid operations never block ordinary UI."""
	with _locked():
		try:
			value = _load()
		except InviteError:
			return _store_failure()
		op = value["operations"].get(op_id) if isinstance(op_id, str) else None
		row = _projection(op_id, op)
		if row["recovery_only"]: _issue(row["error_code"], row["operation_id"])
		return row


def list_operations(pid_alive):
	with _locked():
		try: value = _load()
		except InviteError:
			row = _store_failure()
			_issue(row["error_code"], row["operation_id"])
			return [row]
		changed = False
		rows = []
		for key, op in value["operations"].items():
			if _terminal(op): continue
			row = _projection(key, op)
			if row["recovery_only"]:
				rows.append(row)
				continue
			if op["phase"] == "admitting" and not pid_alive(op.get("pid")):
				import identity
				# Reconcile an interrupted local commit without registering anything.
				complete = op["target_account_id"] in [*settings.active_accounts(), *settings.paused_accounts()] and identity.binding(op["target_account_id"], paths.data_home()) is not None
				op.update(phase="registered" if complete else "interrupted", error_code=None if complete else "accounts.invite_interrupted")
				if not complete: _drop_pending(key, op)
				changed = True
			age = (datetime.now(timezone.utc) - _stamp(op.get("launched_at", op["preview_at"]))).total_seconds()
			if op["phase"] in _ACTIVE and not (op.get("pid") == 0 and age < 30) and not pid_alive(op.get("pid")):
				_drop_pending(key, op)
				op.update(phase="interrupted", error_code="accounts.invite_interrupted")
				changed = True
			if op["phase"] not in _TERMINAL: rows.append(_public(op))
		if changed: _save(value)
		return rows


def continue_operation(op_id, token, pid_alive):
	with _locked():
		value = _load()
		op = _get(value, op_id)
		if op["phase"] != "invite_open" or not _token(token) or not secrets.compare_digest(op["continue_token"], token): raise InviteError("invite_token")
		if not pid_alive(op.get("pid")): raise InviteError("invite_interrupted")
		op["phase"] = "continue_requested"
		_save(value)
		return _public(op)


def interrupt(op_id, code="invite_interrupted", cancelled=False):
	with _locked():
		value = _load()
		op = _get(value, op_id)
		if op["phase"] in ("registered", "discarded", "admitting"): return _public(op)
		_drop_pending(op_id, op)
		op.update(phase="cancelled" if cancelled else "interrupted", error_code=InviteError(code).code)
		_save(value)
		return _public(op)


def for_target(target):
	with _locked():
		try: ops = _load()["operations"]
		except InviteError:
			_issue("accounts.invite_storage")
			return None
		for key, op in reversed(list(ops.items())):
			if not isinstance(op, dict) or op.get("target_account_id") != target or _terminal(op): continue
			row = _projection(key, op)
			if not row["recovery_only"]: return row
			_issue(row["error_code"], row["operation_id"])
		return None


def pending_origin(target, token):
	"""Recognize legacy, untagged invite tokens without following stale roots."""
	with _locked():
		try: ops = _load()["operations"]
		except InviteError: return None
		for key, op in ops.items():
			if isinstance(op, dict) and op.get("target_account_id") == target and isinstance(op.get("enrollment_token_hash"), str) and secrets.compare_digest(op["enrollment_token_hash"], _digest(token)):
				return _handle(key)
		return None


def admit(target, token):
	with _locked():
		try: value = _load()
		except InviteError:
			_issue("accounts.invite_storage")
			return None
		ops = [op for op in value["operations"].values() if isinstance(op, dict) and op.get("target_account_id") == target and not _terminal(op)]
		if not ops: return None
		# A stale or interrupted invitation cannot intercept ordinary account
		# reconfirmation. Pending invite tokens are checked by the caller's tag.
		if ops[-1].get("phase") not in ("awaiting_confirmation", "admitting"): return None
		row = _projection(ops[-1].get("operation_id"), ops[-1])
		if row["recovery_only"]:
			_issue(row["error_code"], row["operation_id"])
			return None
		op = _validate(ops[-1])
		if op["phase"] != "awaiting_confirmation" or not secrets.compare_digest(op.get("enrollment_token_hash") or "", _digest(token)): raise InviteError("invite_token")
		op.update(phase="admitting", pid=os.getpid())
		_save(value)
		return op["operation_id"]


def finish_admission(op_id, success):
	with _locked():
		value = _load()
		op = _get(value, op_id)
		if op["phase"] != "admitting": raise InviteError()
		op["phase"] = "registered" if success else "awaiting_confirmation"
		_save(value)


def admission_failed(op_id):
	with _locked():
		value = _load()
		op = value["operations"].get(operation_id(op_id))
		if not isinstance(op, dict): raise InviteError()
		op.update(phase="interrupted", error_code="accounts.invite_interrupted")
		try: _drop_pending(op_id, op)
		finally:
			_save(value)
			_issue("accounts.invite_interrupted", op_id)


def discard(op_id, recycle, pid_alive):
	with _locked():
		op_id = operation_id(op_id)
		try: value = _load()
		except InviteError:
			if _store_failure()["operation_id"] != op_id: raise InviteError("invite_stale")
			_drop_pending(op_id, all_tagged=True)
			value = {"v": 1, "operations": {}}
			_remember_recovery(value, op_id, "accounts.invite_storage")
			_save(value)
			_issue("accounts.invite_storage", op_id)
			return {**recovery_status(op_id), "phase": "discarded"}
		key = next((key for key in value["operations"] if _handle(key) == op_id), None)
		if key is None: raise InviteError()
		raw = value["operations"][key]
		row = _projection(key, raw)
		if row["recovery_only"] or _terminal(raw):
			# Forget the private continuation only. Never inspect/recycle a stored
			# root that is stale, corrupt or belongs to a registered account.
			_drop_pending(op_id, raw)
			value["operations"].pop(key)
			code = row.get("error_code") or "accounts.invite_registered"
			_remember_recovery(value, op_id, code)
			_save(value)
			_issue(code, op_id)
			return {**recovery_status(op_id, code, raw=raw), "phase": "discarded"}
		op = _get(value, key)
		if op["phase"] not in _DISCARDABLE: raise InviteError("invite_busy")
		target = op["target_account_id"]
		if target in [*settings.active_accounts(), *settings.paused_accounts()]: raise InviteError("invite_registered")
		profile = _safe(settings.user_data_dir() / target)
		if profile.exists():
			try:
				marker = _safe(profile / ".invite-onboarding-owner.json")
				if not op["profile_owned"]: raise InviteError()
				if marker.exists():
					if json.loads(read_file(marker)) != {"operation_id": op_id}: raise InviteError()
				elif any(profile.iterdir()): raise InviteError()
			except (ValueError, OSError): raise InviteError() from None
			if not recycle([profile]): raise InviteError("invite_storage")
		_drop_pending(op_id, op)
		import identity
		bindings = identity.load(paths.data_home())
		if target not in bindings["used_ids"]: bindings["used_ids"].append(target)
		identity.save(bindings, paths.data_home())
		op["phase"] = "discarded"
		_save(value)
		return _public(op)
