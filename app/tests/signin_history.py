"""Manual reproduction: python -m unittest signin_history from app/tests.

Requires the named historical commits, so shallow CI checkouts do not discover
this helper. The normal regression tests need neither Git nor old sources.
"""
import _bootstrap  # noqa: F401

import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

import test_signin_flow as flows

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from fakes import FakeElement
from selenium.webdriver.common.by import By
from test_reader_header import figure, javascript, tree, PartialStateDriver
from test_rewards_state import misc_card


def historical(commit, path):
	text = subprocess.run(["git", "--no-pager", "show", commit + ":" + path], cwd=ROOT,
		capture_output=True, text=True, encoding="utf-8", check=True, timeout=15).stdout
	module = types.ModuleType("repro_" + commit + "_" + Path(path).stem)
	module.__file__ = str(ROOT / path)
	sys.modules[module.__name__] = module
	exec(compile(text, commit + ":" + path, "exec"), module.__dict__)
	return module


class HistoricalSigninTests(unittest.TestCase):
	setUp = flows.SigninFlowTests.setUp
	state = flows.SigninFlowTests.state

	def test_published_balance_gate_misses_observed_header_current_reader_finds_it(self):
		# This shape is documented by test_reader_header.py and the value-free
		# F135 capture (05 Nov), not a claim about the reporter's actual browser.
		dom = tree([figure("12,345", label="Synthetic account control")])
		class HeaderDriver(flows.FakeDriver):
			def find_elements(self, by, selector):
				if (by, selector) == (By.TAG_NAME, "header"):
					return [FakeElement(children={(By.TAG_NAME,"button"):[FakeElement("12,345\nLv", {"aria-label":"Synthetic account control"})]})]
				return []
			def execute_script(self, script, *args):
				if "const patterns" in script: return javascript(script, dom, args=args)
				return super().execute_script(script)
		rows = []
		for commit in ("305446b", "b9d824b"):
			selectors = historical(commit, "src/element_selectors.py")
			with patch.dict(sys.modules, {"element_selectors":selectors}):
				reader = historical(commit, "src/rewards_state.py")
			module = historical(commit, "app/rfapp/signin.py")
			self.visible, self.verification = HeaderDriver(), flows.FakeDriver()
			self.edge_mock.side_effect = [self.visible, self.verification]
			self.clock = 0
			self.rewards.read_balance.side_effect = reader.read_balance
			self.rewards.read_state.return_value = types.SimpleNamespace(unreadable="signed_out")
			with patch.object(self.elements, "ElementSelectionUtils", side_effect=selectors.ElementSelectionUtils):
				balance = reader.read_balance(self.visible, selectors.ElementSelectionUtils(self.visible))
				exit_code = module.run("account-1", False)
			rows.append(dict(commit=commit, balance=balance, exit=exit_code, state=self.state()["state"], elapsed_fake_seconds=self.clock))
		self.assertEqual(rows[0], dict(commit="305446b", balance=None, exit=1, state="timed_out", elapsed_fake_seconds=900))
		self.assertEqual(rows[1], dict(commit="b9d824b", balance=12345, exit=0, state="signed_in", elapsed_fake_seconds=3))
		print("HEADER_SIGNIN_REPRO=" + json.dumps(rows))

	def test_old_page_blank_verification_is_false_success_in_both_versions(self):
		rows = []
		for commit in ("305446b", "b9d824b"):
			module = historical(commit, "app/rfapp/signin.py")
			self.visible, self.verification = flows.FakeDriver(), flows.FakeDriver()
			self.visible.closed = True
			self.edge_mock.side_effect = [self.visible, self.verification]
			self.rewards.read_state.return_value = types.SimpleNamespace(unreadable="page_blank")
			result = module.run("account-1", False)
			self.assertEqual((result, self.state()["state"]), (0, "signed_in"))
			rows.append(dict(commit=commit, verification="page_blank", exit=result, state=self.state()["state"]))
		print("BLANK_VERIFY_REPRO=" + json.dumps(rows))

	def test_observed_no_breakdown_page_is_blank_in_release_but_partial_in_current(self):
		rows = []
		for commit in ("305446b", "b9d824b"):
			selectors = historical(commit, "src/element_selectors.py")
			with patch.dict(sys.modules, {"element_selectors":selectors}):
				reader = historical(commit, "src/rewards_state.py")
			class ObservedEarnDriver(PartialStateDriver):
				def execute_script(self, script, *args):
					if "const patterns" in script:
						return javascript(script, tree([figure("12,345")]), args=args)
					return super().execute_script(script, *args)
			driver = ObservedEarnDriver(cards=[misc_card(20)])
			with patch.object(reader.time, "sleep", return_value=None):
				state = reader.read_state(driver, selectors.ElementSelectionUtils(driver), timeout=0)
			rows.append(dict(commit=commit, unreadable=state.unreadable, cards=state.cards_open, balance=state.balance))
		self.assertEqual(rows[0]["unreadable"], "page_blank")
		self.assertEqual((rows[1]["unreadable"], rows[1]["cards"]), ("searches", 1))
		print("HEADLESS_READER_REPRO=" + json.dumps(rows))


if __name__ == "__main__":
	unittest.main()
