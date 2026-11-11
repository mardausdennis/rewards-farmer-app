"""F148 partial earn renders: real reader/selectors, JS snapshot and record."""
import json
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import page_structure
import rewards_state
import run_record
import main
import rewards_tasks
from fakes import FakeElement
from selenium.webdriver.common.by import By
from test_reader_header import figure, javascript, node
from test_rewards_state import StateDriver, misc_card
from test_quest_only import record_run


def document(shape):
	# Actual reported shape (a): earn tab + streaks, no other data sources.
	children = [node("a", "Earn", {"id": "react-aria-tab-/earn"}),
		node("header", children=[] if shape == "a" else [figure()]),
		node("section", "Synthetic streak", {"id": "streaks"}, [node("button", "Streak")])]
	if shape != "a":
		children.append(node("button", "Points breakdown"))
	if shape == "full":
		for section in ("moreactivities", "quests"):
			children.append(node("section", "Synthetic content", {"id": section}, [node("a", "Item")]))
	return node("document", children=[node("body", children=children)])


class RenderDriver(StateDriver):
	def __init__(self, first="a", second="full"):
		super().__init__(daily_done=1, cards=[misc_card(20)], quests={})
		self.first, self.second = first, second
		self.shape = first
		self.attempt = 0
		self.snapshots = {}

	def get(self, url):
		if url == rewards_state.EARN_URL and self.earn_loads in (0, 4):
			self.attempt += 1
			self.shape = self.first if self.attempt == 1 else self.second
			self.balance = "Loading..." if self.shape == "a" else "12,345"
		super().get(url)
		if url == rewards_state.EARN_URL and self.shape != "full":
			if self.shape in ("a", "b"):
				self.children[(By.ID, "streaks")] = [FakeElement()]
			if self.shape == "a":
				self.children[(By.TAG_NAME, "button")] = []
			elif self.shape == "b":
				self.children[(By.TAG_NAME, "button")] = [self.breakdown]
			self.children[(By.ID, "moreactivities")] = [FakeElement()]
			self.children[(By.ID, "quests")] = [FakeElement()]

	def execute_script(self, script, *args):
		if script == page_structure.SNAPSHOT:
			key = self.shape, self.current_url
			if key not in self.snapshots:
				self.snapshots[key] = javascript(script, document(self.shape), url=self.current_url)
			return self.snapshots[key]
		return super().execute_script(script, *args)


