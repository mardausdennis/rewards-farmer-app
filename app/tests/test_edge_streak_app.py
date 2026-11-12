import _bootstrap  # noqa: F401

from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rfapp import bridge, settings, supervisor
import test_settings as setting_fixtures
import test_signin_flow as signin_fixtures
import test_supervisor as supervisor_fixtures
import run_record


class EdgeSettingTests(unittest.TestCase):
	setUp = setting_fixtures.SettingsTests.setUp
	envfile = setting_fixtures.SettingsTests.envfile

	def test_default_off_and_three_way_bridge_roundtrip(self):
		self.assertEqual(settings.load().get("run.edge_streak"), "off")
		api = bridge.Bridge(dependencies={"settings":settings})
		self.addCleanup(api._close)
		for mode in ("fill", "remind", "off"):
			result = api.save_settings({"changes":{"run.edge_streak":mode}})
			self.assertTrue(result["ok"], result)
			self.assertEqual(api.get_settings({})["data"]["values"]["run.edge_streak"], mode)
			self.assertEqual(api.get_settings({})["data"]["edge_streak"]["mode"], mode)
			self.assertIn("REWARDS_EDGE_STREAK=" + mode, (self.root / ".env").read_text())
		self.assertEqual(settings.validate({"run.edge_streak":"true"}, settings.load()), {"run.edge_streak":"out_of_range"})

	def test_old_data_home_missing_state_remains_read_only_and_off(self):
		from rfapp import edge_streak
		self.assertEqual(edge_streak.snapshot(settings.load(), []), {"mode":"off", "auto_off":None, "accounts":[]})
		self.assertFalse((self.root / "edge-streak.json").exists())

	def test_only_last_slot_fill_and_budget_enable_child(self):
		from rfapp import edge_streak
		with patch.object(self,"home",self.root,create=True), patch.object(self,"folder",self.root / "runs",create=True):
			EdgeSupervisorTests.prior_state(self)
		values = {**settings._DEFAULTS, "run.edge_streak":"fill"}
		for kind, clock, mode, enabled in (
			("scheduled", "21:45", "fill", True), ("scheduled", "21:59", "fill", True),
			("scheduled", "17:45", "fill", False), ("scheduled", "22:30", "fill", False),
			("run_now", "21:45", "fill", False), ("check_now", "21:45", "fill", False),
			("scheduled", "21:45", "off", False), ("scheduled", "21:45", "remind", False)):
			at = datetime.fromisoformat("2026-11-11T" + clock + ":00+01:00")
			plan = edge_streak.plan({**values, "run.edge_streak":mode}, kind, at, elapsed_seconds=0, accounts=1,
				account_ids=["account-1"],user_data_dir=self.root / "profile")
			self.assertEqual(plan["enabled"], enabled, (kind,clock,mode))
			self.assertEqual(plan["env"]["EDGE_BROWSING_STREAK"], str(enabled).lower())
			if enabled: self.assertEqual(plan["env"]["REWARDS_HEADLESS"], "true")
		late = edge_streak.plan(values, "scheduled", datetime(2026,11,11,21,45), elapsed_seconds=2500, accounts=2,
			account_ids=["account-1"],user_data_dir=self.root / "profile")
		self.assertTrue(late["enabled"])
		self.assertLessEqual(2500 + 2 * float(late["env"]["EDGE_BROWSING_STREAK_MAX_MINUTES"]) * 60 + 300, 3590)
		self.assertFalse(edge_streak.plan(values, "scheduled", datetime(2026,11,11,21,45), elapsed_seconds=3280, accounts=1)["enabled"])

	def record(self, day, **changes):
		stamp = day + "T21:45:00+01:00"
		identity = datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-aaaa"
		base = {"v":run_record.SCHEMA_VERSION, "run_id":identity, "ts":stamp}
		observation = dict(outcome="incomplete", maximum_reached=True, final_read_success=True,
			minutes_before=4, minutes_after=4, minutes_target=30, maximum_minutes=35, elapsed_seconds=2100)
		result = changes.pop("run_result", "partial")
		observation.update(changes)
		events = [dict(base,type="run_start",trigger="scheduled",accounts=["account-1"],plan={"edge_topup":True}),
			dict(base,type="edge_streak_plan",slot=stamp,enabled=True,maximum_minutes=35),
			dict(base,type="account_start",account="account-1",profile=str(self.root / "profiles/account-1")),
			dict(base,type="state",account="account-1",phase="after",edge_tile=True,edge_minutes=observation["minutes_after"],edge_target=observation["minutes_target"]),
			dict(base,type="edge_browsing",account="account-1",**observation),
			dict(base,type="account_end",account="account-1",error_code=None), dict(base,type="run_end",result=result,exit_code=0)]
		return identity, events

	def evaluate(self, day, **changes):
		from rfapp import edge_streak
		identity, events = self.record(day, **changes)
		return edge_streak.process_run(identity, events, user_data_dir=str(self.root / "profiles"))

	def test_two_consecutive_counted_days_switch_fill_to_remind_and_are_idempotent(self):
		self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
		self.evaluate("2026-11-10")
		self.assertEqual(settings.load()["run.edge_streak"], "fill")
		self.evaluate("2026-11-11")
		self.assertEqual(settings.load()["run.edge_streak"], "remind")
		from rfapp import edge_streak
		state = edge_streak.load()
		self.assertEqual(state["auto_off"]["dates"], ["2026-11-10", "2026-11-11"])
		self.assertEqual(state["auto_off"]["account_id"], "account-1")
		self.assertEqual(self.evaluate("2026-11-11"), state)

	def test_rise_gap_target_change_unknown_error_and_stop_never_form_two_days(self):
		for middle in ({"minutes_after":5}, {"outcome":"error"}, {"run_result":"stopped"},
			{"final_read_success":False}, {"minutes_after":None}, {"elapsed_seconds":2099},
			{"minutes_target":40}, {"maximum_reached":False}):
			with self.subTest(middle=middle):
				(self.root / "edge-streak.json").unlink(missing_ok=True)
				self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
				self.evaluate("2026-11-09")
				self.evaluate("2026-11-10", **middle)
				self.evaluate("2026-11-11")
				self.assertEqual(settings.load()["run.edge_streak"], "fill")
		self.evaluate("2026-11-13")
		self.assertEqual(settings.load()["run.edge_streak"], "fill")

	def test_setting_fill_explicitly_clears_auto_off_for_new_attempt(self):
		self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
		self.evaluate("2026-11-10")
		self.evaluate("2026-11-11")
		settings.save({"run.edge_streak":"fill"})
		from rfapp import edge_streak
		self.assertEqual(settings.load()["run.edge_streak"], "fill")
		self.assertIsNone(edge_streak.load()["auto_off"])
		self.evaluate("2026-11-12")
		self.assertEqual(settings.load()["run.edge_streak"], "fill")

	def test_snapshot_latest_fields_from_current_account_record_and_unknown(self):
		from rfapp import edge_streak
		identity, events = self.record("2026-11-11")
		(self.root / "runs").mkdir(exist_ok=True)
		(self.root / "runs" / (identity + ".jsonl")).write_text("\n".join(map(json.dumps,events)) + "\n")
		with patch.object(settings, "user_data_dir", return_value=self.root / "profiles"):
			data = edge_streak.snapshot(settings.load(), ["account-1", "account-2"], now=datetime(2026,11,11,23).astimezone())
		self.assertEqual(data["accounts"][0]["edge_minutes"], 4)
		self.assertEqual(data["accounts"][0]["edge_target"], 30)
		self.assertEqual(data["accounts"][0]["last_scheduled_run_id"], identity)
		self.assertIsNone(data["accounts"][1]["edge_tile"])

	def test_no_tile_or_unknown_or_stopped_run_never_looks_reminder_ready(self):
		from rfapp import edge_streak
		identity, events = self.record("2026-11-11", run_result="stopped")
		state = next(e for e in events if e["type"] == "state")
		state.update(edge_tile=False, edge_minutes=20, edge_target=30)
		(self.root / "runs").mkdir(exist_ok=True)
		(self.root / "runs" / (identity + ".jsonl")).write_text("\n".join(map(json.dumps,events)) + "\n")
		data = edge_streak.snapshot(settings.load(), ["account-1"], user_data_dir=str(self.root / "profiles"), now=datetime(2026,11,11,23).astimezone())["accounts"][0]
		self.assertFalse(data["edge_tile"])
		self.assertIsNone(data["edge_minutes"])
		self.assertIsNone(data["last_scheduled_run_id"])

	def test_bridge_overview_carries_same_account_packet_as_settings(self):
		from rfapp import edge_streak
		api = bridge.Bridge(dependencies={"settings":settings,
			"runs":SimpleNamespace(overview=lambda *args,**kwargs:{"accounts":[{"account_id":"account-1"}]}),
			"accounts_admin":SimpleNamespace(list_accounts=lambda:{"accounts":[{"id":"account-1"}]}),
			"schedule":SimpleNamespace(status=lambda:{})})
		self.addCleanup(api._close)
		self.assertEqual(api.get_overview({})["data"]["edge_streak"], api.get_settings({})["data"]["edge_streak"])

	def test_runs_overview_used_by_tray_also_exposes_edge_packet(self):
		from rfapp import prefs, runs
		self.envfile("QUERY_SOURCE=trends\nREWARDS_ACCOUNTS=account-1\nREWARDS_EDGE_STREAK=remind\n")
		with patch("run_lock.status",return_value=None):
			value = runs.overview(settings.load(),prefs.load(),datetime.now(timezone.utc))
		self.assertEqual(value["edge_streak"]["mode"],"remind")
		self.assertEqual(value["edge_streak"]["accounts"][0]["account_id"],"account-1")

	def test_fill_save_failure_does_not_erase_prior_auto_off(self):
		from rfapp import edge_streak
		self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
		self.evaluate("2026-11-10")
		self.evaluate("2026-11-11")
		before = (self.root / "edge-streak.json").read_bytes()
		with patch.object(settings.EnvFile,"save",side_effect=OSError("synthetic")):
			with self.assertRaises(OSError): settings.save({"run.edge_streak":"fill"})
		self.assertEqual((self.root / "edge-streak.json").read_bytes(),before)
		self.assertEqual(settings.load()["run.edge_streak"],"remind")

	def test_nominal_slot_rules_days_fixed_mode_and_near_midnight(self):
		from rfapp import edge_streak
		values = {**settings._DEFAULTS,"run.edge_streak":"fill"}
		self.assertIsNone(edge_streak.last_slot({**values,"schedule.days":["thu"]},datetime(2026,11,11,21,50)))
		self.assertIsNone(edge_streak.last_slot({**values,"schedule.mode":"fixed","schedule.time":"09:45"},datetime(2026,11,11,21,50)))
		self.assertIsNotNone(edge_streak.last_slot({**values,"schedule.mode":"fixed","schedule.time":"21:45"},datetime(2026,11,11,21,50)))
		self.assertEqual(edge_streak.last_slot({**values,"schedule.window_end":"23:55"},datetime(2026,11,12,0,5)).date().isoformat(),"2026-11-11")
		self.assertIsNone(edge_streak.last_slot({**values,"schedule.checks":1},datetime(2026,11,11,21,50)))

	def test_counted_day_requires_exact_bound_maximum_and_complete_account(self):
		from rfapp import edge_streak
		self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
		self.evaluate("2026-11-10")
		identity, events = self.record("2026-11-11", maximum_minutes=10, elapsed_seconds=600)
		edge_streak.process_run(identity, events, user_data_dir=str(self.root / "profiles"))
		self.assertEqual(settings.load()["run.edge_streak"], "fill")
		self.assertIsNone(edge_streak.load()["auto_off"])

	def test_state_record_survives_failed_env_write_and_forces_remind(self):
		from rfapp import edge_streak
		self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
		self.evaluate("2026-11-10")
		with patch.object(edge_streak.EnvFile, "save", side_effect=OSError("synthetic")):
			with self.assertRaises(OSError): self.evaluate("2026-11-11")
		self.assertEqual(settings.load()["run.edge_streak"], "remind")
		self.assertEqual(edge_streak.load()["auto_off"]["dates"], ["2026-11-10", "2026-11-11"])

	def test_same_day_rise_cancels_an_earlier_count(self):
		from rfapp import edge_streak
		self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
		self.evaluate("2026-11-10")
		identity, events = self.record("2026-11-10", minutes_after=5)
		for event in events:
			event["ts"] = "2026-11-10T22:00:00+01:00"
		edge_streak.process_run(identity[:-4] + "bbbb", events, user_data_dir=str(self.root / "profiles"))
		self.evaluate("2026-11-11")
		self.assertEqual(settings.load()["run.edge_streak"], "fill")

	def test_unknown_or_newer_state_never_enables_fill_or_gets_overwritten(self):
		from rfapp import edge_streak
		self.envfile("QUERY_SOURCE=trends\nREWARDS_EDGE_STREAK=fill\n")
		path = self.root / "edge-streak.json"
		path.write_text('{"v":2,"future":"preserve"}')
		self.assertEqual(settings.load()["run.edge_streak"], "remind")
		with self.assertRaises(OSError): settings.save({"run.edge_streak":"fill"})
		self.assertEqual(json.loads(path.read_text())["v"], 2)


