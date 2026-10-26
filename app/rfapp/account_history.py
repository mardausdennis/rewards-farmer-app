"""Dated account projections; stored records and ledger rows stay unchanged."""

import copy
from datetime import datetime, timezone
import re

import stats_ledger

IDENTITY_ERRORS = frozenset(("identity_mismatch", "identity_unknown"))


def _utc(value):
	try:
		at = datetime.fromisoformat(value.replace("Z", "+00:00"))
		return at.astimezone(timezone.utc) if at.tzinfo is not None else None
	except (AttributeError, ValueError, TypeError):
		return None


def aliases(values=None):
	if values is None:
		from rfapp import prefs
		values = prefs.load().get("history_aliases", [])
	if not isinstance(values, list):
		return ()
	result = set()
	for value in values:
		if not isinstance(value, dict) or value.get("from") != "@root":
			continue
		target, before = value.get("to"), _utc(value.get("before"))
		if isinstance(target, str) and re.fullmatch(r"account-[1-9][0-9]*", target) and before is not None:
			result.add((before, target))
	return tuple(sorted(result))


def resolve(account_id, started_at, rules):
	if account_id != "@root":
		return account_id
	at = _utc(started_at)
	if at is None:
		return "@root"
	for before in sorted({before for before, _ in rules}):
		if at < before:
			targets = {target for bound, target in rules if bound == before}
			return next(iter(targets)) if len(targets) == 1 else "@root"
	return "@root"


def started_at(events):
	return next((event.get("ts") for event in events if event.get("type") == "run_start"),
		next((event.get("ts") for event in events if event.get("type") == "account_start"), None))


def _run_started(run_id):
	try:
		return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
	except (TypeError, ValueError):
		return None


def project_events(events, user_data_dir, rules):
	if not rules:
		return events
	start = started_at(events)
	profiles = {event.get("account"): event.get("profile") for event in events if event.get("type") == "account_start"}
	names = {event.get("account") for event in events if isinstance(event.get("account"), str)}
	names.update(name for event in events if event.get("type") == "run_start" for name in event.get("accounts", []) if isinstance(name, str))
	mapping = {}
	for name in names:
		account = stats_ledger.account_id(name, profiles.get(name), user_data_dir)
		target = resolve(account, start, rules)
		if target != account:
			mapping[name] = target
	if not mapping:
		return events
	result = []
	for event in events:
		value = dict(event)
		if event.get("type") == "run_start":
			value["accounts"] = [mapping.get(name, name) for name in event.get("accounts", [])]
		if event.get("account") in mapping:
			value["account"] = mapping[event["account"]]
			if event.get("type") == "account_start":
				# A view-only canonical name must not be reclassified by its old path.
				value.pop("profile", None)
		result.append(value)
	return result


def identity_skipped_before_tasks(events):
	end = next((event for event in reversed(events) if event.get("type") == "account_end"), {})
	return (end.get("error_code") in IDENTITY_ERRORS and end.get("ran_tasks") is False
		and not any(event.get("type") in ("task_start", "task_end")
			or event.get("type") == "account_end" and event.get("ran_tasks") is True for event in events))


def outcome(events, fallback, *, active=False, check_only=False):
	end = next((event for event in reversed(events) if event.get("type") == "account_end"), None)
	decision = next((event for event in reversed(events) if event.get("type") == "decision"), {})
	if end and end.get("error_code") in IDENTITY_ERRORS:
		return "skipped"
	if any(event.get("type") == "browser_failed" for event in events) or end and end.get("error_code"):
		return "failed"
	if decision.get("should_run") is False:
		return "skipped"
	if end is None:
		return None if active else fallback if fallback in ("stopped", "interrupted", "crashed") else "unknown"
	started = {event.get("task") for event in events if event.get("type") == "task_start"}
	finished = {event.get("task") for event in events if event.get("type") == "task_end"}
	if started - finished:
		return fallback if fallback in ("stopped", "interrupted", "crashed") else None if active else "interrupted"
	if any(event.get("type") == "task_end" and event.get("outcome") == "fail" for event in events):
		return "partial"
	if check_only:
		return "checked"
	if end.get("ran_tasks") or any(event.get("type") in ("task_start", "task_end") and event.get("outcome") != "off" for event in events):
		return "ok"
	return fallback