class EarnReloadTests(unittest.TestCase):
	def read(self, driver, **kwargs):
		with tempfile.TemporaryDirectory() as folder:
			recorder = run_record.open_run("cli", ["synthetic"], {}, [], run_id="20261111T090000Z-f148", directory=folder)
			try:
				with run_record.account_context("synthetic"), mock.patch.object(rewards_state.time, "sleep", return_value=None):
					state = rewards_state.read_state(driver, __import__("element_selectors").ElementSelectionUtils(driver), timeout=0, **kwargs)
				rows = [event for event in run_record.read_events(recorder.path) if event["type"] == "reader_structure"]
			finally:
				run_record.close_run("checked", 0)
		return state, rows

	def test_streaks_only_then_full_reloads_once_and_uses_second_result(self):
		driver = RenderDriver()
		state, rows = self.read(driver)
		self.assertIsNone(state.unreadable)
		self.assertEqual((state.balance, state.searches_earned, state.searches_max, state.daily_set_open, state.cards_open, state.open_points, state.earned_today),
			(12345, 60, 60, 2, 1, 40, 2345))
		self.assertEqual((driver.attempt, driver.earn_loads), (2, 5))
		self.assertEqual([(row["attempt"], row["reloaded"]) for row in rows], [(1, False)])
		self.assertEqual(rows[0]["matched"]["streaks"], 1)
		self.assertEqual(rows[0]["matched"]["cards"], 0)

	def test_both_partial_no_third_attempt_two_events(self):
		driver = RenderDriver(second="a")
		state, rows = self.read(driver)
		self.assertEqual(state.unreadable, "searches,daily_set,cards,quests,balance")
		self.assertIsNone(state.balance)
		self.assertIsNone(state.searches_max)
		self.assertEqual((driver.attempt, driver.earn_loads), (2, 5))
		self.assertEqual([(row["attempt"], row["reloaded"]) for row in rows], [(1, False), (2, True)])
		self.assertTrue(all(url == rewards_state.EARN_URL for url in driver.loads))
		self.assertEqual(driver.clicks, [])

	def test_full_first_no_reload_no_event(self):
		driver = RenderDriver(first="full")
		state, rows = self.read(driver)
		self.assertIsNone(state.unreadable)
		self.assertEqual((driver.attempt, driver.earn_loads), (1, 4))
		self.assertEqual(rows, [])

	def test_daily_cards_quests_and_cards_quests_shapes_both_retry(self):
		for shape, reason in (("b", "daily_set,cards,quests"), ("c", "cards,quests")):
			with self.subTest(shape=shape):
				driver = RenderDriver(first=shape)
				state, rows = self.read(driver)
				self.assertIsNone(state.unreadable)
				self.assertEqual(driver.attempt, 2)
				self.assertEqual(len(rows), 1)
				self.assertEqual(rows[0]["reason"], reason)
				self.assertEqual(rows[0]["attempt"], 1)

	def test_remaining_b_and_c_partial_states_get_two_attempt_events(self):
		for shape, reason in (("b", "daily_set,cards,quests"), ("c", "cards,quests")):
			with self.subTest(shape=shape):
				driver = RenderDriver(first=shape, second=shape)
				state, rows = self.read(driver)
				self.assertEqual(state.unreadable, reason)
				self.assertEqual(driver.earn_loads, 5)
				self.assertEqual([(row["attempt"], row["reason"]) for row in rows], [(1, reason), (2, reason)])

	def test_blank_and_signed_out_and_terms_keep_old_two_navigation_gate(self):
		for redirect, expected in ((None, "page_blank"), ("https://login.live.com/", "signed_out"),
			("https://rewards.bing.com/terms-of-use", "terms_of_use")):
			with self.subTest(redirect=redirect):
				driver = StateDriver(blank_loads={1, 2}, redirect=redirect)
				state, rows = self.read(driver)
				self.assertEqual(state.unreadable, expected)
				self.assertEqual(driver.earn_loads, 2)
				self.assertEqual(driver.clicks, [])
				self.assertEqual([(row["attempt"], row["reloaded"]) for row in rows], [(1, False)])

	def test_worse_second_attempt_keeps_first_whole_read(self):
		driver = RenderDriver(first="c", second="a")
		state, rows = self.read(driver)
		self.assertEqual((state.balance, state.searches_earned, state.earned_today), (12345, 60, 2345))
		self.assertEqual(state.unreadable, "cards,quests")
		self.assertEqual(len(rows), 2)

	def test_skip_cards_is_preserved_on_second_read(self):
		state, _ = self.read(RenderDriver(), skip_cards=frozenset({"Read this card"}))
		self.assertEqual((state.cards_open, state.open_points), (0, 20))

	def test_missing_individual_core_marker_triggers_only_one_additional_navigation(self):
		for code in ("cards", "quests", "breakdown", "header_figures"):
			with self.subTest(code=code):
				driver = RenderDriver(first="full")
				original = driver.execute_script
				def execute(script, *args):
					packet = original(script, *args)
					if script == page_structure.SNAPSHOT and driver.attempt == 1:
						packet = {**packet, "matched": {**packet["matched"], code: 0}}
					return packet
				driver.execute_script = execute
				state, rows = self.read(driver)
				self.assertIsNone(state.unreadable)
				self.assertEqual(driver.earn_loads, 5)
				self.assertEqual(len(rows), 1)

	def test_recovery_blank_uses_no_second_navigation_retry(self):
		driver = RenderDriver()
		driver.blank_loads = {5}
		original = driver.execute_script
		def execute(script, *args):
			if script == page_structure.SNAPSHOT and driver.earn_loads == 5:
				return javascript(script, node("document", children=[node("body")]))
			return original(script, *args)
		driver.execute_script = execute
		state, rows = self.read(driver)
		self.assertEqual(state.unreadable, "page_blank")
		self.assertEqual(driver.earn_loads, 5)
		self.assertEqual([(row["attempt"], row["reloaded"]) for row in rows], [(1, False), (2, True)])

	def test_latest_earn_snapshot_after_normal_wait_wins_over_early_partial(self):
		driver = RenderDriver(first="full")
		original = driver.execute_script
		calls = []
		def execute(script, *args):
			if script == page_structure.SNAPSHOT:
				calls.append(driver.earn_loads)
				if len(calls) == 1:
					return javascript(script, document("a"))
			return original(script, *args)
		driver.execute_script = execute
		state, rows = self.read(driver)
		self.assertIsNone(state.unreadable)
		self.assertEqual(driver.earn_loads, 4)
		self.assertEqual(rows, [])

	def test_later_blank_does_not_capture_earlier_full_document_packet(self):
		driver = RenderDriver(first="full")
		driver.blank_loads = {4, 5}
		original = driver.execute_script
		def execute(script, *args):
			if script == page_structure.SNAPSHOT and driver.earn_loads in (4, 5):
				return javascript(script, node("document", children=[node("body")]))
			return original(script, *args)
		driver.execute_script = execute
		state, rows = self.read(driver)
		self.assertEqual(state.unreadable, "page_blank")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["matched"]["earn_tab"], 0)
		self.assertFalse(rows[0]["rendered"])

	def test_location_readiness_and_interstitial_guards_never_authorize_reload(self):
		packet = javascript(page_structure.SNAPSHOT, document("a"))
		self.assertTrue(page_structure.needs_reload(packet))
		for key in ("complete", "rewards_host", "earn_path"):
			with self.subTest(key=key):
				self.assertFalse(page_structure.needs_reload({**packet, key: False}))
		for key in ("earn_tab", "signin", "consent", "error"):
			with self.subTest(key=key):
				self.assertFalse(page_structure.needs_reload({**packet, "matched": {**packet["matched"], key: 0 if key == "earn_tab" else 1}}))

	def test_partial_without_reloadable_document_still_emits_attempt(self):
		state, rows = self.read(StateDriver(daily_text="Loading..."))
		self.assertEqual(state.unreadable, "daily_set")
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0]["attempt"], rows[0]["reloaded"]), (1, False))

	def test_capture_is_limited_to_two_per_account_and_filters_supplied_packet(self):
		with tempfile.TemporaryDirectory() as folder:
			recorder = run_record.open_run("cli", ["synthetic"], {}, [], run_id="20261111T090000Z-f149", directory=folder)
			try:
				packet = javascript(page_structure.SNAPSHOT, document("a"))
				packet.update(text="SECRET user@example.invalid", balance=987654)
				packet["matched"]["private_name"] = "SECRET"
				with run_record.account_context("synthetic"):
					for _ in range(2):
						for attempt in (1, 2, 3):
							page_structure.capture_once(None, "cards,quests", attempt=attempt, reloaded=attempt == 2, packet=packet)
				rows = [event for event in run_record.read_events(recorder.path) if event["type"] == "reader_structure"]
				self.assertEqual(len(rows), 2)
				self.assertNotIn("SECRET", json.dumps(rows))
				self.assertNotIn("987654", json.dumps(rows))
			finally:
				run_record.close_run("checked", 0)


