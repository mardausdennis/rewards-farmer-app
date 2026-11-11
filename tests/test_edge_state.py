"""E1 reads the real tile parser and selects tasks through the recorded run."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from selenium.webdriver.common.by import By
import edge_browsing_streak as edge
import element_selectors
import main
import rewards_state
import rewards_tasks
from fakes import FakeElement
from test_rewards_state import StateDriver
from test_quest_only import QuestDriver, record_run
from test_earn_reload import RenderDriver


def streak(title, counter=None, *, displayed=True):
	return FakeElement(text=title + ("\n" + counter if counter is not None else ""), displayed=displayed)


class EdgeDriver(QuestDriver):
	def __init__(self):
		super().__init__()
		self.quests = {}
		self.edge_minutes = 10

	def get(self, url):
		super().get(url)
		if self.current_url == rewards_state.EARN_URL:
			self.children[(By.ID, "streaks")] = [FakeElement(children={
				(By.TAG_NAME, "button"): [self.daily_opener, streak(edge.TITLE, f"Minutes: {self.edge_minutes}/30")],
			})]


class EdgeStateReads(unittest.TestCase):
	def read(self, tiles=(), *, driver=None):
		driver = driver or StateDriver()
		get = driver.get
		def load(url):
			get(url)
			if driver.current_url == rewards_state.EARN_URL:
				driver.children[(By.ID, "streaks")] = [FakeElement(children={(By.TAG_NAME, "button"): [driver.daily_opener, *tiles]})]
		driver.get = load
		with mock.patch.object(rewards_state.time, "sleep", lambda _: None):
			return rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)

	def fields(self, state):
		return state.edge_tile, state.edge_minutes, state.edge_target

	def test_uses_shared_parser_and_title_not_tile_position(self):
		with mock.patch.object(edge, "read_minutes", wraps=edge.read_minutes) as parser:
			state = self.read([streak("Bing App Streak", "Minutes: 99/100"), streak("  EDGE  Browsing Streak ", "Minutes: 12/30")])
		self.assertEqual(self.fields(state), (True, 12, 30))
		self.assertEqual(state.open_points, 0)
		self.assertIsNone(state.unreadable)
		parser.assert_called_once()

	def test_absent_empty_and_hidden_tiles_keep_false_and_nulls(self):
		for tiles in ([], [streak("Bing App Streak", "Minutes: 1/30")], [streak(edge.TITLE, "Minutes: 1/30", displayed=False)]):
			with self.subTest(tiles=len(tiles)):
				state = self.read(tiles)
				self.assertEqual(self.fields(state), (False, None, None))
				self.assertIsNone(state.unreadable)

	def test_present_unreadable_or_conflicting_counter_is_not_zero(self):
		for counter in (None, "Minutes: loading", "Minutes: 0/0", "Minutes: 10/30 Minutes: 20/30"):
			with self.subTest(counter=counter):
				state = self.read([streak(edge.TITLE, counter)])
				self.assertEqual(self.fields(state), (True, None, None))
				self.assertIsNone(state.unreadable)

	def test_edge_is_read_before_quest_navigation_without_clicking_it(self):
		driver = StateDriver(quests={"/earn/quest/example": []})
		state = self.read([streak(edge.TITLE, "Minutes: 5/30")], driver=driver)
		self.assertEqual(self.fields(state), (True, 5, 30))
		self.assertEqual(driver.current_url, "https://rewards.bing.com/earn/quest/example")
		self.assertEqual(driver.clicks, [driver.breakdown, driver.close, driver.daily_opener, driver.close])

	def test_whole_page_failure_has_no_invented_tile(self):
		state = self.read(driver=StateDriver(redirect="https://login.live.com/"))
		self.assertEqual(self.fields(state), (False, None, None))
		self.assertEqual(state.unreadable, "signed_out")

	def test_recovery_replaces_first_attempt_edge_observation(self):
		for second in ([], [streak(edge.TITLE, "Minutes: 30/30")]):
			with self.subTest(second=bool(second)):
				driver = RenderDriver(first="c", second="full")
				get = driver.get
				def load(url):
					get(url)
					if driver.current_url == rewards_state.EARN_URL:
						tiles = [streak(edge.TITLE, "Minutes: 10/30")] if driver.attempt == 1 else second
						driver.children[(By.ID, "streaks")] = [FakeElement(children={(By.TAG_NAME, "button"): [driver.daily_opener, *tiles]})]
				driver.get = load
				with mock.patch.object(rewards_state.time, "sleep", lambda _: None):
					state = rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)
				self.assertEqual(driver.attempt, 2)
				self.assertEqual(self.fields(state), (True, 30, 30) if second else (False, None, None))


class EdgeDecisions(unittest.TestCase):
	def setUp(self):
		self.plan = main.RunPlan(only_if_open=True, min_points=15, edge_topup=True)
		self.state = rewards_state.RewardsState(edge_tile=True, edge_minutes=10, edge_target=30)

	def test_known_open_edge_bypasses_threshold_without_changing_worth_running(self):
		for changes in ({}, {"open_points": 14}, {"unreadable": "searches"}, {"unreadable": "balance"}):
			with self.subTest(changes=changes):
				self.assertEqual(main.decide(replace(self.state, **changes), self.plan), (True, "edge_topup"))
		self.assertFalse(rewards_state.worth_running(self.state, 15))

	def test_off_complete_absent_and_unknown_never_start_an_edge_only_run(self):
		self.assertEqual(main.decide(self.state, replace(self.plan, edge_topup=False)), (False, "nothing_open"))
		for changes in ({"edge_tile": False}, {"edge_minutes": None}, {"edge_target": None},
			{"edge_minutes": 30}, {"edge_minutes": 31}, {"edge_target": 0}):
			with self.subTest(changes=changes):
				self.assertEqual(main.decide(replace(self.state, **changes), self.plan), (False, "nothing_open"))

	def test_whole_page_safety_and_explicit_unreadable_override_are_preserved(self):
		for code in (*main.WHOLE_PAGE_CODES, "searches,page_blank"):
			with self.subTest(code=code):
				state = replace(self.state, unreadable=code)
				self.assertEqual(main.decide(state, self.plan), (False, "unreadable"))
				self.assertEqual(main.decide(state, replace(self.plan, run_if_unreadable=True)), (True, "unreadable_run_anyway"))

	def test_existing_points_and_quest_reasons_remain(self):
		self.assertEqual(main.decide(replace(self.state, open_points=15), self.plan), (True, "enough_open"))
		self.assertEqual(main.decide(replace(self.state, quests_open=1), self.plan), (True, "quest_unlocked"))


class EdgeRecordedRuns(unittest.TestCase):
	def run_case(self, plan=None, *, enabled="true", **kwargs):
		temp = tempfile.TemporaryDirectory()
		self.addCleanup(temp.cleanup)
		def topup(worker):
			worker.driver.edge_minutes = 30
		with mock.patch.dict(os.environ, {"EDGE_BROWSING_STREAK": enabled}), mock.patch("test_quest_only.QuestDriver", EdgeDriver), mock.patch.object(rewards_tasks.RewardsTaskUtils, "complete_edge_browsing_streak", topup):
			return record_run(temp.name, plan or main.RunPlan(only_if_open=True), **kwargs)

	def task_ids(self, result):
		return [event["task"] for event in result.events if event["type"] == "task_start"]

	def test_only_edge_runs_and_real_state_events_show_before_and_after(self):
		result = self.run_case()
		self.assertEqual(result.code, 0)
		self.assertEqual(self.task_ids(result), ["edge_browsing"])
		self.assertEqual(result.invoked, [])
		self.assertTrue(result.events[0]["plan"]["edge_topup"])
		self.assertEqual(next(event["reason"] for event in result.events if event["type"] == "decision"), "edge_topup")
		states = [event for event in result.events if event["type"] == "state"]
		self.assertEqual([(event["phase"], event["edge_tile"], event["edge_minutes"], event["edge_target"]) for event in states],
			[("before", True, 10, 30), ("after", True, 30, 30)])
		self.assertEqual(result.events[-1]["result"], "ok")
		self.assertEqual(result.drivers["sample"].quit_calls, 1)

	def test_below_threshold_points_do_not_start_extra_tasks(self):
		result = self.run_case(state_changes={"open_points": 14})
		self.assertEqual(self.task_ids(result), ["edge_browsing"])

	def test_enough_points_keep_the_normal_tasks_plus_edge(self):
		result = self.run_case(state_changes={"open_points": 15})
		self.assertEqual(self.task_ids(result), [*rewards_tasks.TASK_IDS, "edge_browsing"])

	def test_quest_only_selection_also_keeps_the_open_edge_task(self):
		result = self.run_case(state_changes={"quests_open": 1})
		self.assertEqual(self.task_ids(result), ["quests", "edge_browsing"])

	def test_false_environment_cannot_be_overridden_by_a_plan_flag(self):
		result = self.run_case(main.RunPlan(only_if_open=True, edge_topup=True), enabled="false")
		self.assertFalse(result.events[0]["plan"]["edge_topup"])
		self.assertEqual(self.task_ids(result), [])
		self.assertEqual(result.events[-1]["result"], "skipped")

	def test_off_environment_keeps_ordinary_task_dispatch_unchanged(self):
		result = self.run_case(enabled="false", state_changes={"open_points": 15})
		self.assertFalse(result.events[0]["plan"]["edge_topup"])
		self.assertEqual(self.task_ids(result), list(rewards_tasks.TASK_IDS))

	def test_check_only_records_edge_without_running_any_task(self):
		result = self.run_case(main.RunPlan(check_only=True))
		self.assertEqual(self.task_ids(result), [])
		self.assertEqual(result.events[-1]["result"], "checked")
		state = next(event for event in result.events if event["type"] == "state")
		self.assertEqual((state["edge_tile"], state["edge_minutes"], state["edge_target"]), (True, 10, 30))

	def test_default_cli_run_keeps_all_tasks_without_adding_state_reads(self):
		result = self.run_case(main.RunPlan())
		self.assertTrue(result.events[0]["plan"]["edge_topup"])
		self.assertEqual(self.task_ids(result), [*rewards_tasks.TASK_IDS, "edge_browsing"])
		self.assertEqual(result.state_calls, [])

	def test_unknown_counter_is_recorded_without_starting_a_topup(self):
		result = self.run_case(state_changes={"edge_minutes": None, "edge_target": None})
		self.assertEqual(self.task_ids(result), [])
		self.assertEqual(result.events[-1]["result"], "skipped")
		state = next(event for event in result.events if event["type"] == "state")
		self.assertIs(state["edge_tile"], True)
		self.assertIsNone(state["edge_minutes"])
		self.assertIsNone(state["edge_target"])

	def test_task_selection_is_not_carried_to_the_next_account(self):
		result = self.run_case(names=("one", "two"), state_changes=lambda name: {"open_points": 0 if name == "one" else 15})
		starts = [(event["account"], event["task"]) for event in result.events if event["type"] == "task_start"]
		self.assertEqual(starts, [("one", "edge_browsing"), *[("two", task) for task in (*rewards_tasks.TASK_IDS, "edge_browsing")]])


if __name__ == "__main__":
	unittest.main()
