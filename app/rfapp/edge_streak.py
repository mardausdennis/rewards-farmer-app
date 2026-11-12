"""App policy for the optional Edge tile, using measured farmer events only."""
import copy
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager

from file_replace import read_file, replace_file
from rfapp import account_history, paths
from rfapp.envfile import EnvFile
import run_lock
import run_record
import stats_ledger

logger = logging.getLogger("rfapp")
GATE_SECONDS = 3590
TASK_SECONDS = 3600
MARGIN_SECONDS = 300
# Keep another two minutes for the stop acknowledgement, receipt/state writes,
# notification and job cleanup; those must not spend the five-minute margin.
HARD_STOP_SECONDS = min(GATE_SECONDS, TASK_SECONDS) - MARGIN_SECONDS - 120
STOP_SECONDS = HARD_STOP_SECONDS - 45
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def process_age():
	# The gate starts its timer before Python imports this module. Subtract
	# that startup too, using the owned process's Windows creation timestamp.
	created = run_lock._process_created(os.getpid())
	if os.name != "nt" or created is None: return None
	return max(0, time.time() - (created / 10000000 - 11644473600))


def _path():
	return paths.data_home() / "edge-streak.json"


def load():
	try:
		value = json.loads(read_file(_path()))
		if not isinstance(value, dict) or value.get("v") != 1 or not isinstance(value.get("accounts"), dict):
			raise ValueError("Unknown Edge state")
		if value.get("auto_off") is not None and not isinstance(value["auto_off"], dict):
			raise ValueError("Invalid Edge state")
		if any(not isinstance(row, dict) for row in value["accounts"].values()):
			raise ValueError("Invalid Edge account state")
		for row in value["accounts"].values():
			if not isinstance(row.get("counted_dates", []), list) or not isinstance(row.get("counted_run_ids", []), list):
				raise ValueError("Invalid Edge day history")
			if any(not isinstance(item, str) for item in row.get("counted_dates", []) + row.get("counted_run_ids", [])):
				raise ValueError("Invalid Edge history identity")
		return value
	except FileNotFoundError:
		return {"v":1, "accounts":{}, "auto_off":None}
	except (OSError, ValueError):
		logger.warning("Edge streak state unreadable; top-up disabled")
		return {"v":1, "accounts":{}, "auto_off":None, "unreadable":True}


def _save(value):
	path = _path()
	path.parent.mkdir(parents=True, exist_ok=True)
	name = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".edge-streak-", delete=False) as stream:
			name = stream.name
			json.dump(value, stream, sort_keys=True)
			stream.write("\n")
			stream.flush()
			os.fsync(stream.fileno())
		replace_file(name, path)
	finally:
		if name is not None: Path(name).unlink(missing_ok=True)


@contextmanager
def transaction():
	# The GUI and the short-lived supervisor can both change this policy.
	lock = run_lock.acquire("edge_streak", directory=str(paths.data_home() / ".edge-streak-state"))
	if lock is None: raise OSError("Edge streak state is in use")
	with lock: yield


def effective_mode(mode, state=None):
	if mode not in ("off", "remind", "fill"): return "off"
	if mode != "fill": return mode
	state = load() if state is None else state
	return "remind" if mode == "fill" and (state.get("auto_off") or state.get("unreadable")) else mode


def reset():
	"""Caller holds transaction; an explicit Fill selection starts a new attempt."""
	state = load()
	if state.get("unreadable"): raise OSError("Edge streak state needs recovery")
	if _path().is_file():
		state["auto_off"] = None
		for row in state["accounts"].values():
			row.update(counted_dates=[], counted_run_ids=[], day_invalid=True)
		_save(state)


def _at(value):
	try:
		at = datetime.fromisoformat(value.replace("Z", "+00:00"))
		return at if at.tzinfo is not None else None
	except (AttributeError, TypeError, ValueError): return None