class SelectionDriver(RenderDriver):
	"""Sections may have read before the final earn document loses its controls."""
	def __init__(self, first="c", second="a", *, fixed_snapshot=False, second_redirect=None, edge_first=True):
		super().__init__(first, second)
		self.fixed_snapshot = fixed_snapshot
		self.second_redirect = second_redirect
		self.edge_first = edge_first
		self.set_page_load_timeout = mock.Mock()
		self.set_script_timeout = mock.Mock()
		self.execute_cdp_cmd = mock.Mock()

	def get(self, url):
		if self.earn_loads == 4 and url == rewards_state.EARN_URL:
			self.redirect = self.second_redirect
		super().get(url)
		if self.current_url != rewards_state.EARN_URL or self.redirect:
			return
		if self.shape != "a":
			self.balance = "1,001" if self.attempt == 1 else "2,002"
			self.children[(By.TAG_NAME, "header")][0].children[(By.TAG_NAME, "button")][0].text = self.balance
		self.searches = "0/60" if self.attempt == 1 else "30/60"
		self.daily_done = 1 if self.attempt == 1 else 3
		if (self.attempt == 1) == self.edge_first:
			self.children[(By.ID, "streaks")][0].children.setdefault((By.TAG_NAME, "button"), []).append(
				FakeElement(text="Edge Browsing Streak\nMinutes: 5/30" if self.attempt == 1 else "Edge Browsing Streak\nMinutes: 25/30"))

	def execute_script(self, script, *args):
		if script == page_structure.SNAPSHOT and self.earn_loads in self.blank_loads:
			return javascript(script, node("document", children=[node("body")]))
		if script == page_structure.SNAPSHOT and self.fixed_snapshot:
			packet = javascript(script, document("a"), url=self.current_url)
			return {**packet, "counts": {**packet["counts"], "buttons": 25, "sections": 10}}
		return super().execute_script(script, *args)