class EdgeSupervisorTests(unittest.TestCase):
	def setUp(self):
		supervisor_fixtures.SupervisorTests.setUp(self)
		self.stack.enter_context(patch("rfapp.edge_streak.process_age",return_value=0))

	def prior_state(self, *, account="account-1", stamp="2026-11-11T13:45:00+01:00", tile=True, minutes=4, target=30):
		at = datetime.fromisoformat(stamp)
		run_id = at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-aaaa"
		base = {"v":run_record.SCHEMA_VERSION,"run_id":run_id,"ts":stamp}
		events = [dict(base,type="run_start",trigger="scheduled",accounts=[account],plan={}),
			dict(base,type="account_start",account=account,profile=str(self.home / "profile" / account)),
			dict(base,type="state",account=account,phase="after",edge_tile=tile,edge_minutes=minutes,edge_target=target),
			dict(base,type="run_end",result="checked",exit_code=0)]
		self.folder.mkdir(exist_ok=True)
		(self.folder / (run_id + ".jsonl")).write_text("\n".join(map(json.dumps,events)) + "\n")


	def evening_env(self, chosen=None):
		self.settings.update({**settings._DEFAULTS,"run.edge_streak":"fill"})
		self.active_accounts = ["account-1"]
		proc = SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
		inherited = {key:value for key,value in os.environ.items() if key != "REWARDS_HEADLESS"}
		if chosen is not None: inherited["REWARDS_HEADLESS"] = chosen
		with patch.object(supervisor,"datetime") as dates, patch.object(supervisor,"_wait_until_available",return_value=None), patch.object(supervisor,"Job"), patch.object(supervisor.paths,"farmer_command",return_value=["fake"]), patch.object(supervisor.subprocess,"Popen",return_value=proc) as popen, patch.dict(os.environ,inherited,clear=True):
			dates.now.return_value = datetime(2026,11,11,21,50,tzinfo=timezone(timedelta(hours=1)))
			self.assertEqual(supervisor.supervise("scheduled"),0)
		return popen.call_args.kwargs["env"]


	def test_fill_without_tile_keeps_normal_visible_evening_run(self):
		self.prior_state(tile=False,minutes=None,target=None)
		env = self.evening_env()
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
		self.assertNotIn("REWARDS_HEADLESS",env)


	def test_fill_with_no_earlier_observation_keeps_browser_choice(self):
		env = self.evening_env("false")
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
		self.assertEqual(env["REWARDS_HEADLESS"],"false")


	def test_fill_with_unknown_minutes_keeps_browser_choice(self):
		self.prior_state(minutes=None)
		env = self.evening_env("false")
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
		self.assertEqual(env["REWARDS_HEADLESS"],"false")


	def test_fill_with_open_tile_from_earlier_today_runs_hidden(self):
		self.prior_state()
		env = self.evening_env("false")
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"true")
		self.assertEqual(env["REWARDS_HEADLESS"],"true")


	def test_fill_with_yesterdays_tile_keeps_browser_choice(self):
		self.prior_state(stamp="2026-11-10T13:45:00+01:00")
		env = self.evening_env("false")
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
		self.assertEqual(env["REWARDS_HEADLESS"],"false")


	def test_fill_with_complete_tile_keeps_browser_choice(self):
		self.prior_state(minutes=30)
		env = self.evening_env("false")
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
		self.assertEqual(env["REWARDS_HEADLESS"],"false")


	def test_no_tile_preserves_explicit_hidden_choice_too(self):
		self.prior_state(tile=False,minutes=None,target=None)
		env = self.evening_env("true")
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
		self.assertEqual(env["REWARDS_HEADLESS"],"true")


	def test_latest_absent_or_unknown_state_overrides_earlier_open_tile(self):
		for tile, minutes, target in ((False,None,None),(None,None,None),(True,None,30),(True,4,None)):
			with self.subTest(tile=tile,minutes=minutes,target=target):
				self.prior_state()
				self.prior_state(stamp="2026-11-11T17:45:00+01:00",tile=tile,minutes=minutes,target=target)
				env = self.evening_env("false")
				self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
				self.assertEqual(env["REWARDS_HEADLESS"],"false")


	def test_current_evening_observation_does_not_grant_its_own_topup(self):
		self.prior_state(stamp="2026-11-11T21:46:00+01:00")
		env = self.evening_env("false")
		self.assertEqual(env["EDGE_BROWSING_STREAK"],"false")
		self.assertEqual(env["REWARDS_HEADLESS"],"false")


	def test_only_same_local_day_not_utc_day_grants_hidden_topup(self):
		# Same UTC day, but this observation was yesterday in the schedule's zone.
		self.prior_state(stamp="2026-11-11T00:30:00+04:00")
		from rfapp import edge_streak
		self.assertEqual(edge_streak.open_accounts(["account-1"],datetime(2026,11,11,21,45,tzinfo=timezone(timedelta(hours=-4))),user_data_dir=self.home / "profile"),set())


	def test_mixed_profiles_keep_no_tile_profile_visible_and_bind_env_per_account(self):
		self.settings.update({**settings._DEFAULTS,"run.edge_streak":"fill"})
		self.active_accounts = ["account-1","account-2"]
		self.prior_state()
		# Separate run id to avoid replacing the first profile's observation.
		self.prior_state(account="account-2",stamp="2026-11-11T13:46:00+01:00",tile=False,minutes=None,target=None)
		proc = SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
		with patch.object(supervisor,"datetime") as dates, patch.object(supervisor,"_wait_until_available",return_value=None), patch.object(supervisor,"Job"), patch.object(supervisor.paths,"farmer_command",return_value=["fake"]), patch.object(supervisor.subprocess,"Popen",return_value=proc) as popen, patch.dict(os.environ,{"REWARDS_HEADLESS":"false"}):
			dates.now.return_value = datetime(2026,11,11,21,50,tzinfo=timezone(timedelta(hours=1)))
			self.assertEqual(supervisor.supervise("scheduled"),0)
		envs = {call.kwargs["env"]["REWARDS_ACCOUNTS"]:call.kwargs["env"] for call in popen.call_args_list}
		self.assertEqual(set(envs),{"account-1","account-2"})
		self.assertEqual((envs["account-1"]["EDGE_BROWSING_STREAK"],envs["account-1"]["REWARDS_HEADLESS"]),("true","true"))
		self.assertEqual((envs["account-2"]["EDGE_BROWSING_STREAK"],envs["account-2"]["REWARDS_HEADLESS"]),("false","false"))

	def test_real_supervisor_env_overrides_true_outside_evening_fill(self):
		self.settings.update(settings._DEFAULTS)
		self.active_accounts = ["account-1"]
		self.prior_state()
		for kind, hour, mode, enabled in (("scheduled",21,"fill",True), ("scheduled",17,"fill",False),
			("scheduled",21,"remind",False), ("scheduled",21,"off",False), ("run_now",21,"fill",False), ("check_now",21,"fill",False)):
			self.settings["run.edge_streak"] = mode
			at = datetime(2026,11,11,hour,50,tzinfo=timezone(timedelta(hours=1)))
			proc = SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
			with patch.object(supervisor, "datetime") as dates, patch.object(supervisor, "_wait_until_available", return_value=None), patch.object(supervisor, "Job"), patch.object(supervisor.paths, "farmer_command", return_value=["fake"]), patch.object(supervisor.subprocess,"Popen",return_value=proc) as popen, patch.dict(os.environ,{"EDGE_BROWSING_STREAK":"true"}):
				dates.now.return_value = at
				self.assertEqual(supervisor.supervise(kind),0)
				self.assertEqual(popen.call_args.kwargs["env"]["EDGE_BROWSING_STREAK"],str(enabled).lower())
				if enabled: self.assertEqual(popen.call_args.kwargs["env"]["REWARDS_HEADLESS"],"true")

	def test_prior_defer_and_tasks_are_stopped_with_five_minute_gate_margin(self):
		from rfapp import edge_streak
		self.settings.update({**settings._DEFAULTS,"run.edge_streak":"fill"})
		self.active_accounts = ["account-1"]
		self.prior_state()
		clock, ended = [0.0], []
		proc = SimpleNamespace(pid=123,returncode=None)
		proc.poll = lambda:proc.returncode
		def wait(timeout):
			if proc.returncode is None:
				clock[0] += timeout
				raise supervisor.subprocess.TimeoutExpired("fake",timeout)
		proc.wait = wait
		def terminate():
			ended.append(clock[0])
			proc.returncode = 1
		def defer():
			clock[0] += 2400
			return {"state":3,"waited_s":2400,"gave_up":False}
		with patch.object(supervisor,"datetime") as dates, patch.object(supervisor.time,"monotonic",side_effect=lambda:clock[0]), patch.object(supervisor,"_wait_until_available",side_effect=defer), patch.object(supervisor,"Job") as jobs, patch.object(supervisor.paths,"farmer_command",return_value=["fake"]), patch.object(supervisor.subprocess,"Popen",return_value=proc) as popen:
			dates.now.return_value = datetime(2026,11,11,21,50,tzinfo=timezone(timedelta(hours=1)))
			jobs.return_value.terminate.side_effect = terminate
			self.assertEqual(supervisor.supervise("scheduled"),1)
			maximum = float(popen.call_args.kwargs["env"]["EDGE_BROWSING_STREAK_MAX_MINUTES"])
		self.assertLessEqual(2400 + maximum * 60 + 300,3590)
		self.assertLessEqual(max(ended) + 5 + 300,3590)
		self.assertTrue(any(event.get("type") == "stop_requested" for path in self.folder.glob("*.jsonl") for event in run_record.read_events(str(path))))

	def test_policy_write_failure_keeps_farmer_return_and_correlated_receipt(self):
		from rfapp import edge_streak
		self.settings.update({**settings._DEFAULTS,"run.edge_streak":"fill"})
		self.active_accounts = ["account-1"]
		proc = SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
		with patch.object(supervisor,"datetime") as dates, patch.object(supervisor,"Job"), patch.object(supervisor.paths,"farmer_command",return_value=["fake"]), patch.object(supervisor.subprocess,"Popen",return_value=proc), patch.object(edge_streak,"process_run",side_effect=OSError("SYNTHETIC_SECRET")):
			dates.now.return_value = datetime(2026,11,11,21,50,tzinfo=timezone(timedelta(hours=1)))
			self.assertEqual(supervisor.supervise("scheduled"),0)
		events = [event for path in self.folder.glob("*.jsonl") for event in run_record.read_events(str(path))]
		self.assertEqual(next(event for event in events if event["type"] == "edge_streak_policy_error")["error"],"OSError")
		self.assertNotIn("SYNTHETIC_SECRET",json.dumps(events))

	def test_unknown_process_start_time_disables_topup(self):
		self.settings.update({**settings._DEFAULTS,"run.edge_streak":"fill"})
		self.active_accounts = ["account-1"]
		proc = SimpleNamespace(pid=123,returncode=0,poll=lambda:0)
		with patch.object(supervisor,"datetime") as dates, patch("rfapp.edge_streak.process_age",return_value=None), patch.object(supervisor,"Job"), patch.object(supervisor.paths,"farmer_command",return_value=["fake"]), patch.object(supervisor.subprocess,"Popen",return_value=proc) as popen:
			dates.now.return_value = datetime(2026,11,11,21,50,tzinfo=timezone(timedelta(hours=1)))
			self.assertEqual(supervisor.supervise("scheduled"),0)
		self.assertEqual(popen.call_args.kwargs["env"]["EDGE_BROWSING_STREAK"],"false")


class EdgeSigninTests(unittest.TestCase):
	setUp = signin_fixtures.SigninFlowTests.setUp
	state = signin_fixtures.SigninFlowTests.state

	def test_signin_forces_false_despite_inherited_or_dotenv_true(self):
		from rfapp import signin
		with patch.dict(os.environ, {"EDGE_BROWSING_STREAK":"true"}):
			self.assertEqual(signin.run("account-1", False), 0)
			self.assertEqual(os.environ["EDGE_BROWSING_STREAK"], "false")