def summary(run_id, events, *, name=None, active=False):
	value = stats_ledger.run_summary(run_id, events, account=name, active=active)
	start = next((event for event in events if event.get("type") == "run_start"), {})
	names = {event.get("account") for event in events if isinstance(event.get("account"), str)} | set(start.get("accounts", []))
	if name is None:
		accounts = [[event for event in events if event.get("account") == account] for account in names]
		if value["result"] != "unknown" and any(identity_skipped_before_tasks(own) for own in accounts):
			points = [0 if identity_skipped_before_tasks(own) else stats_ledger.run_summary(run_id, events, account=account)["points_earned"]
				for account, own in zip(names, accounts)]
			value["points_earned"] = sum(points) if all(point is not None for point in points) else None
		return value
	own = [event for event in events if event.get("account") == name]
	if value["result"] != "unknown" and identity_skipped_before_tasks(own):
		# Denied before earning actions proves zero for this run, not this day.
		value["points_earned"] = 0
	if len(names) > 1 or any(event.get("error_code") in IDENTITY_ERRORS for event in own):
		value["result"] = outcome(own, value["result"], active=active, check_only=start.get("plan", {}).get("check_only", False))
		value["result_key"] = stats_ledger.result_key(value["result"], own)
		if value["result"] == "skipped":
			value["kind"] = "check"
		terminal = {"type": "run_end", "result": value["result"]}
		value["problems"] = stats_ledger.run_summary(run_id, own + [terminal], account=name)["problems"]
	return value


def account_summaries(rows, folder, user_data_dir):
	"""Correct mixed-run display outcomes without changing the compacted source."""
	read = {}
	outcomes = {}
	members = {}
	for row in rows.values():
		for item in row.get("runs") or []:
			members.setdefault(item["run_id"], set()).add(row["account_id"])
	for row in rows.values():
		for index, item in enumerate(row.get("runs") or []):
			mixed = len(members[item["run_id"]]) > 1
			if not mixed and item["points_earned"] is not None:
				continue
			run_id = item["run_id"]
			if run_id not in read:
				try:
					read[run_id] = stats_ledger._read_record(folder / (run_id + ".jsonl"))
				except (OSError, ValueError):
					read[run_id] = []
			events = read[run_id]
			profiles = {event.get("account"): event.get("profile") for event in events if event.get("type") == "account_start"}
			names = {event.get("account") for event in events if isinstance(event.get("account"), str)}
			names.update(name for event in events if event.get("type") == "run_start" for name in event.get("accounts", []) if isinstance(name, str))
			for name in names:
				if stats_ledger.account_id(name, profiles.get(name), user_data_dir) != row["account_id"]:
					continue
				if not mixed and not identity_skipped_before_tasks([event for event in events if event.get("account") == name]):
					continue
				value = summary(run_id, events, name=name)
				row["runs"][index] = value
				outcomes[(row["account_id"], run_id)] = value["result"]
				break
			if not events and item["result"] == "partial":
				value = dict(item)
				if item["problems"]:
					value["result"] = "partial" if any(problem["task"] is not None for problem in item["problems"]) else "unknown"
				elif item["kind"] == "check":
					value["result"] = "skipped"
				elif item["points_earned"] is not None:
					value["result"] = "ok"
				else:
					value["result"] = "unknown"
				value["result_key"] = stats_ledger.result_key(value["result"], [])
				row["runs"][index] = value
	for row in rows.values():
		for run_id in list(row["_runs"]):
			if outcomes.get((row["account_id"], run_id)) == "skipped":
				row["_runs"].remove(run_id)
				row["_checks"] = sorted(set(row["_checks"]) | {run_id})
		row["run_count"], row["check_count"] = len(row["_runs"]), len(row["_checks"])
	return rows