def last_slot(values, at):
	"""Bind eligibility to a nominal trigger, never to a late catch-up run."""
	from rfapp import schedule
	try:
		mode = values.get("schedule.mode")
		start = datetime.strptime(values["schedule.window_start"], "%H:%M").time()
		end = datetime.strptime(values["schedule.window_end"], "%H:%M").time()
		s = schedule.ScheduleSettings(mode, start, end, values["schedule.checks"],
			datetime.strptime(values["schedule.time"], "%H:%M").time(), frozenset(values["schedule.days"]),
			values["schedule.random_delay"], False, True, 1, 1)
		clocks = schedule.trigger_times(s)
		if not clocks or clocks[-1] != end: return None
		for date in (at.date(), at.date() - timedelta(days=1)):
			if _DAYS[date.weekday()] not in s.days: continue
			nominal = datetime.combine(date, end, tzinfo=at.tzinfo)
			if 0 <= (at - nominal).total_seconds() < s.random_delay * 60 + 60:
				return nominal
	except (KeyError, TypeError, ValueError): pass
	return None


def _day_run_ids(at, *, system_local=False):
	"""Use the UTC run-id timestamp to stop before opening an older file."""
	if at.tzinfo is None: at = at.astimezone()
	midnight = datetime.combine(at.date(), datetime.min.time())
	# A local DST transition can change the offset between midnight and now.
	start = midnight.astimezone(timezone.utc) if system_local else midnight.replace(tzinfo=at.tzinfo).astimezone(timezone.utc)
	lower = start.strftime("%Y%m%dT%H%M%SZ")
	upper = at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	for run_id in run_record.list_run_ids(str(paths.runs_dir())):
		stamp = run_id[:16]
		if stamp < lower: break
		if stamp <= upper: yield run_id


def open_accounts(account_ids, slot, *, user_data_dir):
	"""A Fill choice is not evidence that any profile has an Edge tile."""
	if slot.tzinfo is None: slot = slot.astimezone()
	selected = set(account_ids)
	if not selected: return set()
	latest = {}
	rules = account_history.aliases()
	try:
		for run_id in _day_run_ids(slot, system_local=slot.utcoffset() == slot.astimezone().utcoffset()):
			events = account_history.project_events(run_record.read_events(str(paths.runs_dir() / (run_id + ".jsonl"))), str(user_data_dir), rules)
			if any(event.get("v") != run_record.SCHEMA_VERSION for event in events): continue
			start = next((event for event in events if event.get("type") == "run_start"), {})
			started = _at(start.get("ts"))
			if started is None or started >= slot or started.astimezone(slot.tzinfo).date() != slot.date(): continue
			profiles = {event.get("account"):event.get("profile") for event in events if event.get("type") == "account_start"}
			for event in events:
				if event.get("type") != "state": continue
				account = stats_ledger.account_id(event.get("account"), profiles.get(event.get("account")), str(user_data_dir))
				at = _at(event.get("ts"))
				if account not in selected or at is None or at >= slot or at.astimezone(slot.tzinfo).date() != slot.date(): continue
				if account not in latest or at >= latest[account][0]: latest[account] = (at, event)
	except (OSError, ValueError):
		logger.warning("Edge tile history unavailable; top-up disabled")
		return set()
	return {account for account, (_, event) in latest.items() if event.get("edge_tile") is True
		and _minutes(event.get("edge_minutes")) is not None and _known_target(event.get("edge_target")) is not None
		and event["edge_minutes"] < event["edge_target"]}


