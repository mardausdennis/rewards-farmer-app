import _bootstrap  # noqa: F401

import json
import unittest
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from rfapp import signin
import test_signin_flow as flows


class DetectionRegressionTests(unittest.TestCase):
	setUp = flows.SigninFlowTests.setUp
	state = flows.SigninFlowTests.state

	def test_missing_balance_accepts_independent_profile_signal_without_long_wait(self):
		self.rewards.read_balance.return_value = None
		self.visible.auth["profile"] = True
		self.assertEqual(signin.run("account-1", False), 0)
		self.assertEqual(self.state()["state"], "signed_in")
		self.assertLess(self.clock, 10)
		self.rewards.read_state.assert_not_called()

	def test_missing_balance_accepts_dashboard_identity_signal(self):
		self.rewards.read_balance.return_value = None
		self.visible.auth["identity"] = True
		self.assertEqual(signin.run("account-1", False), 0)
		self.assertLess(self.clock, 10)

	def test_rendered_earn_is_independent_of_both_header_variants(self):
		self.rewards.read_balance.return_value = None
		self.visible.url = "https://rewards.bing.com/earn"
		self.visible.structure.update(earn_path=True, rendered=True)
		self.visible.structure["matched"] = {"earn_tab": 1, "streaks": 1}
		self.assertEqual(signin.run("account-1", False), 0)
		self.assertLess(self.clock, 10)

	def test_signin_prompt_overrides_stale_balance_and_profile(self):
		self.visible.auth.update(profile=True, signin=True)
		self.visible.closed = False
		self.assertEqual(signin.run("account-1", False), 1)
		self.assertNotEqual(self.state()["state"], "signed_in")

	def test_terms_page_never_becomes_signed_in_from_stale_balance(self):
		self.visible.url = "https://rewards.bing.com/terms"
		self.visible.auth["profile"] = True
		self.rewards.read_state.return_value = SimpleNamespace(unreadable="terms_of_use")
		self.assertEqual(signin.run("account-1", False), 1)
		self.assertEqual(self.state()["error_code"], "terms_of_use")
		self.assertEqual(self.state()["message_key"], "errors.state.terms_of_use")

	def test_blank_headless_page_is_unknown_not_a_saved_account(self):
		self.visible.closed = True
		self.rewards.read_balance.return_value = None
		self.verification.url = "https://rewards.bing.com/"
		self.rewards.read_state.return_value = SimpleNamespace(unreadable="page_blank")
		self.assertEqual(signin.run("account-1", True), 1)
		self.assertEqual(self.state()["state"], "failed")
		self.assertEqual(self.state()["message_key"], "accounts.signin.failed")
		self.assertNotIn("REWARDS_ACCOUNTS", (self.root / ".env").read_text())
		self.admin._recycle.assert_not_called()

	def test_unknown_rendered_page_finishes_with_value_free_structure(self):
		self.rewards.read_balance.return_value = None
		self.visible.structure["counts"] = {"buttons": 25, "sections": 10, "private": "SYNTHETIC-SECRET"}
		self.verification.structure = self.visible.structure
		self.verification.url = "https://rewards.bing.com/"
		self.rewards.read_state.return_value = SimpleNamespace(unreadable="page_blank")
		with self.assertLogs("rfapp", "WARNING") as log:
			self.assertEqual(signin.run("account-1", False), 1)
		self.assertLess(self.clock, 120)
		self.assertEqual(self.state()["error_code"], "page_unrecognized")
		self.assertIn('"buttons": 25', " ".join(log.output))
		self.assertNotIn("SYNTHETIC-SECRET", json.dumps(self.state()) + " ".join(log.output))
		self.assertEqual(self.state()["diagnostic"]["structure"]["counts"]["buttons"], 25)
		self.rewards.read_state.assert_called_once_with(self.verification, self.verification, timeout=0)

	def test_rewards_lookalike_or_insecure_host_cannot_use_positive_signals(self):
		for url in ("http://rewards.bing.com/", "https://rewards.bing.com.example/", "https://rewards.bing.com@evil.example/"):
			with self.subTest(url=url):
				self.visible.url = url
				self.assertEqual(signin._detection(self.visible, self.rewards, None)[0], "signed_out")

	def test_existing_same_account_resignin_keeps_binding_and_never_recycles(self):
		import identity
		(self.root / ".env").write_text("REWARDS_ACCOUNTS=account-1\nUSER_DATA_DIR=" + str(self.profile) + "\n")
		value = identity.load(self.root)
		value["bindings"]["account-1"] = {"account_id":"account-1", "expected_name_hash":identity.name_hash("Synthetic Person"), "confirmed_at":identity.utc()}
		identity.save(value, self.root)
		before = (self.root / "account-identities.json").read_bytes()
		self.rewards.read_balance.return_value = None
		self.visible.auth["profile"] = True
		self.assertEqual(signin.run("account-1", False), 0)
		self.assertEqual((self.root / "account-identities.json").read_bytes(), before)
		self.admin._recycle.assert_not_called()
		self.admin.migrate_root_profile.assert_not_called()

	def test_signed_out_snapshot_overrides_positive_verification(self):
		self.visible.closed = True
		self.verification.url = "https://rewards.bing.com/"
		self.verification.auth["profile"] = True
		self.assertEqual(signin.run("account-1", False), 1)
		self.assertEqual(self.state()["state"], "cancelled")

	def test_cancel_during_verification_cannot_register_new_account(self):
		self.visible.closed = True
		self.verification.url = "https://rewards.bing.com/"
		def reading(*args, **kwargs):
			signin._cancel_path("account-1").touch()
			return SimpleNamespace(unreadable=None)
		self.rewards.read_state.side_effect = reading
		self.assertEqual(signin.run("account-1", True), 1)
		self.assertEqual(self.state()["state"], "cancelled")
		self.assertNotIn("REWARDS_ACCOUNTS", (self.root / ".env").read_text())

	def test_cancel_during_save_grace_period_cannot_register_new_account(self):
		def sleep(seconds):
			self.clock += seconds
			signin._cancel_path("account-1").touch()
		with patch.object(signin.time, "sleep", side_effect=sleep):
			self.assertEqual(signin.run("account-1", True), 1)
		self.assertEqual(self.state()["state"], "cancelled")
		self.assertNotIn("REWARDS_ACCOUNTS", (self.root / ".env").read_text())

	def test_existing_profile_and_binding_survive_failed_resignin(self):
		import identity
		profile = self.profile / "account-1"
		profile.mkdir()
		(profile / "saved-session").write_text("synthetic")
		value = identity.load(self.root)
		value["bindings"]["account-1"] = {"account_id":"account-1", "expected_name_hash":identity.name_hash("Synthetic Person"), "confirmed_at":identity.utc()}
		identity.save(value, self.root)
		before = (self.root / "account-identities.json").read_bytes()
		self.visible.closed = True
		self.assertEqual(signin.run("account-1", False), 1)
		self.assertEqual((self.root / "account-identities.json").read_bytes(), before)
		self.assertEqual((profile / "saved-session").read_text(), "synthetic")
		self.admin._recycle.assert_not_called()

	def test_profile_signal_does_not_bypass_enrollment_identity_confirmation(self):
		import identity
		self.rewards.read_balance.return_value = None
		self.visible.auth["profile"] = True
		self.admin._profile_identity = lambda account: {"name":"Synthetic Person"}
		with patch.object(identity, "read_dashboard_identity", return_value=("unknown", None)):
			self.assertEqual(signin.run("account-1", True, enroll=True), 1)
		self.assertEqual(self.state()["error_code"], "identity_unknown")
		self.assertIsNone(identity.pending("account-1", self.root))
		self.assertNotIn("REWARDS_ACCOUNTS", (self.root / ".env").read_text())
		self.admin._recycle.assert_not_called()

	def test_terms_instruction_is_visible_while_the_browser_stays_open(self):
		self.visible.url = "https://rewards.bing.com/terms"
		def sleep(seconds):
			self.clock += seconds
			self.assertEqual(self.state()["state"], "waiting")
			self.assertEqual(self.state()["message_key"], "errors.state.terms_of_use")
			signin._cancel_path("account-1").touch()
		with patch.object(signin.time, "sleep", side_effect=sleep):
			self.assertEqual(signin.run("account-1", False), 1)
		self.assertEqual(self.state()["state"], "cancelled")

	def test_positive_header_cannot_override_login_form_error_or_consent(self):
		for code in ("signin", "error", "consent"):
			self.visible.structure["matched"] = {code:1}
			self.assertNotEqual(signin._detection(self.visible, self.rewards, None)[0], "signed_in")

	def test_broken_balance_reader_does_not_hide_profile_signal(self):
		self.rewards.read_balance.side_effect = RuntimeError("SYNTHETIC-SECRET")
		self.visible.auth["profile"] = True
		self.assertEqual(signin.run("account-1", False), 0)
		self.assertNotIn("SYNTHETIC-SECRET", json.dumps(self.state()))


