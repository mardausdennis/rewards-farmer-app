"""Unavailable tiles have plain reasons in task logs, receipts and checks."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
import check_selectors
import edge_browsing_streak as edge
import element_selectors
import rewards_tasks
import run_record
from fakes import FakeElement
from test_visual_identity import Harness as VisualHarness, SLIMDEBUG
from test_edge_browsing_streak import Harness as EdgeHarness


class PlainSkipReasons(unittest.TestCase):
	def assert_plain_skip(self, end, logs, task_name, reason):
		detail = "not available in this UI variant: " + reason
		self.assertEqual((end["outcome"], end["reason_key"]), ("skip", "not_available"))
		self.assertEqual(end["detail"], detail)
		line = next(record.getMessage() for record in logs.records if "[SKIP]" in record.getMessage())
		self.assertEqual(line, "[SKIP] " + task_name + ": " + detail)
		for text in (line, end["detail"]):
			self.assertNotIn("For documentation", text)
			self.assertNotIn("selenium.dev", text)

	def test_missing_visual_tile_has_plain_log_and_durable_skip_detail(self):
		harness = VisualHarness(titles=SLIMDEBUG)
		with self.assertLogs(rewards_tasks.logger, level="WARNING") as logs:
			end = harness.run()
		reason = "visual search streak is not available; shown tiles: 1,000 BONUS, " + ", ".join(SLIMDEBUG)
		self.assert_plain_skip(end, logs, "Visual search", reason)
		self.assertEqual(harness.clicked, [harness.earn])
		self.assertEqual(harness.upload_calls, 0)

	def test_missing_edge_tile_has_plain_log_and_durable_skip_detail(self):
		harness = EdgeHarness(absent=True)
		titles = ("1,000 BONUS", "Bing Search Streak", "Daily Set Streak", "Bing App Streak", "Visual Search Streak")
		harness.section.children[(By.TAG_NAME, "button")] = [FakeElement(text=title) for title in titles]
		with tempfile.TemporaryDirectory() as folder:
			recorder = run_record.open_run("app", ["synthetic"], {}, [], run_id="20261112T094700Z-0154", directory=folder)
			try:
				with mock.patch.dict(os.environ, {"EDGE_BROWSING_STREAK": "true", "REWARDS_SKIP_TASKS": ""}), run_record.account_context("synthetic"), self.assertLogs(rewards_tasks.logger, level="WARNING") as logs:
					harness.tasks.complete_all_tasks(only={"edge_browsing"})
				end = next(event for event in run_record.read_events(recorder.path) if event["type"] == "task_end")
			finally:
				run_record.close_run("partial", 0)
		reason = "Edge Browsing Streak is not available; shown tiles: " + ", ".join(titles)
		self.assert_plain_skip(end, logs, "Edge browsing streak", reason)
		self.assertEqual(harness.loads, [edge.EARN])
		self.assertEqual(harness.clicks, [])

	def test_visual_absent_check_uses_plain_reason(self):
		harness = VisualHarness(titles=SLIMDEBUG)
		report = mock.Mock()
		check_selectors._check_visual_search(report, harness.tasks.elements)
		reason = "visual search streak is not available; shown tiles: 1,000 BONUS, " + ", ".join(SLIMDEBUG)
		report.record.assert_called_once_with("get_open_visual_search_sidebar", "ABSENT", reason)
		report.check.assert_not_called()

	def test_exception_keeps_plain_reason_and_selenium_type_for_both_tasks(self):
		for exception_type in (element_selectors.VisualSearchUnavailable, edge.EdgeStreakUnavailable):
			with self.subTest(exception=exception_type.__name__):
				error = exception_type("shown tiles: Bing App Streak")
				self.assertEqual(error.reason, "shown tiles: Bing App Streak")
				self.assertIsInstance(error, NoSuchElementException)
				self.assertIn(error.reason, error.msg)


if __name__ == "__main__":
	unittest.main()
