import _bootstrap  # noqa: F401

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rfapp import account_history, i18n, prefs, runs, settings, streaks
import run_record
import stats_ledger
import overview_profile as fixture


class OverviewWorkTests(unittest.TestCase):
	def test_cold_rebuild_reads_and_projects_each_record_only_once(self):
		with fixture.synthetic() as (home,ids,api):
			with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader, patch.object(account_history,"project_events",wraps=account_history.project_events) as project:
				value = api.get_overview({})
			self.assertTrue(value["ok"],value)
			self.assertEqual(reader.call_count,100)
			# The Edge projection is separate policy and only reprojects today's
			# three records from shared memory, without opening their files again.
			self.assertEqual(project.call_count,103)

	def test_unchanged_overview_poll_does_not_reread_or_reduce_old_records(self):
		with fixture.synthetic() as (home,ids,api):
			before = api.get_overview({})
			self.assertTrue(before["ok"],before)
			with patch.object(run_record,"read_events",wraps=run_record.read_events) as reads, patch.object(runs,"load_run",wraps=runs.load_run) as details, patch.object(stats_ledger,"reduce_records",wraps=stats_ledger.reduce_records) as daily, patch.object(account_history,"project_events",wraps=account_history.project_events) as aliases, patch.object(streaks,"parse",wraps=streaks.parse) as parse:
				for _ in range(3): self.assertEqual(api.get_overview({}),before)
			self.assertEqual(reads.call_count,0)
			self.assertEqual(details.call_count,0)
			self.assertEqual(daily.call_count,0)
			self.assertEqual(aliases.call_count,0)
			self.assertEqual(parse.call_count,0)

	def test_parallel_polls_share_work_and_return_independent_snapshots(self):
		with fixture.synthetic() as (home,ids,api):
			before = api.get_overview({})
			with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
				with ThreadPoolExecutor(max_workers=12) as executor:
					values = list(executor.map(lambda _:api.get_overview({}),range(12)))
			self.assertTrue(all(value==before for value in values))
			self.assertEqual(reader.call_count,0)
			values[0]["data"]["accounts"][0]["state"]["balance"] = -999
			self.assertEqual(api.get_overview({}),before)

	def test_parallel_cold_polls_build_each_record_once(self):
		with fixture.synthetic() as (home,ids,api):
			with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
				with ThreadPoolExecutor(max_workers=12) as executor:
					values = list(executor.map(lambda _:api.get_overview({}),range(12)))
			self.assertTrue(values[0]["ok"],values[0])
			self.assertTrue(all(value==values[0] for value in values))
			self.assertEqual(reader.call_count,100)

	def test_one_and_one_hundred_runs_have_same_zero_warm_content_reads(self):
		for count in (1,100):
			with self.subTest(count=count),fixture.synthetic(count) as (home,ids,api):
				api.get_overview({})
				with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
					self.assertTrue(api.get_overview({})["ok"])
				self.assertEqual(reader.call_count,0)

	def test_append_new_record_removal_and_log_size_invalidate_snapshot(self):
		with fixture.synthetic() as (home,ids,api):
			before = api.get_overview({})["data"]
			with patch.object(run_record,"_timestamp",return_value=(fixture.NOW-timedelta(minutes=1)).isoformat()):
				run_record.append_event(str(home / "runs" / (ids[-1] + ".jsonl")),"state",ids[-1],account="account-1",phase="after",
					balance=99999,earned_today=200,unreadable="cards")
			changed = api.get_overview({})["data"]
			self.assertEqual(changed["accounts"][0]["state"]["balance"],99999)
			self.assertNotEqual(changed,before)
			log = home / "runs" / (ids[-1] + ".log")
			log.write_text("larger synthetic log\n" * 10)
			self.assertEqual(api.get_overview({})["data"]["last_run"]["log_size"],log.stat().st_size)
			latest = fixture.record(home,fixture.NOW-timedelta(seconds=30),101,capture=True)
			self.assertEqual(api.get_overview({})["data"]["last_run"]["run_id"],latest)
			(home / "runs" / (latest + ".jsonl")).unlink()
			self.assertEqual(api.get_overview({})["data"]["last_run"]["run_id"],ids[-1])

	def test_replaced_same_size_same_mtime_record_is_not_stale(self):
		with fixture.synthetic(1) as (home,ids,api):
			before = api.get_overview({})["data"]
			path = home / "runs" / (ids[0] + ".jsonl")
			stat = path.stat()
			replacement = path.with_suffix(".tmp")
			replacement.write_bytes(path.read_bytes().replace(b'"balance": 10020',b'"balance": 20020'))
			self.assertEqual(replacement.stat().st_size,stat.st_size)
			os.utime(replacement,ns=(stat.st_atime_ns,stat.st_mtime_ns))
			os.replace(replacement,path)
			self.assertEqual(api.get_overview({})["data"]["accounts"][0]["state"]["balance"],20020)
			self.assertEqual(before["accounts"][0]["state"]["balance"],10020)

	def test_account_labels_goals_aliases_and_paused_selection_invalidate_context(self):
		with fixture.synthetic(1) as (home,ids,api):
			before = api.get_overview({})["data"]
			prefs.save({"account_labels":{"account-1":"Changed synthetic"},"goals":{"account-1":30000}})
			changed = api.get_overview({})["data"]
			self.assertEqual(changed["accounts"][0]["label"],"Changed synthetic")
			self.assertEqual(changed["accounts"][0]["goal"]["target"],30000)
			self.assertNotEqual(changed,before)
			with patch.object(settings,"active_accounts",return_value=["account-1"]),patch.object(settings,"paused_accounts",return_value=["account-2"]):
				self.assertTrue(api.get_overview({})["data"]["accounts"][1]["paused"])
			path = home / "runs" / (ids[0] + ".jsonl")
			events = run_record.read_events(str(path))
			for event in events:
				if event.get("account") == "account-1":
					event["account"] = "default"
					if event["type"] == "account_start": event["profile"] = str(home / "profiles")
				if event["type"] == "run_start": event["accounts"][0] = "default"
			path.write_text("\n".join(map(json.dumps,events))+"\n")
			self.assertIsNone(api.get_overview({})["data"]["accounts"][0]["state"])
			prefs.save({"history_aliases":[{"from":"@root","to":"account-1","before":"2027-01-01T00:00:00Z"}]})
			self.assertIsNotNone(api.get_overview({})["data"]["accounts"][0]["state"])

	def test_daily_rollover_and_future_observation_crossing_cannot_reuse_stale_view(self):
		with fixture.synthetic(1) as (home,ids,api):
			values,p = settings.load(),prefs.load()
			first = runs.overview(values,p,fixture.NOW)
			tomorrow = runs.overview(values,p,fixture.NOW+timedelta(days=1))
			self.assertNotEqual(first["accounts"][0]["chart"],tomorrow["accounts"][0]["chart"])
			with patch.object(run_record,"_timestamp",return_value=(fixture.NOW+timedelta(minutes=1)).isoformat()):
				run_record.append_event(str(home / "runs" / (ids[0] + ".jsonl")),"state",ids[0],account="account-1",phase="after",
					balance=99999,earned_today=100,unreadable="cards")
			first = runs.overview(values,p,fixture.NOW)
			later = runs.overview(values,p,fixture.NOW+timedelta(minutes=2))
			self.assertNotEqual(first["accounts"][0]["state"]["balance"],99999)
			self.assertEqual(later["accounts"][0]["state"]["balance"],99999)

	def test_active_lock_and_edge_state_change_are_not_hidden(self):
		with fixture.synthetic(1) as (home,ids,api):
			api.get_overview({})
			info = SimpleNamespace(owner="farmer",run_id=ids[-1],since="synthetic")
			with patch("run_lock.status",return_value=info):
				self.assertEqual(api.get_overview({})["data"]["active_run"],{"run_id":ids[-1],"since":"synthetic"})
			self.assertIsNone(api.get_overview({})["data"]["active_run"])
			state = {"v":1,"accounts":{},"auto_off":{"reason":"minutes_not_counted","account_id":"account-1"}}
			(home / "edge-streak.json").write_text(json.dumps(state))
			self.assertEqual(api.get_overview({})["data"]["edge_streak"]["auto_off"],state["auto_off"])

	def test_bridge_keeps_schedule_generated_time_and_signin_status_live(self):
		with fixture.synthetic(1) as (home,ids,api):
			api.get_overview({})
			with patch("rfapp.schedule.status",return_value={"in_sync":False,"error_code":"synthetic"}),patch("rfapp.bridge._utc",return_value="later"),patch.object(api,"_signin_active",return_value={"account_id":"account-2"}):
				value = api.get_overview({})["data"]
			self.assertEqual(value["generated_at"],"later")
			self.assertEqual(value["signin_active"],{"account_id":"account-2"})
			self.assertFalse(value["schedule"]["in_sync"])

	def test_transient_empty_read_from_nonempty_record_is_not_cached(self):
		with fixture.synthetic(1) as (home,ids,api):
			with patch.object(run_record,"read_events",return_value=[]):
				failed = api.get_overview({})
			self.assertIsNone(failed["data"]["accounts"][0]["state"])
			self.assertIsNotNone(api.get_overview({})["data"]["accounts"][0]["state"])

	def test_cache_does_not_survive_a_different_home_with_same_run_ids(self):
		with fixture.synthetic(1) as (home,ids,api):
			before = api.get_overview({})
		with fixture.synthetic(1) as (home,ids,api):
			path = home / "runs" / (ids[0] + ".jsonl")
			path.write_bytes(path.read_bytes().replace(b'"balance": 10020',b'"balance": 20020'))
			self.assertEqual(api.get_overview({})["data"]["accounts"][0]["state"]["balance"],20020)
			self.assertEqual(before["data"]["accounts"][0]["state"]["balance"],10020)

	def test_cached_all_and_selected_views_equal_uncached_reduction_exactly(self):
		with fixture.synthetic() as (home,ids,api):
			for account in (None,"account-1","account-2"):
				values,p = settings.load(),prefs.load()
				expected = runs._overview(values,p,fixture.NOW,account)
				self.assertEqual(runs.overview(values,p,fixture.NOW,account),expected)
				self.assertEqual(runs.overview(values,p,fixture.NOW,account),expected)

	def test_language_reload_invalidates_default_translated_account_labels(self):
		with fixture.synthetic(1) as (home,ids,api):
			prefs.save({"account_labels":{}})
			for label in ("Synthetic language one","Synthetic language two"):
				translator = SimpleNamespace(t=lambda key,**kwargs:label)
				with patch.object(i18n,"current",return_value=translator):
					self.assertEqual(api.get_overview({})["data"]["accounts"][0]["label"],label)

	def test_cache_contexts_stay_bounded(self):
		with fixture.synthetic(1) as (home,ids,api):
			for target in range(10000,10007):
				prefs.save({"goals":{"account-1":target}})
				self.assertEqual(api.get_overview({})["data"]["accounts"][0]["goal"]["target"],target)
			self.assertLessEqual(len(runs._overview_cache),4)

	def test_future_id_crossing_refreshes_edge_even_with_past_event_timestamps(self):
		with fixture.synthetic(1) as (home,ids,api):
			future = fixture.record(home,fixture.NOW+timedelta(minutes=1),101)
			path = home / "runs" / (future + ".jsonl")
			events = run_record.read_events(str(path))
			for event in events: event["ts"] = (fixture.NOW-timedelta(minutes=1)).isoformat()
			path.write_text("\n".join(map(json.dumps,events))+"\n")
			values,p = settings.load(),prefs.load()
			first = runs.overview(values,p,fixture.NOW)
			self.assertIsNone(first["edge_streak"]["accounts"][0]["edge_tile"])
			later = runs.overview(values,p,fixture.NOW+timedelta(minutes=2))
			self.assertTrue(later["edge_streak"]["accounts"][0]["edge_tile"])
