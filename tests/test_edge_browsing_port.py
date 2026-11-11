"""Run-local Edge opt-in must preserve app task records and task filtering."""
import os
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import rewards_tasks
import run_record
import edge_browsing_streak as edge
from selenium.webdriver.common.by import By
from fakes import FakeDriver
from test_edge_browsing_streak import Harness


class EdgeTaskRegistration(unittest.TestCase):
	def runner(self):
		tasks = rewards_tasks.RewardsTaskUtils.__new__(rewards_tasks.RewardsTaskUtils)
		tasks.driver = FakeDriver()
		tasks.balance_reader = lambda: 100
		tasks.restore_main_tab = mock.Mock()
		tasks.return_to_rewards_home = mock.Mock()
		for _, _, method in rewards_tasks.TASKS:
			setattr(tasks, method, mock.Mock())
		tasks.complete_edge_browsing_streak = mock.Mock()
		return tasks

	def test_enabled_edge_only_records_one_task_without_running_other_tasks(self):
		tasks = self.runner()
		with mock.patch.dict(os.environ, {"EDGE_BROWSING_STREAK": "true"}), mock.patch.object(run_record, "emit") as emit:
			tasks.complete_all_tasks(only={"edge_browsing"})
		tasks.complete_edge_browsing_streak.assert_called_once_with()
		for _, _, method in rewards_tasks.TASKS:
			getattr(tasks, method).assert_not_called()
		self.assertEqual([call.args[0] for call in emit.call_args_list], ["task_start", "task_end"])
		end = emit.call_args_list[-1].kwargs
		self.assertEqual((end["task"], end["outcome"], end["points_before"], end["points_after"]), ("edge_browsing", "ok", 100, 100))

	def test_upstream_edge_entrypoint_exists(self):
		self.assertTrue(callable(getattr(rewards_tasks.RewardsTaskUtils, "complete_edge_browsing_streak", None)))

	def test_disabled_next_process_has_no_edge_task_or_record(self):
		tasks = self.runner()
		with mock.patch.dict(os.environ, {"EDGE_BROWSING_STREAK": "false"}), mock.patch.object(run_record, "emit") as emit:
			tasks.complete_all_tasks(only={"edge_browsing"})
		tasks.complete_edge_browsing_streak.assert_not_called()
		emit.assert_not_called()

	def test_enabling_edge_adds_only_one_final_task_and_preserves_all_others(self):
		tasks = self.runner()
		with mock.patch.dict(os.environ, {"EDGE_BROWSING_STREAK": "true", "REWARDS_SKIP_TASKS": ""}), mock.patch.object(run_record, "emit") as emit:
			tasks.complete_all_tasks()
		for _, _, method in rewards_tasks.TASKS:
			getattr(tasks, method).assert_called_once_with()
		tasks.complete_edge_browsing_streak.assert_called_once_with()
		ends = [call.kwargs for call in emit.call_args_list if call.args == ("task_end",)]
		self.assertEqual([row["task"] for row in ends], [row[0] for row in rewards_tasks.TASKS] + ["edge_browsing"])
		self.assertTrue(all(row["outcome"] == "ok" for row in ends))

	def test_false_process_environment_cannot_be_reenabled_by_dotenv(self):
		# The app controls its child process env; normal .env loading must not
		# replace an explicit per-run false with a user's permanent true value.
		import dotenv
		import io
		with mock.patch.dict(os.environ, {"EDGE_BROWSING_STREAK": "false"}):
			dotenv.load_dotenv(stream=io.StringIO("EDGE_BROWSING_STREAK=true\n"))
			self.assertFalse(edge.enabled())

	def test_real_record_contains_exactly_one_value_free_account_bound_measurement(self):
		h = Harness(((0, 30),))
		with tempfile.TemporaryDirectory() as folder:
			recorder = run_record.open_run("app", ["synthetic"], {}, [], run_id="20261111T193000Z-ed36", directory=folder)
			try:
				class Actions:
					def __init__(self, driver):
						pass
					def scroll_by_amount(self, *args):
						return self
					def perform(self):
						pass
				with mock.patch.dict(os.environ, {"EDGE_BROWSING_STREAK": "true", "EDGE_BROWSING_STREAK_MAX_MINUTES": "5"}), mock.patch.object(edge, "time", types.SimpleNamespace(monotonic=h.clock, sleep=h.clock.sleep)), mock.patch.object(edge, "ActionChains", Actions), run_record.account_context("synthetic"):
					with self.assertRaises(edge.EdgeStreakIncomplete):
						edge.complete(h.tasks)
			finally:
				run_record.close_run("partial", 0)
			rows = [row for row in run_record.read_events(recorder.path) if row["type"] == "edge_browsing"]
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["account"], "synthetic")
		self.assertEqual((row["minutes_before"], row["minutes_after"], row["minutes_target"]), (0, 0, 30))
		self.assertEqual((row["elapsed_seconds"], row["duration_ms"], row["maximum_minutes"]), (300, 300000, 5))
		self.assertIs(row["maximum_reached"], True)
		self.assertIs(row["final_read_success"], True)
		self.assertEqual(row["outcome"], "incomplete")
		self.assertEqual(set(row), {"type", "v", "ts", "run", "account", "minutes_before", "minutes_after", "minutes_target", "activated", "maximum_minutes", "maximum_reached", "final_read_success", "outcome", "elapsed_seconds", "duration_ms"})
		self.assertNotIn("http", json.dumps(row))

	def test_final_unreadable_counter_at_limit_cannot_count_as_unchanged_topup(self):
		h = Harness(((0, 30),))
		get = h.driver.get
		def final_loading(url):
			get(url)
			if url == edge.EARN and h.clock.now >= 300:
				h.tile.text = h.title
		h.driver.get = final_loading
		with self.assertRaises(__import__("element_selectors").ElementNotReady):
			h.run(maximum="5")
		row = h.records[0]
		self.assertIs(row["maximum_reached"], True)
		self.assertIs(row["final_read_success"], False)
		self.assertEqual(row["outcome"], "error")
		self.assertEqual(len(h.records), 1)

	def test_keyboard_stop_records_stopped_once_not_a_full_unchanged_topup(self):
		h = Harness(((0, 30),))
		get = h.driver.get
		def stop(url):
			if url in edge.PAGES:
				raise KeyboardInterrupt()
			return get(url)
		h.driver.get = stop
		with self.assertRaises(KeyboardInterrupt):
			h.run()
		self.assertEqual(len(h.records), 1)
		self.assertEqual(h.records[0]["outcome"], "stopped")
		self.assertIs(h.records[0]["final_read_success"], False)
		self.assertIs(h.records[0]["maximum_reached"], False)


if __name__ == "__main__":
	unittest.main()
