"""App issue1: identity and safe re-finding through real selectors/task loop."""
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait as RealWait
import element_selectors
import check_selectors
import rewards_tasks
import run_record
from fakes import FakeDriver, FakeElement

HEADINGS = 'h1, h2, h3, h4, h5, h6, [role="heading"]'
DENNIS = ("Bing Search Streak", "Daily Set Streak", "Bing App Streak", "Visual Search Streak")
SLIMDEBUG = ("Bing Search Streak", "Daily Set Streak", "Edge Browsing Streak", "Bing App Streak")


def tile(title, counter="Activity: 0/1"):
	return FakeElement(text=title + "\nDay 0 of 7\n" + counter)


def panel(title="Visual Search Streak", links=()):
	return FakeElement(attributes={"id": "react-aria-synthetic"}, children={
		(By.CSS_SELECTOR, HEADINGS): [FakeElement(text=title)] if title is not None else [], (By.TAG_NAME, "a"): list(links)})


class Harness:
	def __init__(self, titles=DENNIS, counter="Activity: 0/1", title="Visual Search Streak", action="Search now", stale="", upload="ok", consent=False):
		self.tiles = [tile(value, counter) for value in titles]
		self.action = FakeElement(text=action)
		self.camera = FakeElement(text="camera")
		self.earn = FakeElement(text="Earn")
		self.driver = FakeDriver(children={(By.ID, "streaks"): [FakeElement(children={(By.TAG_NAME, "button"): [FakeElement(text="1,000 BONUS"), *self.tiles]})],
			(By.TAG_NAME, "button"): self.tiles,
			(By.CSS_SELECTOR, '[id$="-tab-/earn"]'): [self.earn],
			(By.TAG_NAME, "section"): [panel(title, [self.action])],
			(By.CSS_SELECTOR, "#sb_form > div.camera.icon"): [self.camera]})
		self.driver.current_url = "https://rewards.bing.com/earn"
		self.driver.current_window_handle = "main"
		self.driver.window_handles = ["main", "activity"]
		self.clicked, self.upload_calls, self.stale_calls = [], 0, 0
		self.last = None
		harness = self
		class Upload(FakeElement):
			def send_keys(inner, value):
				harness.upload_calls += 1
				if upload in ("ok", "stale_after_effect"):
					harness.driver.current_url = "https://www.bing.com/images/search?synthetic=uploaded"
				if upload.startswith("stale"):
					raise StaleElementReferenceException("input replaced")
		self.driver.children[(By.CSS_SELECTOR, "#sb_fileinput")] = [Upload()]
		self.tasks = rewards_tasks.RewardsTaskUtils.__new__(rewards_tasks.RewardsTaskUtils)
		self.tasks.driver = self.driver
		self.tasks.elements = element_selectors.ElementSelectionUtils(self.driver)
		self.tasks.progress = None
		self.tasks.balance_reader = lambda: 100
		self.tasks.restore_main_tab = lambda: None
		self.tasks.return_to_rewards_home = lambda: None
		def move(control):
			self.last = control
			if control is self.action and stale in ("once", "always"):
				self.stale_calls += 1
				if stale == "always" or self.stale_calls == 1:
					raise StaleElementReferenceException("panel replaced before pointer click")
		self.tasks.mouse = types.SimpleNamespace(move_to_element=move, human_like_click=lambda: self.clicked.append(self.last))
		def switch():
			self.driver.current_url = "https://www.bing.com/visualsearch"
		self.tasks.tab_utils = types.SimpleNamespace(switch_to_other_tab=switch, close_all_other_tabs=lambda: None)
		if consent:
			button = FakeElement(text="Accept")
			self.driver.children[(By.ID, "bnp_btn_accept")] = [button]
			def script(script, target):
				self.driver.children[(By.ID, "bnp_btn_accept")] = []
			self.driver.execute_script = script

	def run(self):
		with tempfile.TemporaryDirectory() as folder:
			recorder = run_record.open_run("app", ["synthetic"], {}, [], run_id="20261111T160000Z-0153", directory=folder)
			try:
				with mock.patch.object(rewards_tasks, "VISUAL_SEARCH_IMAGE_PATH", __file__), mock.patch.object(rewards_tasks.time, "sleep", return_value=None), mock.patch.object(rewards_tasks, "WebDriverWait", lambda driver, timeout: RealWait(driver, 0)), run_record.account_context("synthetic"), self.logs():
					self.tasks.complete_all_tasks(only={"visual_search"})
				return next(event for event in run_record.read_events(recorder.path) if event["type"] == "task_end")
			finally:
				run_record.close_run("partial", 0)

	@staticmethod
	def logs():
		return mock.patch.object(rewards_tasks.logger, "info")


