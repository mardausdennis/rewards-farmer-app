"""Read-only referral observation and private, account-bound local snapshots."""

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

import constants
from file_replace import read_file, replace_file
import run_record


TARGET = "https://rewards.bing.com/referandearn/"
# F102 observed the page skeleton, not this relationship or the link value.
SELECTORS_VERIFIED = False
UNREADABLE_CODES = frozenset(("signed_out", "challenge", "page_unknown", "link_missing", "link_ambiguous", "link_invalid", "read_failed", "storage_failed", "storage_unreadable", "not_read"))
REASON_CODES = frozenset(("location", "too_many_controls", "no_main", "not_complete", "no_copy_button", "copy_button_ambiguous", "input_ambiguous", "input_missing", "link_invalid", "invalid_snapshot", "read_failed"))
READY_TIMEOUT = 15.0
POLL_INTERVAL = 0.25


@dataclass(frozen=True)
class ReferralState:
	link: str | None = field(default=None, repr=False)
	link_found: bool = False
	referrals: int | None = None
	friends: list | None = None
	unreadable: str | None = None
	reason: str | None = None


def _allowed_page(value):
	try:
		p = urlsplit(value)
		return p.scheme == "https" and p.hostname == "rewards.bing.com" and p.path in ("/refer", "/refer/", "/referandearn", "/referandearn/") and not p.username and not p.password and p.port in (None, 443)
	except (TypeError, ValueError):
		return False


def _location_error(value):
	try:
		if urlsplit(value).hostname in ("login.live.com", "login.microsoftonline.com", "account.microsoft.com", "account.live.com"):
			return "signed_out"
	except (TypeError, ValueError):
		pass
	return "page_unknown"


def _link(value):
	if not isinstance(value, str) or not value or len(value) > 4096 or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
		return None
	try:
		p = urlsplit(value)
		if p.scheme == "https" and p.hostname in ("rewards.bing.com", "www.bing.com", "bing.com") and not p.username and not p.password and p.port in (None, 443):
			return value
	except ValueError:
		pass
	return None


_PAGE_CHECK = r"""
const norm = value => String(value || '').replace(/\s+/g,' ').trim().toLowerCase();
const controls = document.querySelectorAll('h1,h2,h3,input[type="password"],button,a,[role="button"],iframe');
if (controls.length > 400) return {error:'page_unknown',reason:'too_many_controls'};
for (const n of controls) {
  const tag = n.tagName.toLowerCase();
  const label = norm(n.getAttribute('aria-label') || n.textContent);
  if (tag === 'input' && n.getAttribute('type') === 'password') return {error:'signed_out'};
  if (['sign in','sign up','join now','anmelden','sign in to microsoft rewards','sign in to your microsoft account','join microsoft rewards','sign up for microsoft rewards','log in'].includes(label)) return {error:'signed_out'};
  if (['verify you are human','verify your identity','security check','help us protect your account',"verify it's you"].includes(label)) return {error:'challenge'};
  if (tag === 'iframe' && /captcha|challenge|arkoselabs|verify/i.test((n.getAttribute('src') || '')+' '+(n.getAttribute('title') || ''))) return {error:'challenge'};
}
const root = document.querySelector('#main') || document.querySelector('main') || document.querySelector('[role="main"]');
if (!root) return {error:'page_unknown',reason:'no_main'};
if (document.readyState !== 'complete') return {error:'page_unknown',reason:'not_complete'};
const buttons = [...root.querySelectorAll('button,[role="button"]')].filter(n => ['copy link','link kopieren'].includes(norm(n.getAttribute('aria-label') || n.textContent)));
if (buttons.length !== 1) return {error:buttons.length ? 'link_ambiguous' : 'page_unknown',reason:buttons.length ? 'copy_button_ambiguous' : 'no_copy_button'};
"""
_READY = _PAGE_CHECK + "return {ready:true};"
_SNAPSHOT = _PAGE_CHECK + r"""
const [copy] = buttons;
let parent = copy.parentElement;
for (let depth=0; parent && depth<6 && root.contains(parent); depth++, parent=parent.parentElement) {
  const inputs = parent.querySelectorAll('input[type="text"],input[type="url"],input:not([type])');
  if (inputs.length > 1) return {error:'link_ambiguous',reason:'input_ambiguous'};
  if (inputs.length === 1) {
    for (const input of inputs) return {link:String(input.value || '').trim()};
  }
  if (parent === root) break;
}
return {error:'link_missing',reason:'input_missing'};
"""