class ReloadSelectionTests(unittest.TestCase):
	# Reuse the recorder helper without inheriting the original test cases.
	read = EarnReloadTests.read

	def observed_read(self, driver):
		attempts = []
		read = rewards_state._read_state_attempt
		def observe(*args, **kwargs):
			state = read(*args, **kwargs)
			attempts.append(state)
			return state
		with mock.patch.object(rewards_state, "_read_state_attempt", side_effect=observe):
			state, rows = self.read(driver)
		return state, rows, attempts

	def assert_whole_read(self, state, attempts, number):
		fields = asdict(state)
		self.assertEqual(fields.pop("reader_attempt"), number)
		expected = asdict(attempts[number - 1])
		expected.pop("reader_attempt")
		self.assertEqual(fields, expected)

	def test_dbe4_same_document_shape_keeps_better_first_read_and_run_decision(self):
		state, rows, attempts = self.observed_read(SelectionDriver(fixed_snapshot=True))
		self.assertEqual([attempt.unreadable for attempt in attempts], ["cards,quests", "searches,daily_set,cards,quests,balance"])
		self.assertEqual(rows[0]["matched"], rows[1]["matched"])
		self.assertEqual(rows[0]["counts"], rows[1]["counts"])
		self.assertTrue(all(row["rendered"] for row in rows))
		self.assertEqual({key: rows[0]["matched"][key] for key in ("streaks", "breakdown", "cards", "quests", "header_figures")},
			{"streaks": 1, "breakdown": 0, "cards": 0, "quests": 0, "header_figures": 0})
		self.assertEqual((rows[0]["counts"]["buttons"], rows[0]["counts"]["sections"]), (25, 10))
		self.assertEqual(state.unreadable, "cards,quests")
		self.assertTrue(rewards_state.worth_running(state, 15))
		self.assertEqual(main.decide(state, main.RunPlan(only_if_open=True, min_points=15)), (True, "enough_open"))
		self.assert_whole_read(state, attempts, 1)

	def test_better_second_read_is_kept_without_first_edge_values(self):
		state, _, attempts = self.observed_read(SelectionDriver(first="a", second="c"))
		self.assertEqual(state.unreadable, "cards,quests")
		self.assertFalse(state.edge_tile)
		self.assertIsNone(state.edge_minutes)
		self.assert_whole_read(state, attempts, 2)

	def test_tie_uses_second_read_even_with_different_values_and_no_edge_tile(self):
		state, _, attempts = self.observed_read(SelectionDriver(first="c", second="c"))
		self.assertEqual(attempts[0].unreadable, attempts[1].unreadable)
		self.assertEqual((state.balance, state.searches_earned, state.daily_set_open, state.open_points), (2002, 30, 0, 30))
		self.assertFalse(state.edge_tile)
		self.assert_whole_read(state, attempts, 2)

	def test_equal_section_counts_choose_second_even_when_missing_sections_differ(self):
		driver = SelectionDriver(first="c", second="full", edge_first=False)
		get = driver.get
		def load(url):
			get(url)
			if driver.attempt == 2:
				driver.children[(By.TAG_NAME, "button")] = []
				driver.children[(By.ID, "streaks")] = [FakeElement()]
		driver.get = load
		state, _, attempts = self.observed_read(driver)
		self.assertEqual([attempt.unreadable for attempt in attempts], ["cards,quests", "searches,daily_set"])
		self.assertEqual(state.unreadable, "searches,daily_set")
		self.assertIsNone(state.searches_earned)
		self.assertEqual(state.balance, 2002)
		self.assert_whole_read(state, attempts, 2)

	def test_first_read_does_not_import_new_edge_measurements_from_worse_second(self):
		state, _, attempts = self.observed_read(SelectionDriver(edge_first=False))
		self.assertTrue(attempts[1].edge_tile)
		self.assertFalse(state.edge_tile)
		self.assertIsNone(state.edge_minutes)
		self.assert_whole_read(state, attempts, 1)

	def test_second_whole_page_failure_still_overrides_earlier_known_work(self):
		for redirect, code in (("https://login.live.com/", "signed_out"),
			("https://rewards.bing.com/terms-of-use", "terms_of_use"), (None, "page_blank")):
			with self.subTest(code=code):
				driver = SelectionDriver(second_redirect=redirect)
				if redirect is None:
					driver.blank_loads = {5}
				state, _, attempts = self.observed_read(driver)
				self.assertEqual(state.unreadable, code)
				self.assertIsNone(rewards_state.worth_running(state, 15))
				self.assert_whole_read(state, attempts, 2)

	def test_single_read_records_attempt_one(self):
		state, rows, attempts = self.observed_read(SelectionDriver(first="full"))
		self.assertEqual(rows, [])
		self.assertEqual(len(attempts), 1)
		self.assert_whole_read(state, attempts, 1)

	def test_real_state_record_identifies_kept_read_without_recording_discarded_values(self):
		with tempfile.TemporaryDirectory() as folder, mock.patch("test_quest_only.QuestDriver", SelectionDriver):
			result = record_run(folder, main.RunPlan(only_if_open=True), skipped=",".join(rewards_tasks.TASK_IDS))
		before = next(event for event in result.events if event["type"] == "state" and event["phase"] == "before")
		self.assertEqual(before["reader_attempt"], 1)
		self.assertEqual(before["balance"], 1001)
		self.assertEqual(before["unreadable"], "cards,quests")
		self.assertEqual((before["edge_tile"], before["edge_minutes"], before["edge_target"]), (True, 5, 30))
		self.assertTrue(next(event for event in result.events if event["type"] == "decision")["should_run"])
		self.assertNotEqual(result.events[-1]["result"], "skipped")


if __name__ == "__main__":
	unittest.main()
