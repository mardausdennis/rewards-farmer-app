"""Atomic app-only preferences, retaining keys from later app versions."""

import copy
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from file_replace import read_file, replace_file
from rfapp import paths


_DEFAULTS = {
	"v": 1,
	"language": "system",
	"close_to_tray": True,
	"update_check": True,
	"update_last_checked": None,
	"update_dismissed_version": None,
	"setup_done": False,
	"tray_hint_shown": False,
	"account_labels": {},
	"invited_friends": [],
	"goals": {},
	"window": {"width": 1100, "height": 760, "x": None, "y": None, "maximized": False},
	"last_route": "#/overview",
	"log_panel_open": False,
}
_lock = threading.RLock()
_logger = logging.getLogger("rfapp")


def load() -> dict:
	values = copy.deepcopy(_DEFAULTS)
	try:
		stored = json.loads(read_file(paths.prefs_path()))
		if not isinstance(stored, dict):
			raise ValueError("Preferences must be an object")
	except FileNotFoundError:
		return values
	except (ValueError, OSError):
		_logger.exception("Could not read app preferences")
		return values
	values.update(stored)
	return values


class FriendNotesError(ValueError):
	code = "bad_request"


def _friend_text(value, limit, *, email=False):
	if not isinstance(value, str):
		raise FriendNotesError("bad_request")
	value = value.strip()
	if not 1 <= len(value) <= limit or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
		raise FriendNotesError("bad_request")
	if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
		raise FriendNotesError("bad_request")
	return value


def _friend_id(value):
	if not isinstance(value, str) or not re.fullmatch(r"friend-[0-9a-f]{32}", value):
		raise FriendNotesError("bad_request")
	return value


def invited_friends() -> list[dict]:
	stored = load().get("invited_friends", [])
	if not isinstance(stored, list):
		return []
	result, seen = [], set()
	for item in stored:
		try:
			if not isinstance(item, dict):
				continue
			identity = _friend_id(item.get("id"))
			added_at = item.get("added_at")
			if identity in seen or not isinstance(added_at, str) or datetime.fromisoformat(added_at.replace("Z", "+00:00")).tzinfo is None:
				continue
			result.append({"id": identity, "name": _friend_text(item.get("name"), 40), "email": _friend_text(item.get("email"), 254, email=True), "added_at": added_at})
			seen.add(identity)
		except ValueError:
			continue
	return result


def save_invited_friend(name, email, identity=None) -> dict:
	name, email = _friend_text(name, 40), _friend_text(email, 254, email=True)
	with _lock:
		friends = invited_friends()
		if identity is None:
			friend = {"id": "friend-" + secrets.token_hex(16), "name": name, "email": email, "added_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
			friends.append(friend)
		else:
			identity = _friend_id(identity)
			friend = next((item for item in friends if item["id"] == identity), None)
			if friend is None:
				raise FriendNotesError("bad_request")
			friend.update(name=name, email=email)
		save({"invited_friends": friends})
		return dict(friend)


def remove_invited_friend(identity) -> list[dict]:
	identity = _friend_id(identity)
	with _lock:
		friends = invited_friends()
		if not any(item["id"] == identity for item in friends):
			raise FriendNotesError("bad_request")
		friends = [item for item in friends if item["id"] != identity]
		save({"invited_friends": friends})
		return friends


def save(changes: dict) -> dict:
	with _lock:
		values = load()
		for key, value in changes.items():
			values[key] = copy.deepcopy(value)
		path = paths.prefs_path()
		path.parent.mkdir(parents=True, exist_ok=True)
		name = None
		try:
			with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".prefs-", delete=False) as stream:
				name = stream.name
				json.dump(values, stream, ensure_ascii=False, indent=2)
				stream.write("\n")
				stream.flush()
				os.fsync(stream.fileno())
			replace_file(name, path)
		finally:
			if name is not None:
				Path(name).unlink(missing_ok=True)
		return values