@contextmanager
def _private_transport():
	# WebDriver and HTTP debug responses include returned input values.
	loggers = [logging.getLogger(name) for name in ("selenium.webdriver.remote.remote_connection", "urllib3.connectionpool")]
	previous = [logger.disabled for logger in loggers]
	try:
		for logger in loggers:
			logger.disabled = True
		yield
	finally:
		for logger, disabled in zip(loggers, previous):
			logger.disabled = disabled


def read_referral_page(driver) -> ReferralState:
	"""One navigation, no clicks; figures and friends have no verified selectors."""
	with _private_transport():
		return _read_page(driver)


@contextmanager
def _command_timeout(driver, seconds):
	# Bound HTTP commands as well as the readiness loop. Disable retries so a
	# transport timeout cannot multiply the budget; restore the caller's setup.
	connection = getattr(driver, "command_executor", None)
	config = getattr(connection, "_client_config", None)
	if config is None:
		yield
		return
	from urllib3.util import Timeout
	previous = config.timeout
	pool = getattr(connection, "_conn", None)
	request = getattr(pool, "request", None)
	def bounded_request(*args, **kwargs):
		kwargs["retries"] = False
		return request(*args, **kwargs)
	try:
		config.timeout = Timeout(total=seconds, connect=min(2, seconds), read=seconds)
		if callable(request): pool.request = bounded_request
		yield
	finally:
		config.timeout = previous
		if callable(request): pool.request = request


def _page_failure(raw):
	if not isinstance(raw, dict):
		return ReferralState(unreadable="page_unknown", reason="invalid_snapshot")
	code, reason = raw.get("error"), raw.get("reason")
	return ReferralState(unreadable=code if isinstance(code, str) and code in UNREADABLE_CODES else "page_unknown",
		reason=reason if isinstance(reason, str) and reason in REASON_CODES else None)


def _wait_ready(driver):
	deadline = time.monotonic() + READY_TIMEOUT
	failure = ReferralState(unreadable="page_unknown", reason="not_complete")
	# At most 61 probes including the initial one; no unbounded fake-clock loop.
	for attempt in range(int(READY_TIMEOUT / POLL_INTERVAL) + 1):
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			return failure
		with _command_timeout(driver, min(2, remaining)):
			raw = driver.execute_script(_READY)
		if isinstance(raw, dict) and raw.get("ready") is True and not raw.get("error"):
			return None if time.monotonic() <= deadline else failure
		failure = _page_failure(raw)
		if failure.unreadable in ("signed_out", "challenge") or failure.reason in ("too_many_controls", "invalid_snapshot"):
			return failure
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			return failure
		time.sleep(min(POLL_INTERVAL, remaining))
	return failure


def _read_page(driver):
	try:
		with _command_timeout(driver, 65):
			driver.get(TARGET)
		with _command_timeout(driver, 2):
			location = driver.current_url
		if not _allowed_page(location):
			return ReferralState(unreadable=_location_error(location), reason="location")
		failure = _wait_ready(driver)
		if failure is not None:
			return failure
		with _command_timeout(driver, 8):
			raw = driver.execute_script(_SNAPSHOT)
		with _command_timeout(driver, 2):
			location = driver.current_url
		if not _allowed_page(location):
			return ReferralState(unreadable=_location_error(location), reason="location")
		if not isinstance(raw, dict):
			return _page_failure(raw)
		if raw.get("error"):
			return _page_failure(raw)
		value = raw.get("link")
		link = _link(value)
		if link is None:
			return ReferralState(unreadable="link_missing" if not value else "link_invalid", reason="input_missing" if not value else "link_invalid")
		return ReferralState(link=link, link_found=True)
	except Exception:
		# WebDriver messages can contain the full private URL or returned markup.
		return ReferralState(unreadable="read_failed", reason="read_failed")


def _home(directory=None):
	return Path(directory if directory is not None else os.environ.get("REWARDS_FARMER_DATA_HOME") or constants.DATA_HOME)


def _stored(directory):
	try:
		value = json.loads(read_file(_home(directory) / "referral.json"))
	except FileNotFoundError:
		return {"v": 1, "accounts": {}}
	if not isinstance(value, dict) or value.get("v") != 1 or not isinstance(value.get("accounts"), dict):
		raise ValueError("storage_unreadable")
	return value


