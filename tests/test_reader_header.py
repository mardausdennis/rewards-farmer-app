"""F135 observed shapes, synthetic figures only; real JS and real recorder."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import element_selectors
import page_structure
import rewards_state
import rewards_tasks
import run_record
from fakes import FakeElement
from selenium.webdriver.common.by import By
from test_rewards_state import StateDriver, misc_card


def node(tag, text="", attrs=None, children=()):
	return {"tag": tag, "text": text, "attrs": attrs or {}, "children": list(children)}


def figure(value="12,345", *, label="Synthetic account control", image=True, extra=False, hidden=False):
	# Probe JSON L1347-1445: header > div > button > span > div >
	# div > p + img, with a second nonnumeric badge row. No live value/text.
	row = node("div", children=[node("p", value), *([node("img")] if image else [])])
	rows = [row, node("div", children=[node("p", "Lv")])]
	if extra:
		rows.insert(0, node("div", children=[node("p", "98,765"), node("img")]))
	return node("button", attrs={"aria-label": label, **({"hidden": ""} if hidden else {})},
		children=[node("span", children=[node("div", children=rows)])])


def tree(buttons=(), *, earn=True, section="moreactivities", hidden=False, empty=False):
	children = [node("header", children=[node("div", children=buttons)])]
	if earn:
		children.append(node("a", "Earn", {"id": "react-aria-tab-/earn"}))
	if section:
		children.append(node("section", "" if empty else "Synthetic content", {"id": section, **({"hidden": ""} if hidden else {})},
			children=[] if empty else [node("button" if section == "streaks" else "a", "Synthetic item")]))
	return node("document", children=[node("body", children=children)])


def javascript(script, dom, *, args=(), url="https://rewards.bing.com/earn", ready_state="complete"):
	env = {key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")}
	process = subprocess.run([shutil.which("node"), str(ROOT / "tests/fixtures/reader_header_dom.js")],
		input=json.dumps({"script": script, "tree": dom, "args": args, "url": url, "ready_state": ready_state}),
		text=True, encoding="utf-8", capture_output=True, timeout=8, env=env)
	if process.returncode:
		raise AssertionError(process.stderr)
	packet = json.loads(process.stdout)
	assert packet["effects"] == 0
	return packet["result"]


class NativeHeaderTests(unittest.TestCase):
	def balance(self, *buttons):
		packet = javascript(element_selectors._BALANCE_SNAPSHOT, tree(buttons),
			args=[list(element_selectors._BALANCE_PATTERNS), element_selectors._BALANCE_NUMBER])
		class Driver:
			def execute_script(self, script, *args):
				return packet
		return rewards_state.read_balance(Driver(), element_selectors.ElementSelectionUtils(Driver())), packet

	def test_new_figure_is_found_without_matching_aria_label_language(self):
		for value, expected in (("12,345", 12345), ("12.345", 12345), ("12\u202f345", 12345), ("0", 0)):
			with self.subTest(value=value):
				result, _ = self.balance(figure(value, label="Synthetisches Konto"))
				self.assertEqual(result, expected)

	def test_profile_only_without_digits_is_unknown_and_old_profile_is_preserved(self):
		self.assertIsNone(self.balance(node("button", attrs={"aria-label": "View profile"}, children=[node("span")]))[0])
		self.assertEqual(self.balance(node("button", attrs={"aria-label": "View profile"}, children=[node("span", "12,345")]))[0], 12345)

	def test_badge_does_not_override_structured_figure(self):
		self.assertEqual(self.balance(figure(), node("button", "Level 2", {"aria-label": "View profile"}))[0], 12345)

	def test_missing_structure_loading_or_conflicting_figures_never_guess(self):
		for buttons in ((figure(image=False),), (figure(label=""),), (figure("Loading..."),), (figure(extra=True),),
			(figure(), figure("98,765")), (figure(hidden=True),), (figure("+10"),), (figure("5/60"),)):
			with self.subTest(buttons=buttons):
				self.assertIsNone(self.balance(*buttons)[0])

	def test_numeric_name_outside_observed_paragraph_does_not_become_balance(self):
		self.assertIsNone(self.balance(node("button", "Private Name 42", {"aria-label": "Account"}))[0])

	def test_real_structure_snapshot_removes_all_seeded_page_values(self):
		dom = tree([figure("987,654", label="Private Name user@example.invalid")])
		packet = javascript(page_structure.SNAPSHOT, dom)
		encoded = json.dumps(packet)
		for secret in ("Private", "user@example.invalid", "987", "Synthetic content"):
			self.assertNotIn(secret, encoded)
		self.assertEqual(packet["matched"]["header_figures"], 1)

	def test_earn_structure_requires_correct_location_tab_and_visible_hydrated_content(self):
		for section in ("moreactivities", "quests", "streaks"):
			self.assertTrue(javascript(page_structure.SNAPSHOT, tree(section=section))["rendered"])
		for dom, url, state in ((tree(earn=False), rewards_state.EARN_URL, "complete"),
			(tree(section=None), rewards_state.EARN_URL, "complete"), (tree(hidden=True), rewards_state.EARN_URL, "complete"),
			(tree(empty=True), rewards_state.EARN_URL, "complete"), (tree(), "https://rewards.bing.com/dashboard", "complete"),
			(tree(), "https://login.live.com/earn", "complete"), (tree(), rewards_state.EARN_URL, "loading")):
			with self.subTest(url=url, state=state, dom=dom):
				self.assertFalse(javascript(page_structure.SNAPSHOT, dom, url=url, ready_state=state)["rendered"])


class PartialStateDriver(StateDriver):
	def get(self, url):
		super().get(url)
		if url == rewards_state.EARN_URL:
			self.children[(By.TAG_NAME, "button")] = [self.daily_opener]

	def execute_script(self, script, *args):
		if script == page_structure.SNAPSHOT:
			return javascript(script, tree(), url=self.current_url)
		return super().execute_script(script, *args)


class ReaderIntegrationTests(unittest.TestCase):
	def test_missing_breakdown_does_not_erase_known_cards_daily_or_quests(self):
		driver = PartialStateDriver(daily_done=1, cards=[misc_card(20)], quests={"/earn/quest/week": [FakeElement(text="Activity", attributes={"href": "https://bing.com/search?q=synthetic"})]})
		with mock.patch.object(rewards_state.time, "sleep", return_value=None):
			state = rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)
		self.assertEqual((state.daily_set_open, state.cards_open, state.quests_open, state.open_points), (2, 1, 1, 40))
		self.assertEqual(state.unreadable, "searches")
		self.assertEqual(state.balance, 12345)
		self.assertIsNone(state.searches_earned)
		self.assertIsNone(state.searches_max)
		self.assertTrue(rewards_state.worth_running(state, 15))

	def test_truly_blank_stays_page_blank(self):
		driver = StateDriver(blank_loads={1, 2})
		with mock.patch.object(rewards_state.time, "sleep", return_value=None):
			self.assertEqual(rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0).unreadable, "page_blank")
		self.assertEqual(driver.clicks, [])

	def test_missing_balance_and_search_sources_keep_known_cards_not_zeroed_readings(self):
		driver = PartialStateDriver(balance="Loading...", cards=[misc_card(20)])
		with mock.patch.object(rewards_state.time, "sleep", return_value=None):
			state = rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)
		self.assertEqual((state.cards_open, state.open_points, state.unreadable), (1, 20, "searches,balance"))
		self.assertIsNone(state.balance)
		self.assertIsNone(state.searches_max)
		self.assertIsNone(state.earned_today)
		self.assertTrue(rewards_state.worth_running(state, 15))

	def test_search_task_does_not_send_any_query_if_quota_source_is_missing(self):
		tasks = rewards_tasks.RewardsTaskUtils.__new__(rewards_tasks.RewardsTaskUtils)
		tasks.read_search_points = mock.Mock(side_effect=element_selectors.ElementNotReady("no breakdown"))
		tasks.run_search_batch = mock.Mock()
		with self.assertRaises(element_selectors.ElementNotReady):
			tasks.complete_required_searches()
		tasks.run_search_batch.assert_not_called()

	def test_one_durable_value_free_snapshot_per_account_when_reads_fail(self):
		with tempfile.TemporaryDirectory() as folder:
			recorder = run_record.open_run("cli", ["synthetic"], {}, [], run_id="20261105T180000Z-f135", directory=folder)
			try:
				class Driver:
					def execute_script(self, script):
						packet = javascript(script, tree())
						packet.update(text="SECRET user@example.invalid 987654", balance=987654)
						return packet
				with run_record.account_context("synthetic"):
					page_structure.capture_once(Driver(), "page_blank")
					page_structure.capture_once(Driver(), "balance")
				rows = [row for row in run_record.read_events(recorder.path) if row["type"] == "reader_structure"]
				self.assertEqual(len(rows), 1)
				self.assertEqual((rows[0]["reason"], rows[0]["status"]), ("page_blank", "observed"))
				self.assertTrue(rows[0]["rendered"])
				self.assertNotIn("SECRET", json.dumps(rows))
				self.assertNotIn("987654", json.dumps(rows))
			finally:
				run_record.close_run("checked", 0)

	def test_page_blank_and_balance_failures_reach_real_record_once(self):
		for reason in ("page_blank", "balance"):
			with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
				recorder = run_record.open_run("cli", ["synthetic"], {}, [], run_id="20261105T180000Z-f136", directory=folder)
				try:
					driver = StateDriver(blank_loads={1, 2} if reason == "page_blank" else (), balance="Loading...")
					with run_record.account_context("synthetic"), mock.patch.object(rewards_state.time, "sleep", return_value=None):
						state = rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)
					self.assertIn(reason, state.unreadable)
					rows = [row for row in run_record.read_events(recorder.path) if row["type"] == "reader_structure"]
					self.assertEqual(len(rows), 1)
					self.assertEqual((rows[0]["reason"], rows[0]["status"]), (reason, "unavailable"))
				finally:
					run_record.close_run("checked", 0)


if __name__ == "__main__":
	unittest.main()
