import _bootstrap  # noqa: F401

from contextlib import ExitStack
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import constants
import rfapp
from rfapp import account_history, paths, prefs, runs, stats, streaks
import run_record
import stats_ledger

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
CUTOFF = "2026-10-25T10:00:00Z"
ALIASES = [{"from": "@root", "to": "account-1", "before": CUTOFF}]


class AccountViews(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		self.folder = self.home / "runs"
		self.folder.mkdir()
		self.user_dir = str(self.home / "profiles")
		self.active, self.paused = ["account-1"], []
		self.values = {"history_aliases": [], "account_labels": {}, "goals": {}}
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		settings = types.SimpleNamespace(load=lambda: {}, active_accounts=lambda: list(self.active), paused_accounts=lambda: list(self.paused), user_data_dir=lambda: Path(self.user_dir))
		translator = types.SimpleNamespace(current=lambda: types.SimpleNamespace(t=lambda key, **kw: "Account " + str(kw.get("number", 1))))
		self.stack.enter_context(mock.patch.dict(sys.modules, {"rfapp.settings": settings, "rfapp.i18n": translator}))
		self.stack.enter_context(mock.patch.object(rfapp, "settings", settings, create=True))
		self.stack.enter_context(mock.patch.object(rfapp, "i18n", translator, create=True))
		self.stack.enter_context(mock.patch.object(prefs, "load", side_effect=lambda: copy.deepcopy(self.values)))
		self.stack.enter_context(mock.patch.object(paths, "data_home", return_value=self.home))
		self.stack.enter_context(mock.patch.object(paths, "runs_dir", return_value=self.folder))
		self.stack.enter_context(mock.patch.object(constants, "RUNS_DIR", str(self.folder)))
		self.stack.enter_context(mock.patch.object(runs, "active_run_id", return_value=None))
		self.stack.enter_context(mock.patch.object(runs.run_lock, "status", return_value=None))

	def record(self, stamp, accounts, *, root=False, suffix="0001", result="ok"):
		at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
		run_id = at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + suffix
		events = []
		def emit(kind, **fields):
			events.append({"v": 1, "run": run_id, "ts": (at + timedelta(milliseconds=len(events))).isoformat(), "type": kind, **fields})
		emit("run_start", accounts=list(accounts), trigger="app", plan={})
		for name, gain in accounts.items():
			emit("account_start", account=name, profile=self.user_dir if root else str(Path(self.user_dir, name)))
			if isinstance(gain, str):
				emit("account_end", account=name, started=True, ran_tasks=False, error_code=gain, duration_ms=3)
				continue
			state = {"balance": 100, "earned_today": 0, "daily_set_open": 0, "cards_open": 0, "quests_open": 0,
				"searches_earned": 0, "searches_max": 60, "open_points": 60, "unreadable": None}
			emit("state", account=name, phase="before", **state)
			emit("task_start", account=name, task="searches", points_before=100)
			emit("task_end", account=name, task="searches", outcome="ok", points_before=100, points_after=100 + gain, progress="sent 1 search", duration_ms=2)
			emit("state", account=name, phase="after", **{**state, "balance": 100 + gain, "earned_today": gain, "searches_earned": 60, "open_points": 0})
			emit("streak_capture", account=name, status="ok", phase="after", sections=[{"id": "streaks", "text": f"Daily Set Streak Day {gain} of 7 streak completed. Activity: 3/3", "nodes": []}])
			emit("account_end", account=name, started=True, ran_tasks=True, duration_ms=8)
		emit("run_end", result=result, accounts_ran=sum(type(value) is int for value in accounts.values()), duration_ms=20)
		path = self.folder / (run_id + ".jsonl")
		path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
		return run_id, events

	def get(self, account=None):
		return stats.get_stats({"account_id": account} if account is not None else {}, settings={}, prefs=self.values, now=NOW)

	def test_alias_cutoff_strict_utc_idempotent_and_profile_bound(self):
		self.values["history_aliases"] = ALIASES
		before, events = self.record("2026-10-25T09:59:59Z", {"default": 5}, root=True)
		boundary, _ = self.record(CUTOFF, {"default": 2}, root=True)
		after, _ = self.record("2026-10-25T10:00:01Z", {"default": 3}, root=True)
		named, _ = self.record("2026-10-25T09:00:00Z", {"default": 4})
		original = {path: path.read_bytes() for path in self.folder.iterdir()}
		self.assertEqual([item["run_id"] for item in runs.history(account_id="account-1", user_data_dir=self.user_dir)["runs"]], [before])
		self.assertEqual({item["run_id"] for item in runs.history(account_id="@root", user_data_dir=self.user_dir)["runs"]}, {boundary, after})
		self.assertEqual(runs.load_run(named, {}, self.user_dir)["accounts"][0]["account_id"], "default")
		rules = account_history.aliases(ALIASES + ALIASES)
		projected = account_history.project_events(events, self.user_dir, rules)
		self.assertEqual(projected, account_history.project_events(projected, self.user_dir, rules))
		self.assertEqual({path: path.read_bytes() for path in self.folder.iterdir()}, original)
		self.assertEqual(account_history.resolve("@root", "2026-10-25T11:59:59+02:00", rules), "account-1")

	def test_multiple_moves_never_reassign_earlier_root_history_to_new_person(self):
		rules = account_history.aliases(ALIASES + [{"from": "@root", "to": "account-2", "before": "2026-10-26T10:00:00Z"}])
		self.assertEqual(account_history.resolve("@root", "2026-10-24T12:00:00Z", rules), "account-1")
		self.assertEqual(account_history.resolve("@root", CUTOFF, rules), "account-2")
		self.assertEqual(account_history.resolve("account-1", CUTOFF, rules), "account-1")
		self.assertEqual(account_history.resolve("@root", "bad", rules), "@root")
		for values in (None, {}, [None], [{"from": "account-1", "to": "account-2", "before": CUTOFF}], [{"from": "@root", "to": "account-1", "before": "2026-10-25T10:00:00"}]):
			with self.subTest(values=values):
				self.assertFalse(account_history.aliases(values if values is not None else []))

	def test_compacted_alias_moves_stats_without_raw_or_ledger_rewrite(self):
		run_id, _ = self.record("2026-10-24T12:00:00Z", {"default": 5}, root=True)
		self.active = ["@root"]
		original = self.get("@root")
		(self.folder / (run_id + ".jsonl")).unlink()
		stats_ledger.fold(str(self.folder), data_home=self.home, user_data_dir=self.user_dir, now=NOW)
		ledger = self.home / "stats-ledger.json"
		stored = ledger.read_bytes()
		self.active = ["account-1"]
		self.values["history_aliases"] = ALIASES
		with mock.patch.object(stats_ledger, "_read_record", side_effect=AssertionError("compacted raw read")):
			first = self.get("account-1")
			second = self.get("account-1")
		self.assertEqual(first, second)
		self.assertEqual(first["totals"], original["totals"])
		self.assertEqual(first["streak"], original["streak"])
		self.assertEqual(first["days"], original["days"])
		self.assertEqual(ledger.read_bytes(), stored)

	def test_two_accounts_keep_own_history_points_and_streaks(self):
		self.active = ["account-1", "account-2"]
		run_id, _ = self.record("2026-10-26T10:00:00Z", {"account-1": 3, "account-2": 6})
		for name, gain in (("account-1", 3), ("account-2", 6)):
			with self.subTest(account=name):
				detail = runs.load_run(run_id, {}, self.user_dir, account_id=name)
				self.assertEqual([row["account_id"] for row in detail["accounts"]], [name])
				self.assertEqual((detail["points_earned"], detail["result"]), (gain, "ok"))
				data = self.get(name)
				self.assertEqual(data["days"][-1]["earned"], gain)
				self.assertEqual(data["totals"]["balance"], 100 + gain)
				self.assertEqual(data["streak"]["current"], 1)
				page = runs.history(account_id=name, user_data_dir=self.user_dir)["runs"]
				self.assertEqual(page[0]["points_earned"], gain)
				view = runs.overview({}, self.values, NOW, account_id=name)
				self.assertEqual(view["accounts"][0]["state"]["balance"], 100 + gain)
				self.assertEqual(view["streaks"]["items"][0]["day"], gain)
		self.assertEqual(self.get()["days"][-1]["earned"], 9)
		view = runs.overview({}, self.values, NOW)
		self.assertIsNone(view["streaks"])
		self.assertEqual(set(view["streaks_by_account"]), set(self.active))

	def test_bridge_uses_its_prefs_snapshot_for_all_account_readers(self):
		from rfapp.bridge import Bridge
		self.active = ["account-1", "account-2"]
		self.values["history_aliases"] = ALIASES
		run_id, _ = self.record("2026-10-25T09:00:00Z", {"default": 5}, root=True)
		api = Bridge(dependencies={
			"prefs": types.SimpleNamespace(load=lambda: copy.deepcopy(self.values)),
			"settings": types.SimpleNamespace(load=lambda: {}, user_data_dir=lambda: Path(self.user_dir)),
			"runs": runs, "stats": stats,
			"schedule": types.SimpleNamespace(status=lambda: {"mode": "manual"}),
			"accounts_admin": types.SimpleNamespace(),
		})
		self.addCleanup(api._close)
		# A global reload must not replace the bridge's migration snapshot.
		with mock.patch.object(prefs, "load", return_value={"history_aliases": []}):
			page = api.get_history({"account_id": "account-1"})
			self.assertTrue(page["ok"], page)
			self.assertEqual([row["run_id"] for row in page["data"]["runs"]], [run_id])
			detail = api.get_run({"run_id": run_id, "account_id": "account-1"})
			self.assertTrue(detail["ok"], detail)
			self.assertEqual(detail["data"]["accounts"][0]["account_id"], "account-1")
			self.assertEqual(detail["data"]["points_earned"], 5)
			view = api.get_overview({"account_id": "account-1"})
			self.assertTrue(view["ok"], view)
			self.assertEqual([row["account_id"] for row in view["data"]["accounts"]], ["account-1"])
			self.assertEqual(view["data"]["streaks"]["items"][0]["day"], 5)
			all_accounts = api.get_overview({})["data"]
			self.assertIsNone(all_accounts["streaks"])
			self.assertEqual(set(all_accounts["streaks_by_account"]), set(self.active))
			data = api.get_stats({"account_id": "account-1"})
			self.assertTrue(data["ok"], data)
			day = next(day for day in data["data"]["days"] if day["date"] == "2026-10-25")
			self.assertEqual(day["earned"], 5)

	def test_partial_identity_skip_has_zero_run_points_but_unknown_day_totals(self):
		self.active = ["account-1", "account-2"]
		for index, reason in enumerate(("identity_mismatch", "identity_unknown")):
			with self.subTest(reason=reason):
				run_id, _ = self.record(f"2026-10-26T{9 + index:02d}:00:00Z", {"account-1": 3, "account-2": reason}, result="partial")
				one = runs.load_run(run_id, {}, self.user_dir, account_id="account-1")
				two = runs.load_run(run_id, {}, self.user_dir, account_id="account-2")
				self.assertEqual((one["result"], one["points_earned"]), ("ok", 3))
				self.assertEqual((two["result"], two["accounts"][0]["status"]), ("skipped", "skipped"))
				self.assertEqual(two["accounts"][0]["error"]["code"], reason)
				self.assertEqual((two["points_earned"], two["accounts"][0]["points_earned"]), (0, 0))
				self.assertTrue(all(task["status"] == "not_run" for task in two["accounts"][0]["tasks"]))
				self.assertEqual(runs.load_run(run_id, {}, self.user_dir)["points_earned"], 3)
				self.assertEqual(runs.history(user_data_dir=self.user_dir)["runs"][0]["points_earned"], 3)
				data = self.get("account-2")
				item = next(item for day in data["days"] for item in day["runs"] if item["run_id"] == run_id)
				self.assertEqual((item["result"], item["kind"], item["points_earned"]), ("skipped", "check", 0))
				self.assertEqual(data["totals"]["run_count"], 0)
				self.assertIsNone(data["totals"]["balance"])
				aggregate = self.get()
				self.assertIsNone(aggregate["days"][-1]["earned"])
				self.assertEqual(next(item for item in aggregate["days"][-1]["runs"] if item["run_id"] == run_id)["points_earned"], 3)
				for field in ("earned", "week_earned", "month_earned", "balance"):
					self.assertIsNone(aggregate["totals"][field], field)
				self.assertIsNone(runs.overview({}, self.values, NOW)["totals"]["balance"])

	def test_identity_zero_needs_explicit_no_tasks_and_no_task_evidence(self):
		self.active = ["account-1", "account-2"]
		for index, variant in enumerate(("missing_flag", "ran_tasks", "task_start", "task_end", "other_error", "missing_end")):
			with self.subTest(variant=variant):
				run_id, events = self.record("2026-10-26T10:00:00Z", {"account-1": 40, "account-2": "identity_unknown"}, suffix=f"{index + 10:04x}", result="partial")
				end = next(event for event in events if event.get("account") == "account-2" and event["type"] == "account_end")
				if variant == "missing_flag":
					end.pop("ran_tasks")
				elif variant == "ran_tasks":
					end["ran_tasks"] = True
				elif variant in ("task_start", "task_end"):
					events.insert(-2, {**end, "type": variant, "task": "searches"})
				elif variant == "other_error":
					end["error_code"] = "other_failure"
				else:
					events.remove(end)
				(self.folder / (run_id + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
				self.assertIsNone(runs.load_run(run_id, {}, self.user_dir)["points_earned"])
				self.assertIsNone(account_history.summary(run_id, events, name="account-2")["points_earned"])

	def test_identity_zero_does_not_hide_another_account_without_terminal_evidence(self):
		self.active = ["account-1", "account-2", "account-3"]
		run_id, events = self.record("2026-10-26T10:00:00Z", {"account-1": 40, "account-2": "identity_mismatch", "account-3": 5}, result="partial")
		events = [event for event in events if event.get("account") != "account-3" or event["type"] in ("account_start", "state", "task_start") and event.get("phase") != "after"]
		(self.folder / (run_id + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
		detail = runs.load_run(run_id, {}, self.user_dir)
		self.assertEqual([row["points_earned"] for row in detail["accounts"]], [40, 0, None])
		self.assertIsNone(detail["points_earned"])
		self.assertIsNone(account_history.summary(run_id, events)["points_earned"])
		data = self.get()
		self.assertIsNone(next(item for item in data["days"][-1]["runs"] if item["run_id"] == run_id)["points_earned"])

	def test_single_identity_skip_is_zero_without_inventing_daily_measurements(self):
		for index, reason in enumerate(("identity_mismatch", "identity_unknown")):
			with self.subTest(reason=reason):
				run_id, _ = self.record("2026-10-26T10:00:00Z", {"account-1": reason}, suffix=f"{index + 20:04x}", result="partial")
				detail = runs.load_run(run_id, {}, self.user_dir)
				self.assertEqual(detail["points_earned"], 0)
				data = self.get("account-1")
				self.assertEqual(next(item for item in data["days"][-1]["runs"] if item["run_id"] == run_id)["points_earned"], 0)
				self.assertIsNone(data["days"][-1]["earned"])
				self.assertIsNone(data["totals"]["balance"])

	def test_alias_streaks_never_leak_across_cutoff_or_accounts(self):
		self.values["history_aliases"] = ALIASES
		self.record("2026-10-25T09:00:00Z", {"default": 4}, root=True)
		self.record("2026-10-25T10:00:01Z", {"default": 6}, root=True)
		self.assertEqual(streaks.latest(["account-1"], self.user_dir, NOW)["items"][0]["day"], 4)
		self.assertEqual(streaks.latest(["@root"], self.user_dir, NOW)["items"][0]["day"], 6)
		self.assertIsNone(streaks.latest(["account-2"], self.user_dir, NOW))

	def test_alias_boundary_stats_split_raw_day_without_borrowing_post_move_points(self):
		self.active = ["account-1", "@root"]
		self.values["history_aliases"] = ALIASES
		self.record("2026-10-25T09:00:00Z", {"default": 4}, root=True)
		self.record("2026-10-25T11:00:00Z", {"default": 7}, root=True)
		first, second = self.get("account-1"), self.get("@root")
		old = next(day for day in first["days"] if day["date"] == "2026-10-25")
		new = next(day for day in second["days"] if day["date"] == "2026-10-25")
		self.assertEqual((old["earned"], new["earned"]), (4, 7))
		self.assertEqual((old["balance"], new["balance"]), (104, 107))
		self.assertEqual((len(old["runs"]), len(new["runs"])), (1, 1))

	def test_compacted_boundary_day_does_not_assign_mixed_totals_to_either_account(self):
		self.active = ["account-1", "@root"]
		self.record("2026-10-25T09:00:00Z", {"default": 4}, root=True)
		self.record("2026-10-25T11:00:00Z", {"default": 7}, root=True)
		self.get("@root")
		for path in self.folder.glob("*.jsonl"):
			path.unlink()
		self.get("@root")
		stored = (self.home / "stats-ledger.json").read_bytes()
		self.values["history_aliases"] = ALIASES
		for account in self.active:
			with self.subTest(account=account):
				value = self.get(account)
				day = next(day for day in value["days"] if day["date"] == "2026-10-25")
				self.assertIsNone(day["earned"])
				self.assertIsNone(day["balance"])
				self.assertEqual(len(day["runs"]), 1)
		self.assertEqual((self.home / "stats-ledger.json").read_bytes(), stored)
		# A separate known row cannot turn an unsplittable contribution into a
		# plausible partial total when it merges into the migrated account.
		rows = json.loads(stored)["rows"]
		known = copy.deepcopy(rows[0])
		known["account_id"] = "account-1"
		view = account_history.project_rows(rows + [known], account_history.aliases(ALIASES))
		self.assertIsNone(view[("2026-10-25", "account-1")]["earned"])
		self.assertIsNone(view[("2026-10-25", "account-1")]["balance"])

	def test_compacted_multi_account_partial_summary_keeps_known_own_outcome(self):
		self.active = ["account-1", "account-2"]
		run_id, _ = self.record("2026-10-24T10:00:00Z", {"account-1": 3, "account-2": "identity_unknown"}, result="partial")
		self.get()
		(self.folder / (run_id + ".jsonl")).unlink()
		value = self.get("account-1")
		item = next(item for day in value["days"] for item in day["runs"] if item["run_id"] == run_id)
		self.assertEqual((item["result"], item["points_earned"]), ("ok", 3))
		value = self.get("account-2")
		item = next(item for day in value["days"] for item in day["runs"] if item["run_id"] == run_id)
		self.assertNotEqual(item["result"], "ok")
		self.assertIsNone(item["points_earned"])

	def test_one_normal_skip_does_not_inherit_the_other_accounts_ok_result(self):
		self.active = ["account-1", "account-2"]
		run_id, events = self.record("2026-10-26T10:00:00Z", {"account-1": 3, "account-2": "identity_unknown"}, result="ok")
		for index, event in enumerate(events):
			if event.get("account") == "account-2" and event["type"] == "account_end":
				events[index:index + 1] = [
					{**event, "type": "decision", "should_run": False, "reason": "nothing_open"},
					{key: value for key, value in event.items() if key != "error_code"},
				]
				break
		(self.folder / (run_id + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
		self.assertEqual(runs.load_run(run_id, {}, self.user_dir, account_id="account-2")["result"], "skipped")
		data = self.get("account-2")
		item = next(item for day in data["days"] for item in day["runs"] if item["run_id"] == run_id)
		self.assertEqual((item["result"], item["kind"], item["points_earned"]), ("skipped", "check", None))
		self.assertEqual(data["totals"]["run_count"], 0)
		self.assertEqual(data["totals"]["check_count"], 1)

	def test_single_account_fixture_is_unchanged_when_selected(self):
		fixture = Path(__file__).parent / "fixtures/runs/stopped.jsonl"
		events = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
		run_id = events[0]["run"]
		(self.folder / (run_id + ".jsonl")).write_bytes(fixture.read_bytes())
		original = runs.load_run(run_id, {}, "profiles", history_aliases=[])
		selected = runs.load_run(run_id, {}, "profiles", account_id=original["accounts"][0]["account_id"], history_aliases=[])
		self.assertEqual(selected, original)
		self.assertEqual(runs._summary(selected), runs.history(account_id=original["accounts"][0]["account_id"], user_data_dir="profiles")["runs"][0])

	def test_legacy_root_account_name_and_unrecognized_schema_never_leak_views(self):
		self.values["history_aliases"] = ALIASES
		run_id, _ = self.record("2026-10-24T10:00:00Z", {"@root": 3}, root=True)
		self.assertEqual(runs.load_run(run_id, {}, self.user_dir, account_id="account-1")["points_earned"], 3)
		other, events = self.record("2026-10-26T10:00:00Z", {"account-2": 7})
		for event in events:
			event["v"] = 999
		(self.folder / (other + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
		self.assertEqual(runs.load_run(other, {}, self.user_dir)["result"], "unknown")
		with self.assertRaises(FileNotFoundError):
			runs.load_run(other, {}, self.user_dir, account_id="account-1")
		self.assertEqual([item["run_id"] for item in runs.history(account_id="account-1", user_data_dir=self.user_dir)["runs"]], [run_id])

	def test_cross_midnight_compacted_row_uses_run_start_for_alias_not_terminal_day(self):
		self.active = ["account-1", "@root"]
		run_id, events = self.record("2026-10-24T23:59:59Z", {"default": 3}, root=True)
		for event in events[2:]:
			event["ts"] = "2026-10-25T00:00:01Z"
		(self.folder / (run_id + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
		folded = stats_ledger.fold(str(self.folder), data_home=self.home, user_data_dir=self.user_dir, now=NOW, zone=timezone.utc)
		(self.folder / (run_id + ".jsonl")).unlink()
		rules = account_history.aliases([{"from": "@root", "to": "account-1", "before": "2026-10-25T00:00:00Z"}])
		view = account_history.rows_for_view(folded.ledger["rows"], self.folder, self.user_dir, rules, NOW)
		self.assertEqual({row["account_id"] for row in view.values()}, {"account-1"})
		self.assertEqual(view[("2026-10-25", "account-1")]["earned"], 3)

	def test_account_with_unfinished_task_keeps_stopped_outcome_not_other_accounts_ok(self):
		self.active = ["account-1", "account-2"]
		run_id, events = self.record("2026-10-26T10:00:00Z", {"account-1": 3, "account-2": 5}, result="stopped")
		events = [event for event in events if not (event.get("account") == "account-2" and (event["type"] == "task_end" or event["type"] == "state" and event.get("phase") == "after"))]
		(self.folder / (run_id + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
		self.assertEqual(runs.load_run(run_id, {}, self.user_dir, account_id="account-1")["result"], "ok")
		self.assertEqual(runs.load_run(run_id, {}, self.user_dir, account_id="account-2")["result"], "stopped")
		data = self.get("account-1")
		item = next(item for day in data["days"] for item in day["runs"] if item["run_id"] == run_id)
		self.assertEqual((item["result"], item["problems"]), ("ok", []))
		data = self.get("account-2")
		item = next(item for day in data["days"] for item in day["runs"] if item["run_id"] == run_id)
		self.assertEqual(item["result"], "stopped")
		self.assertEqual(item["problems"], [{"task": None, "reason_key": "errors.run.stopped"}])

	def test_account_without_terminal_evidence_never_inherits_other_accounts_success(self):
		self.active = ["account-1", "account-2"]
		run_id, events = self.record("2026-10-26T10:00:00Z", {"account-1": 3, "account-2": 5})
		events = [event for event in events if event.get("account") != "account-2"]
		(self.folder / (run_id + ".jsonl")).write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
		detail = runs.load_run(run_id, {}, self.user_dir, account_id="account-2")
		self.assertEqual((detail["result"], detail["points_earned"]), ("unknown", None))
		data = self.get("account-2")
		item = next(item for day in data["days"] for item in day["runs"] if item["run_id"] == run_id)
		self.assertEqual((item["result"], item["points_earned"]), ("unknown", None))

	def test_aggregate_period_totals_never_skip_an_unknown_account_day(self):
		self.active = ["account-1", "account-2"]
		self.record("2026-10-25T10:00:00Z", {"account-1": 3, "account-2": "identity_unknown"}, result="partial")
		self.record("2026-10-26T10:00:00Z", {"account-1": 3, "account-2": 6})
		data = self.get()
		self.assertEqual(data["days"][-1]["earned"], 9)
		self.assertIsNone(data["totals"]["month_earned"])
		self.assertEqual(data["totals"]["week_earned"], 9)
		self.assertEqual(self.get("account-1")["totals"]["month_earned"], 6)

	def test_bridge_reads_migrated_history_with_real_readers_and_prefs_aliases(self):
		from rfapp.bridge import Bridge
		self.values["history_aliases"] = ALIASES
		run_id, _ = self.record("2026-10-24T10:00:00Z", {"default": 4}, root=True)
		api = Bridge(dependencies={"runs": runs, "stats": stats, "prefs": prefs, "settings": rfapp.settings,
			"schedule": types.SimpleNamespace(status=lambda: {}), "accounts_admin": types.SimpleNamespace()})
		self.addCleanup(api._close)
		for selected in (None, "account-1"):
			with self.subTest(account_id=selected):
				request = {"account_id": selected} if selected is not None else {}
				detail = api.get_run({"run_id": run_id, **request})
				self.assertTrue(detail["ok"], detail)
				self.assertEqual([row["account_id"] for row in detail["data"]["accounts"]], ["account-1"])
				self.assertEqual(detail["data"]["points_earned"], 4)
				history = api.get_history(request)
				self.assertTrue(history["ok"], history)
				self.assertEqual(history["data"]["runs"][0]["accounts"][0]["account_id"], "account-1")
			self.assertEqual(api.get_run({"run_id": run_id, "account_id": "account-2"})["error"]["code"], "run.unknown")
		view = api.get_overview({"account_id": "account-1"})
		self.assertTrue(view["ok"], view)
		self.assertEqual(view["data"]["accounts"][0]["state"]["balance"], 104)
		data = api.get_stats({"account_id": "account-1"})
		self.assertTrue(data["ok"], data)
		self.assertEqual((data["data"]["account_id"], data["data"]["totals"]["balance"]), ("account-1", 104))


if __name__ == "__main__":
	unittest.main()