def save_referral_state(account, state, *, read_at, run_id, directory=None):
	value = _stored(directory)
	previous = _snapshot(value["accounts"].get(account))
	if _time(read_at) is None:
		raise ValueError("storage_unreadable")
	link = _link(state.link) if state.link_found is True and state.unreadable is None else None
	unreadable = _unreadable(state.unreadable) if state.unreadable is not None else None if link else "link_invalid" if state.link else "link_missing"
	value["accounts"][account] = {"link": link or previous.get("link"), "link_found": bool(link or previous.get("link")),
		"link_read_at": read_at if link else previous.get("link_read_at"), "last_attempt_at": read_at,
		"referrals": state.referrals, "friends": state.friends, "unreadable": unreadable, "run_id": run_id}
	path = _home(directory) / "referral.json"
	path.parent.mkdir(parents=True, exist_ok=True)
	name = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".referral-", delete=False) as stream:
			name = stream.name
			json.dump(value, stream, ensure_ascii=False)
			stream.write("\n")
			stream.flush()
			os.fsync(stream.fileno())
		replace_file(name, path)
	finally:
		if name is not None:
			Path(name).unlink(missing_ok=True)


def _time(value):
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
		return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
	except (ValueError, TypeError, AttributeError):
		return None


def _unreadable(value):
	return value if isinstance(value, str) and value in UNREADABLE_CODES else "storage_unreadable" if value is not None else None


def _snapshot(value):
	if value is None:
		return {}
	if not isinstance(value, dict) or _time(value.get("last_attempt_at", value.get("read_at"))) is None:
		raise ValueError("storage_unreadable")
	stamp = value.get("last_attempt_at", value.get("read_at"))
	link = _link(value.get("link")) if value.get("link_found") is True else None
	# Older snapshots only verified their link when that single read succeeded.
	link_at = value.get("link_read_at") if "link_read_at" in value else value.get("read_at") if value.get("unreadable") is None else None
	if _time(link_at) is None or _time(link_at) > _time(stamp):
		link, link_at = None, None
	return {**value, "link": link, "link_read_at": link_at if link else None, "last_attempt_at": stamp}


def get_referral_info(account, *, directory=None):
	result = {"link": None, "link_found": False, "referrals": None, "friends": None, "read_at": None, "unreadable": "not_read"}
	if account is None:
		return result
	try:
		stored = _snapshot(_stored(directory)["accounts"].get(account))
	except (OSError, ValueError):
		stored = {}
		result["unreadable"] = "storage_unreadable"
	latest = None
	for identity in run_record.list_run_ids(str(_home(directory) / "runs")):
		for event in run_record.read_events(str(_home(directory) / "runs" / (identity + ".jsonl"))):
			if event.get("type") != "referral_state" or event.get("account_id", "@root" if event.get("account") == "default" else event.get("account")) != account or event.get("v", 1) != 1:
				continue
			stamp = _time(event.get("read_at") or event.get("ts"))
			if stamp is not None and (latest is None or stamp >= latest[0]):
				latest = stamp, event
	data = stored
	matched = True
	if latest is not None and (not stored or latest[0] >= _time(stored["last_attempt_at"])):
		event = latest[1]
		matched = bool(stored) and stored.get("run_id") == event.get("run") and _time(stored["last_attempt_at"]) == latest[0]
		data = {**event, "last_attempt_at": event.get("read_at") or event.get("ts")}
	if not data:
		return result
	# The link is an independently dated good observation, not a field of the
	# latest event. A later failed attempt must not invalidate that observation.
	result.update(link=stored.get("link"), link_found=bool(stored.get("link")),
		referrals=data.get("referrals") if type(data.get("referrals")) is int and data["referrals"] >= 0 else None,
		read_at=stored.get("link_read_at") if stored.get("link") else data.get("last_attempt_at"), unreadable=_unreadable(data.get("unreadable")))
	if matched and stored and result["unreadable"] is None:
		result["friends"] = stored.get("friends") if isinstance(stored.get("friends"), list) else None
	if data.get("link_found") is True and (not matched or result["link"] is None) and result["unreadable"] is None:
		result["unreadable"] = "storage_unreadable"
	return result