class VisualIdentityTests(unittest.TestCase):
	def test_loading_heading_settles_then_task_records_ok(self):
		h = Harness(title="Loading...")
		section = h.driver.children[(By.TAG_NAME, "section")][0]
		find = section.find_elements
		reads = []
		def settle(by, selector):
			if (by, selector) == (By.CSS_SELECTOR, HEADINGS):
				reads.append(None)
				section.children[(by, selector)][0].text = "Loading..." if len(reads) < 3 else "Visual Search Streak"
			return find(by, selector)
		section.find_elements = settle
		wait = RealWait
		with mock.patch(__name__ + ".RealWait", lambda driver, timeout: wait(driver, 0.2, poll_frequency=0.01)):
			end = h.run()
		self.assertEqual((end["outcome"], end["reason_key"]), ("ok", None))
		self.assertEqual(h.clicked[:3], [h.earn, h.tiles[3], h.action])

	def test_loading_forever_is_not_ready_without_activity_click(self):
		for title in ("Loading...", ""):
			with self.subTest(title=title):
				h = Harness(title=title)
				end = h.run()
				self.assertEqual((end["outcome"], end["reason_key"]), ("fail", "not_ready"))
				self.assertEqual(end["detail"], "opened the sidebar, then visual search state: panel_action_missing")
				self.assertNotIn("expected Visual Search Streak panel", end["detail"])
				self.assertEqual(h.clicked, [h.earn, h.tiles[3]])

	def test_busy_and_visible_spinner_cannot_authorize_panel_actions(self):
		for variant in ("busy", "spinner"):
			with self.subTest(variant=variant):
				h = Harness()
				section = h.driver.children[(By.TAG_NAME, "section")][0]
				if variant == "busy":
					section.attributes["aria-busy"] = "true"
				else:
					section.children[(By.CSS_SELECTOR, '[aria-busy="true"], [role="progressbar"], [role="spinner"], [role="status"]')] = [FakeElement(attributes={"role": "progressbar"})]
				with self.assertRaises(element_selectors.ElementNotReady):
					h.tasks.elements.get_search_now_link_from_visual_search_sidebar()

	def test_absent_tile_skips_without_clicking_app_or_generating_image(self):
		h = Harness(titles=SLIMDEBUG)
		end = h.run()
		self.assertEqual((end["outcome"], end["reason_key"]), ("skip", "not_available"))
		self.assertIn("Bing App Streak", end["detail"])
		self.assertEqual(h.clicked, [h.earn])

	def test_done_counter_no_panel_or_upload(self):
		h = Harness(counter="Activity: 1/1")
		end = h.run()
		self.assertEqual(end["outcome"], "ok")
		self.assertEqual(h.clicked, [h.earn])
		self.assertEqual(h.upload_calls, 0)

	def test_wrong_panel_even_with_search_label_does_not_click_action(self):
		h = Harness(title="Bing App Streak")
		end = h.run()
		self.assertEqual(end["outcome"], "fail")
		self.assertIn("Bing App Streak", end["detail"])
		self.assertNotIn(h.action, h.clicked)

	def test_panel_without_heading_and_activate_action_are_supported(self):
		h = Harness(title=None, action="Activate streak")
		self.assertEqual(h.run()["outcome"], "ok")
		self.assertIn(h.action, h.clicked)
		self.assertEqual(h.upload_calls, 1)

	def test_panel_rerender_before_click_is_refound_and_continues(self):
		h = Harness(stale="once")
		self.assertEqual(h.run()["outcome"], "ok")
		self.assertEqual(h.stale_calls, 2)
		self.assertEqual(h.clicked.count(h.action), 1)

	def test_persistent_panel_change_has_named_state_and_getter(self):
		h = Harness(stale="always")
		end = h.run()
		self.assertEqual(end["outcome"], "fail")
		self.assertIn("panel_action_changed_or_missing", end["detail"])
		self.assertEqual(end["reason_key"], "not_ready")
		self.assertEqual(end["element_wait"], {"getter": "get_search_now_link_from_visual_search_sidebar"})
		self.assertNotIn(h.action, h.clicked)

	def test_missing_action_does_not_take_second_unrelated_link(self):
		h = Harness(action="Check-in now")
		h.driver.children[(By.TAG_NAME, "section")][0].children[(By.TAG_NAME, "a")].append(FakeElement(text="Get app"))
		end = h.run()
		self.assertEqual(end["reason_key"], "not_ready")
		self.assertIn("panel_action_missing", end["detail"])
		self.assertNotIn(h.action, h.clicked)
		self.assertEqual(end["element_wait"]["getter"], "get_search_now_link_from_visual_search_sidebar")

	def test_upload_stale_response_never_resends_unknown_effect(self):
		for mode, expected in (("stale_unconfirmed", "fail"), ("stale_after_effect", "ok")):
			with self.subTest(mode=mode):
				h = Harness(upload=mode)
				end = h.run()
				self.assertEqual(end["outcome"], expected)
				self.assertEqual(h.upload_calls, 1)
				if expected == "fail":
					self.assertIn("upload_input_changed_unconfirmed", end["detail"])

	def test_upload_without_navigation_is_unconfirmed_not_false_ok(self):
		h = Harness(upload="no_effect")
		end = h.run()
		self.assertEqual(end["outcome"], "fail")
		self.assertIn("upload_not_confirmed", end["detail"])
		self.assertEqual(h.upload_calls, 1)

	def test_consent_banner_uses_existing_dismissal_then_proceeds(self):
		h = Harness(consent=True)
		self.assertEqual(h.run()["outcome"], "ok")
		self.assertEqual(h.upload_calls, 1)

	def test_hidden_upload_input_remains_supported(self):
		h = Harness()
		h.driver.children[(By.CSS_SELECTOR, "#sb_fileinput")][0].displayed = False
		self.assertEqual(h.run()["outcome"], "ok")
		self.assertEqual(h.upload_calls, 1)

	def test_upload_input_replaced_before_send_is_refound_without_duplicate_send(self):
		h = Harness()
		control = h.driver.children[(By.CSS_SELECTOR, "#sb_fileinput")][0]
		base = control.get_dom_attribute
		reads = []
		def attribute(name):
			reads.append(name)
			if len(reads) == 1:
				raise StaleElementReferenceException("input replaced before send")
			return base(name)
		control.get_dom_attribute = attribute
		# A two-poll wait without a real clock delay exercises re-finding.
		class TwoPollWait:
			def __init__(self, driver, timeout):
				self.driver = driver
			def until(self, predicate):
				value = predicate(self.driver)
				return value or predicate(self.driver)
		with mock.patch.object(rewards_tasks, "WebDriverWait", TwoPollWait):
			with mock.patch.object(rewards_tasks, "VISUAL_SEARCH_IMAGE_PATH", __file__), mock.patch.object(rewards_tasks.time, "sleep", return_value=None):
				h.tasks.complete_visual_search()
		self.assertEqual(h.upload_calls, 1)
		self.assertEqual(len(reads), 2)

	def test_tile_title_scope_is_normalized_substring_and_order_independent(self):
		h = Harness()
		section = h.driver.children[(By.ID, "streaks")][0]
		section.children[(By.TAG_NAME, "button")].reverse()
		visual = h.tiles[-1]
		visual.text = "Start  VISUAL  Search\tStreak today\nActivity: 0/1"
		self.assertIs(h.tasks.elements.get_open_visual_search_sidebar(), visual)
		section.children[(By.TAG_NAME, "button")] = [tile("Bing App Streak")]
		h.driver.children[(By.TAG_NAME, "button")] = [tile("Visual Search Streak")]
		with self.assertRaises(element_selectors.VisualSearchUnavailable):
			h.tasks.elements.get_open_visual_search_sidebar()

	def test_unrendered_row_fails_not_skip(self):
		h = Harness()
		h.driver.children[(By.ID, "streaks")] = []
		self.assertEqual(h.run()["reason_key"], "not_ready")
		self.assertEqual(h.clicked, [h.earn])

	def test_completed_day_seven_and_reset_day_zero_use_only_daily_counter(self):
		for counter, done in (("Activity: 1/1", True), ("Activity: 0/1", False), ("Activity: 0/0", False)):
			h = Harness(counter=counter)
			h.tiles[-1].text = "Visual Search Streak\nDay 7 of 7 streak completed.\n" + counter
			self.assertEqual(h.tasks.elements.visual_search_tile_done_today(), done)

	def test_missing_action_diagnostic_is_durable_and_value_free(self):
		h = Harness(title=None, action="PRIVATE user@example.invalid https://synthetic.invalid/?token=PRIVATE")
		with mock.patch.object(run_record, "emit", wraps=run_record.emit) as emit:
			end = h.run()
		calls = [call.kwargs for call in emit.call_args_list if call.args == ("visual_search_panel",)]
		self.assertEqual(calls, [{"state": "panel_action_missing", "links": 1, "buttons": 0, "role_links": 0}])
		self.assertEqual(end["reason_key"], "not_ready")
		self.assertNotIn("PRIVATE", end["detail"])

	def test_action_removed_between_wait_and_click_has_named_state(self):
		h = Harness()
		base = h.tasks.elements.get_search_now_link_from_visual_search_sidebar
		reads = []
		def disappearing():
			reads.append(None)
			if len(reads) >= 3:
				raise NoSuchElementException("removed between wait and click")
			return base()
		h.tasks.elements.get_search_now_link_from_visual_search_sidebar = disappearing
		end = h.run()
		self.assertEqual(end["reason_key"], "not_ready")
		self.assertIn("panel_action_changed_or_missing", end["detail"])
		self.assertNotIn(h.action, h.clicked)

	def test_missing_camera_and_upload_input_have_named_getters_not_page_changed(self):
		for selector, state in (("#sb_form > div.camera.icon", "camera_changed_or_missing"), ("#sb_fileinput", "upload_input_missing")):
			with self.subTest(selector=selector):
				h = Harness()
				h.driver.children[(By.CSS_SELECTOR, selector)] = []
				end = h.run()
				self.assertEqual(end["reason_key"], "not_ready")
				self.assertIn(state, end["detail"])
				self.assertIn("element_wait", end)
				self.assertEqual(h.upload_calls, 0)

	def test_search_page_tab_never_appears_is_named_camera_state(self):
		h = Harness()
		h.tasks.tab_utils.switch_to_other_tab = lambda: None
		h.driver.children[(By.CSS_SELECTOR, "#sb_form > div.camera.icon")] = []
		end = h.run()
		self.assertIn("camera_changed_or_missing", end["detail"])
		self.assertEqual(end["outcome"], "fail")

	def test_cookie_overlay_that_swallows_upload_is_not_reported_ok(self):
		h = Harness()
		h.tasks.dismiss_cookie_consent = lambda: False
		h.driver.children[(By.CSS_SELECTOR, "#sb_fileinput")][0].send_keys = lambda path: None
		end = h.run()
		self.assertEqual(end["outcome"], "fail")
		self.assertIn("upload_not_confirmed", end["detail"])

	def test_disabled_panel_done_marker_is_ok_without_action(self):
		h = Harness(action="Other control")
		h.driver.children[(By.TAG_NAME, "section")][0].children[(By.CSS_SELECTOR, '[role="link"]')] = [FakeElement(text="Search now", attributes={"aria-disabled": "true"})]
		end = h.run()
		self.assertEqual(end["outcome"], "ok")
		self.assertNotIn(h.action, h.clicked)
		self.assertEqual(h.upload_calls, 0)

	def test_real_panel_reference_is_replaced_and_refound_after_stale(self):
		h = Harness()
		replacement = FakeElement(text="Search now")
		base = h.tasks.mouse.move_to_element
		def move(control):
			if control is h.action:
				h.driver.children[(By.TAG_NAME, "section")] = [panel(links=[replacement])]
				raise StaleElementReferenceException("old panel removed")
			base(control)
		h.tasks.mouse.move_to_element = move
		end = h.run()
		self.assertEqual(end["outcome"], "ok")
		self.assertNotIn(h.action, h.clicked)
		self.assertIn(replacement, h.clicked)
		self.assertEqual(h.upload_calls, 1)

	def test_replaced_panel_identified_as_wrong_is_not_clicked_during_retry(self):
		h = Harness()
		base = h.tasks.mouse.move_to_element
		def move(control):
			if control is h.action:
				h.driver.children[(By.TAG_NAME, "section")] = [panel("Bing App Streak", [FakeElement(text="Search now")])]
				raise StaleElementReferenceException("panel replaced by other streak")
			base(control)
		h.tasks.mouse.move_to_element = move
		end = h.run()
		self.assertEqual(end["outcome"], "fail")
		self.assertIn("Bing App Streak", end["detail"])
		self.assertEqual(h.upload_calls, 0)

	def test_check_selectors_preserves_absent_vs_unready_and_driver_failure(self):
		for error, status in ((element_selectors.ElementNotReady("not rendered"), "FAILED"),
			(element_selectors.VisualSearchUnavailable("shown tiles: Bing App Streak"), "ABSENT"),
			(RuntimeError("driver broke"), "FAILED")):
			with self.subTest(error=type(error).__name__):
				report = mock.Mock()
				elements = types.SimpleNamespace(get_open_visual_search_sidebar=mock.Mock(side_effect=error))
				check_selectors._check_visual_search(report, elements)
				self.assertEqual(report.record.call_args.args[:2], ("get_open_visual_search_sidebar", status))
				report.check.assert_not_called()


if __name__ == "__main__":
	unittest.main()