def plan(values, kind, at, *, elapsed_seconds, accounts, account_ids=(), user_data_dir=None):
	slot = last_slot(values, at) if kind == "scheduled" else None
	remaining = max(0, HARD_STOP_SECONDS - max(0, elapsed_seconds))
	# Divide a single global allowance across the profiles already selected by
	# this run. Prior task time is enforced again by the supervisor stop bound.
	maximum = min(35, math.floor(remaining / max(1, accounts) / 60 * 1000) / 1000)
	enabled = slot is not None and effective_mode(values.get("run.edge_streak", "off")) == "fill" and maximum >= 1
	if enabled:
		known = open_accounts(account_ids, slot, user_data_dir=user_data_dir)
		enabled = bool(account_ids) and set(account_ids) <= known
	env = {"EDGE_BROWSING_STREAK":str(enabled).lower(), "EDGE_BROWSING_STREAK_MAX_MINUTES":format(maximum if enabled else 0, ".8g")}
	if enabled: env["REWARDS_HEADLESS"] = "true"
	return {"enabled":enabled, "slot":slot.isoformat() if slot else None, "maximum_minutes":maximum if enabled else 0,
		"remaining_seconds":remaining, "env":env}


def _number(value):
	return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _minutes(value):
	return value if type(value) is int and value >= 0 else None


def _known_target(value):
	return value if type(value) is int and value > 0 else None


def process_run(run_id, events, *, user_data_dir):
	"""Consume only complete run receipts. A stop cannot prove a full attempt."""
	start = next((e for e in events if e.get("type") == "run_start"), {})
	end = next((e for e in reversed(events) if e.get("type") == "run_end"), {})
	policy = next((e for e in events if e.get("type") == "edge_streak_plan"), {})
	slot = _at(policy.get("slot"))
	if (not end or slot is None or start.get("trigger") != "scheduled" or policy.get("enabled") is not True
		or start.get("plan", {}).get("edge_topup") is not True or any(e.get("v") != run_record.SCHEMA_VERSION for e in events)):
		return load()
	observations = [e for e in events if e.get("type") == "edge_browsing"]
	profiles = {e.get("account"):e.get("profile") for e in events if e.get("type") == "account_start"}
	rules = account_history.aliases()
	with transaction():
		state = load()
		if state.get("unreadable"): return state
		previous = copy.deepcopy(state)
		for event in observations:
			at = _at(event.get("ts"))
			if at is None or not isinstance(event.get("account"), str): continue
			account = account_history.resolve(stats_ledger.account_id(event["account"], profiles.get(event["account"]), user_data_dir), start.get("ts"), rules)
			row = state["accounts"].get(account, {})
			last_at = _at(row.get("at"))
			if row.get("run_id") == run_id or last_at is not None and at < last_at: continue
			# The day belongs to the nominal local schedule, even if the final
			# bounded read returns after midnight. Never pair different days.
			day = slot.date()
			before, after, target = _minutes(event.get("minutes_before")), _minutes(event.get("minutes_after")), _known_target(event.get("minutes_target"))
			elapsed, maximum = event.get("elapsed_seconds"), event.get("maximum_minutes")
			account_end = next((e for e in reversed(events) if e.get("type") == "account_end" and e.get("account") == event["account"]), {})
			valid = (end.get("result") in ("ok", "partial") and end.get("exit_code") == 0 and bool(account_end) and not account_end.get("error_code")
				and not any(e.get("type") == "stop_requested" for e in events) and event.get("final_read_success") is True
				and before is not None and after is not None and target is not None and 0 <= before <= after <= target
				and _number(elapsed) and _number(maximum) and 0 < maximum <= 35 and maximum == policy.get("maximum_minutes"))
			rose = (event.get("final_read_success") is True and before is not None and after is not None and target is not None
				and 0 <= before < after <= target)
			changed_target = row.get("target") is not None and target != row["target"]
			same_day = row.get("day") == day.isoformat()
			invalid = not valid or changed_target or same_day and row.get("day_invalid", False)
			rose = rose or same_day and row.get("day_rose", False)
			counted = (not invalid and not rose and event.get("outcome") == "incomplete" and event.get("maximum_reached") is True
				and before == after < target and elapsed >= maximum * 60)
			dates, ids = [], []
			if counted:
				prior = row.get("counted_dates", [])
				if prior and prior[-1] == (day - timedelta(days=1)).isoformat():
					dates, ids = prior[-1:], row.get("counted_run_ids", [])[-1:]
				elif same_day and prior and prior[-1] == day.isoformat():
					dates, ids = prior[:-1], row.get("counted_run_ids", [])[:-1]
				dates, ids = (dates + [day.isoformat()])[-2:], (ids + [run_id])[-2:]
			row = {**row, "at":event["ts"], "run_id":run_id, "day":day.isoformat(), "target":target,
				"day_rose":bool(rose), "day_invalid":bool(invalid), "counted_dates":dates, "counted_run_ids":ids}
			state["accounts"][account] = row
			if len(dates) == 2 and EnvFile.load(paths.env_path()).get("REWARDS_EDGE_STREAK") == "fill" and state.get("auto_off") is None:
				state["auto_off"] = {"account_id":account, "dates":dates, "run_ids":ids, "at":event["ts"], "reason":"minutes_not_counted"}
		if state != previous: _save(state)
		# Persist the fail-closed record BEFORE editing the setting. A crash in
		# between still makes load/plan expose Remind rather than launch again.
		if state.get("auto_off"):
			editor = EnvFile.load(paths.env_path())
			if editor.get("REWARDS_EDGE_STREAK") == "fill":
				editor.set("REWARDS_EDGE_STREAK", "remind")
				editor.save()
		return state


