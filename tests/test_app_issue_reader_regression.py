"""Issue3-reader regression surface already fixed in0.3.4/0.3.5.

Synthetic values, shape based on F135's value-free live probe, not saved HTML.
These tests protect the signature that the sign-in owner consumes.
"""
import inspect
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import element_selectors
import rewards_state
from test_reader_header import figure, javascript, tree, node, PartialStateDriver
from test_rewards_state import StateDriver, misc_card
from test_earn_reload import RenderDriver


class ReporterReaderRegression(unittest.TestCase):
	def test_new_header_known_figure_then_profile_without_digits_remains_unknown(self):
		class Driver:
			current_url = rewards_state.EARN_URL
			def __init__(self, dom):
				self.dom = dom
			def execute_script(self, script, *args):
				return javascript(script, self.dom, args=args)
		driver = Driver(tree([figure("12,345")]))
		selectors = element_selectors.ElementSelectionUtils(driver)
		self.assertEqual(rewards_state.read_balance(driver, selectors), 12345)
		driver.dom = tree([node("button", attrs={"aria-label": "View profile"}, children=[node("span")])])
		self.assertIsNone(rewards_state.read_balance(driver, selectors))

	def test_rendered_cards_without_breakdown_do_not_claim_page_blank_or_daily_total(self):
		driver = PartialStateDriver(cards=[misc_card(20)])
		with mock.patch.object(rewards_state.time, "sleep", return_value=None):
			state = rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)
		self.assertEqual((state.unreadable, state.cards_open, state.balance), ("searches", 1, 12345))
		self.assertIsNone(state.earned_today)
		self.assertIsNone(state.searches_max)

	def test_partial_render_recovers_once_but_persistent_partial_stays_unknown(self):
		for second, expected in (("full", None), ("a", "searches,daily_set,cards,quests,balance")):
			with self.subTest(second=second):
				driver = RenderDriver(second=second)
				with mock.patch.object(rewards_state.time, "sleep", return_value=None):
					state = rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)
				self.assertEqual(state.unreadable, expected)
				self.assertEqual(driver.earn_loads, 5)
				if expected:
					self.assertIsNone(state.balance)
					self.assertIsNone(state.earned_today)

	def test_unreadable_balance_is_not_equivalent_to_signed_out_and_api_is_unchanged(self):
		driver = StateDriver(balance="Loading...", cards=[misc_card(20)])
		with mock.patch.object(rewards_state.time, "sleep", return_value=None):
			state = rewards_state.read_state(driver, element_selectors.ElementSelectionUtils(driver), timeout=0)
		self.assertEqual(state.unreadable, "balance")
		self.assertEqual(state.earned_today, 2345)
		self.assertIsNone(state.balance)
		self.assertEqual(str(inspect.signature(rewards_state.read_state)), "(driver, elements, timeout: float = 30, skip_cards: frozenset[str] = frozenset(), *, phase: str = 'before') -> rewards_state.RewardsState")
		self.assertEqual(str(inspect.signature(rewards_state.read_balance)), "(driver, elements) -> int | None")


if __name__ == "__main__":
	unittest.main()
