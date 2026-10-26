import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import identity
import main
import rewards_state
import run_record


NAME = "Synthetic Person"


def driver(text="Welcome, Synthetic Person!", url="https://rewards.bing.com/dashboard", visible=True):
	value = SimpleNamespace(current_url=url, get=Mock(), find_elements=Mock(return_value=[SimpleNamespace(text=text, is_displayed=lambda: visible)]))
	value.get.side_effect = lambda target: setattr(value, "current_url", "https://rewards.bing.com/dashboard")
	return value


class IdentityTests(unittest.TestCase):
	def test_match_casefold_whitespace_and_hash_never_name(self):
		for expected in ("Synthetic Person", " SYNTHETIC\tPerson "):
			browser = driver("  Welcome,\n SYNTHETIC  Person! ")
			status, digest = identity.read_dashboard_identity(browser, expected)
			self.assertEqual(status, "match")
			self.assertEqual(digest, hashlib.sha256(b"synthetic person").hexdigest())
			browser.get.assert_not_called()
			browser.find_elements.assert_called_once_with("css selector", "p.pii")
			self.assertEqual(identity.read_dashboard_identity(browser, expected_name_hash=digest), ("match", digest))

	def test_mismatch_unknown_absent_ambiguous_hidden_never_match(self):
		self.assertEqual(identity.read_dashboard_identity(driver("Welcome, Someone Else!"), NAME), ("mismatch", None))
		for browser in (driver(""), driver(visible=False)):
			self.assertEqual(identity.read_dashboard_identity(browser, NAME), ("unknown", None))
		browser = driver()
		for nodes in ([], [SimpleNamespace(text=NAME, is_displayed=lambda: True)] * 2):
			browser.find_elements.return_value = nodes
			self.assertEqual(identity.read_dashboard_identity(browser, NAME), ("unknown", None))
		browser.find_elements.side_effect = RuntimeError(NAME)
		self.assertEqual(identity.read_dashboard_identity(browser, NAME), ("unknown", None))

	def test_one_navigation_only_when_not_dashboard_no_menu_clicks(self):
		browser = driver(url="https://www.bing.com/")
		self.assertEqual(identity.read_dashboard_identity(browser, NAME)[0], "match")
		browser.get.assert_called_once_with("https://rewards.bing.com/")
		browser = driver(url="https://login.live.com/")
		browser.get.side_effect = None
		self.assertEqual(identity.read_dashboard_identity(browser, NAME), ("unknown", None))
		browser.find_elements.assert_not_called()

	def test_same_name_limit_is_explicit_and_matching_does_not_prove_unique_person(self):
		self.assertTrue(identity.SAME_NAME_LIMIT)
		for _different_person_with_same_name in range(2):
			self.assertEqual(identity.read_dashboard_identity(driver(), NAME)[0], "match")

	def test_name_not_logged_even_debug_or_exception_and_no_expected_no_get(self):
		with patch.object(logging.Logger, "handle") as log:
			self.assertEqual(identity.read_dashboard_identity(driver(), NAME)[0], "match")
			browser = driver()
			browser.find_elements.side_effect = RuntimeError(NAME)
			self.assertEqual(identity.read_dashboard_identity(browser, NAME), ("unknown", None))
			log.assert_not_called()
		browser = driver()
		self.assertEqual(identity.read_dashboard_identity(browser), ("unknown", None))
		browser.get.assert_not_called()

	def test_binding_requires_explicit_confirmation_and_no_silent_rebind(self):
		with tempfile.TemporaryDirectory() as folder:
			pending = identity.stage_enrollment("account-1", NAME, new=False, directory=folder)
			self.assertIsNone(identity.binding("account-1", folder))
			self.assertEqual(identity.pending("account-1", folder)["token"], pending["token"])
			value = identity.load(folder)
			value["bindings"]["account-1"] = {"account_id": "account-1", "expected_name_hash": identity.name_hash(NAME), "confirmed_at": identity.utc()}
			identity.save(value, folder)
			before = (Path(folder) / "account-identities.json").read_bytes()
			with self.assertRaises(identity.IdentityError): identity.stage_enrollment("account-1", "Another Person", new=False, directory=folder)
			self.assertEqual((Path(folder) / "account-identities.json").read_bytes(), before)

	def test_corrupt_missing_bindings_managed_marker_fail_closed(self):
		with tempfile.TemporaryDirectory() as folder:
			self.assertFalse(identity.managed(folder))
			self.assertEqual(identity.check_account(driver(), "account-1", folder), ("unknown", "binding_missing"))
			(Path(folder) / "account-identities.json").write_text("[]")
			self.assertTrue(identity.managed(folder))
			self.assertEqual(identity.check_account(driver(), "account-1", folder), ("unknown", "binding_unreadable"))
			(Path(folder) / "account-identities.json").unlink()
			(Path(folder) / "app-prefs.json").write_text('{"identity_managed":true}')
			self.assertTrue(identity.managed(folder))


class StrictRunnerTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		self.stack = contextlib.ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.dict(os.environ, {"REWARDS_FARMER_DATA_HOME": str(self.home), "USER_DATA_DIR": str(self.home / "profiles"), "REWARDS_ACCOUNTS": "one,two"}))
		self.stack.enter_context(patch.object(run_record.constants, "RUNS_DIR", str(self.home / "runs")))
		self.stack.enter_context(patch.object(main.log_utils, "setup_logging"))
		self.stack.enter_context(patch.object(main, "HEADLESS", True))
		for name, value in (("prepare_desktop_before_launch", True), ("switch_back_after_launch", None), ("cleanup_virtual_desktop", None), ("reset_virtual_desktop_state", None), ("is_virtual_desktop_enabled", False)):
			self.stack.enter_context(patch.object(main.desktop_utils, name, return_value=value))
		self.browser = Mock()
		self.stack.enter_context(patch.object(main.browser, "start_driver", return_value=self.browser))
		self.tasks = self.stack.enter_context(patch.object(main.rewards_tasks, "RewardsTaskUtils"))
		self.read = self.stack.enter_context(patch.object(main.rewards_state, "read_state", return_value=rewards_state.RewardsState(open_points=40)))
		self.stack.enter_context(patch.object(main, "_record_referrals"))

	def events(self):
		return [event for path in (self.home / "runs").glob("*.jsonl") for event in run_record.read_events(str(path))]

	def test_mismatch_unknown_no_tasks_state_or_retry_and_other_account_continues(self):
		for status in ("mismatch", "unknown"):
			with patch.object(identity, "check_account", side_effect=[(status, "name_mismatch" if status == "mismatch" else "dashboard_unknown"), ("match", "confirmed_name")]) as check:
				self.assertEqual(main.main(True, main.RunPlan(require_identity=True)), 0)
				self.assertEqual(check.call_count, 2)
			self.assertEqual(self.tasks.return_value.complete_all_tasks.call_count, 1)
			self.tasks.reset_mock()
		self.read.assert_not_called()
		events = self.events()
		self.assertNotIn(NAME, json.dumps(events))
		self.assertTrue(all(event["result"] == "partial" for event in events if event["type"] == "run_end"))
		self.assertTrue(any(event.get("error_code") == "identity_mismatch" for event in events))
		self.assertTrue(any(event.get("error_code") == "identity_unknown" for event in events))

	def test_check_only_all_denied_is_failed_even_run_if_unreadable(self):
		with patch.object(identity, "check_account", return_value=("unknown", "binding_missing")), patch.object(run_record, "read_events", side_effect=AssertionError("Outcome must not depend on record readback")):
			self.assertEqual(main.main(True, main.RunPlan(check_only=True, run_if_unreadable=True, require_identity=True)), 1)
		self.tasks.assert_not_called()
		self.read.assert_not_called()
		self.assertEqual(self.events()[-1]["result"], "failed")

	def test_check_only_good_account_and_denied_account_is_partial(self):
		with patch.object(identity, "check_account", side_effect=[("unknown", "binding_missing"), ("match", "confirmed_name")]):
			self.assertEqual(main.main(True, main.RunPlan(check_only=True, require_identity=True)), 0)
		self.assertEqual(self.events()[-1]["result"], "partial")
		self.read.assert_called_once()
		self.tasks.assert_not_called()

	def test_without_flag_no_binding_read_or_identity_navigation(self):
		with patch.object(identity, "check_account", side_effect=AssertionError("legacy untouched")):
			self.assertEqual(main.main(True), 0)
		self.assertEqual(self.tasks.return_value.complete_all_tasks.call_count, 2)

	def test_parser_strict_option_preserves_existing_flags(self):
		with patch.object(main, "main", return_value=0) as run:
			self.assertEqual(main.cli(["--unattended", "--check-only", "--run-if-unreadable", "--require-identity"]), 0)
		self.assertTrue(run.call_args.kwargs["plan"].require_identity)
		self.assertTrue(run.call_args.kwargs["plan"].check_only)
		self.assertTrue(run.call_args.kwargs["plan"].run_if_unreadable)