def project_rows(rows, rules):
	"""Whole dated rows map exactly; an unsplittable boundary stays unknown."""
	result = {}
	unknown = set()
	for source in rows:
		row = copy.deepcopy(source)
		account = row["account_id"]
		first = resolve(account, row.get("first_at"), rules)
		last = resolve(account, row.get("last_at"), rules)
		summaries = {item["run_id"]: item for item in row.get("runs") or []}
		starts = {run_id: summaries.get(run_id, {}).get("started_at") or _run_started(run_id) for run_id in row["run_ids"]}
		if starts and all(starts.values()):
			targets = {resolve(account, stamp, rules) for stamp in starts.values()}
			if len(targets) == 1:
				first = last = targets.pop()
		if first == last:
			row["account_id"] = first
			parts = [row]
		else:
			# A compacted day may span the move and have no raw records left.
			# Its money cannot be divided safely, so neither account gets a guess.
			groups = {}
			for item in row.get("runs") or []:
				target = resolve(account, item.get("started_at"), rules)
				groups.setdefault(target, []).append(item)
			for target in (first, last):
				groups.setdefault(target, [])
			parts = []
			for target, summaries in groups.items():
				unknown.add((row["date"], target))
				part = stats_ledger._row(row["date"], target, row["_day_start"], row["_day_end"])
				part.update(first_at=row["first_at"], last_at=row["last_at"], runs=summaries,
					run_ids=[item["run_id"] for item in summaries],
					_runs=[item["run_id"] for item in summaries if item["kind"] == "run"],
					_checks=[item["run_id"] for item in summaries if item["kind"] == "check"])
				parts.append(stats_ledger._finalize(part))
		for part in parts:
			key = (part["date"], part["account_id"])
			result[key] = stats_ledger._merge(result[key], part) if key in result else part
	for key in unknown:
		row = result[key]
		for field in ("earned", "earned_basis", "balance", "balance_at", "done", "done_at", "daily_set_open", "searches_earned", "searches_max",
			"batch_queries_sent", "open_points", "quests_open", "_state_at", "_page_earned", "_run_earned"):
			row[field] = None
		row.update(sources=dict.fromkeys(stats_ledger.SOURCES), parts=[], source_complete=False,
			counted_sources=dict.fromkeys(stats_ledger.TASKS, 0), _counted_runs=[], query_count_complete=False)
	return result


def rows_for_view(rows, folder, user_data_dir, rules, now):
	rows = account_summaries(project_rows(rows, ()), folder, user_data_dir)
	# A boundary day's raw records can separate old and new root observations.
	# Prefer that exact split; compacted ambiguous totals stay unknown.
	read = {}
	expanded = []
	for row in rows.values():
		if row["account_id"] == "@root" and resolve("@root", row.get("first_at"), rules) != resolve("@root", row.get("last_at"), rules):
			complete = True
			for run_id in row["run_ids"]:
				if run_id not in read:
					try:
						read[run_id] = stats_ledger._read_record(folder / (run_id + ".jsonl"))
					except (OSError, ValueError):
						read[run_id] = []
				complete &= bool(read[run_id])
			if complete and row["run_ids"]:
				records = {}
				for run_id in row["run_ids"]:
					events = read[run_id]
					profiles = {event.get("account"): event.get("profile") for event in events if event.get("type") == "account_start"}
					names = {name for name, profile in profiles.items() if stats_ledger.account_id(name, profile, user_data_dir) == "@root"}
					own = [dict(event) for event in events if event.get("account") is None or event.get("account") in names]
					for event in own:
						if event.get("type") == "run_start":
							event["accounts"] = [name for name in event.get("accounts", []) if name in names]
					records[run_id] = project_events(own, user_data_dir, rules)
				bounds = [(row["date"], stats_ledger._at(row["_day_start"]), stats_ledger._at(row["_day_end"]))]
				parts = stats_ledger.reduce_records(records, user_data_dir=user_data_dir, now=now, bounds=bounds)
				expanded.extend(value for value in parts.values() if value["date"] == row["date"])
				continue
		expanded.append(row)
	return project_rows(expanded, rules)