class SigninSignalScriptTests(unittest.TestCase):
	def probe(self, **page):
		value = subprocess.run(["node", str(Path(__file__).with_name("signin_signals.mjs"))],
			input=json.dumps({"script":signin._SIGNIN_SIGNALS, **page}), capture_output=True,
			text=True, encoding="utf-8", timeout=8, check=True,
			env={key:value for key,value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")})
		packet = json.loads(value.stdout)
		self.assertEqual(packet["effects"], 0)
		self.assertNotIn("SYNTHETIC", json.dumps(packet))
		return packet["result"]

	def test_profile_identity_and_login_prompt_are_independent_booleans(self):
		self.assertTrue(self.probe(profile=[{"aria-label":"View profile"}])["profile"])
		self.assertTrue(self.probe(identity=[{"text":"SYNTHETIC PERSON"}])["identity"])
		self.assertTrue(self.probe(controls=[{"href":"https://login.live.com/"}])["signin"])
		self.assertTrue(self.probe(controls=[{"href":"/signin"}])["signin"])
		self.assertTrue(self.probe(controls=[{"text":"Anmelden"}])["signin"])

	def test_hidden_controls_generic_profile_and_signout_are_not_positive_auth(self):
		self.assertFalse(self.probe(profile=[{"aria-label":"View profile", "hidden":True}])["profile"])
		self.assertFalse(self.probe(profile=[{"aria-label":"Profile"}])["profile"])
		self.assertFalse(self.probe(identity=[{"text":"SYNTHETIC PERSON", "hidden":True}])["identity"])
		self.assertFalse(self.probe(controls=[{"text":"Sign out", "href":"/signout"}])["signin"])