def snapshot(values, account_ids, *, user_data_dir=None, now=None):
	from rfapp import settings
	now = (now or datetime.now()).astimezone()
	today = now.date()
	selected = list(dict.fromkeys(account_ids))
	state = load()
	rows = {account:{"account_id":account, "edge_tile":None, "edge_minutes":None, "edge_target":None, "at":None,
		"run_id":None, "last_scheduled_at":None, "last_scheduled_run_id":None} for account in selected}
	if not selected:
		return {"mode":effective_mode(values.get("run.edge_streak", "off"), state), "auto_off":state.get("auto_off"), "accounts":[]}
	rules = account_history.aliases()
	directory = str(user_data_dir or settings.user_data_dir())
	for run_id in _day_run_ids(now, system_local=True):
		events = account_history.project_events(run_record.read_events(str(paths.runs_dir() / (run_id + ".jsonl"))), directory, rules)
		if any(e.get("v") != run_record.SCHEMA_VERSION for e in events): continue
		start = next((e for e in events if e.get("type") == "run_start"), {})
		end = next((e for e in reversed(events) if e.get("type") == "run_end"), {})
		policy = next((e for e in events if e.get("type") == "edge_streak_plan"), {})
		finished = _at(end.get("ts"))
		slot = _at(policy.get("slot"))
		profiles = {e.get("account"):e.get("profile") for e in events if e.get("type") == "account_start"}
		for event in events:
			account = stats_ledger.account_id(event.get("account"), profiles.get(event.get("account")), directory)
			if account not in rows: continue
			row, at = rows[account], _at(event.get("ts"))
			if (event.get("type") == "state" and "edge_tile" in event and at is not None and at <= now
				and at.astimezone().date() == today and (_at(row["at"]) is None or at >= _at(row["at"]))):
				tile = event.get("edge_tile") if type(event.get("edge_tile")) is bool else None
				minutes, target = _minutes(event.get("edge_minutes")), _known_target(event.get("edge_target"))
				row.update(edge_tile=tile, edge_minutes=minutes if tile else None, edge_target=target if tile else None, at=event["ts"], run_id=run_id)
			if (event.get("type") == "account_end" and not event.get("error_code") and start.get("trigger") == "scheduled"
				and slot is not None and slot.astimezone().date() == today
				and end.get("result") in ("ok", "partial", "skipped", "checked") and finished is not None
				and finished <= now and finished.astimezone().date() == today
				and not any(e.get("type") == "stop_requested" for e in events)
				and (_at(row["last_scheduled_at"]) is None or finished >= _at(row["last_scheduled_at"]))):
				row.update(last_scheduled_at=end["ts"], last_scheduled_run_id=run_id)
	return {"mode":effective_mode(values.get("run.edge_streak", "off"), state), "auto_off":state.get("auto_off"), "accounts":list(rows.values())}
