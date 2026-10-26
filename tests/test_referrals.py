"""Synthetic referral pages; no browser, profile or live account is used."""

import contextlib
from dataclasses import FrozenInstanceError
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import accounts
import constants
import main
import referrals
import rewards_state
import run_record


LINK = "https://rewards.bing.com/refer?code=SYNTHETIC"
RUN = "20261025T223000Z-0103"
AT = "2026-10-25T22:30:00Z"


class Driver:
	def __init__(self, *, raw=None, url="https://rewards.bing.com/refer"):
		self.current_url = url
		self.raw = {"link": LINK} if raw is None else raw
		self.loads = []
		self.scripts = []

	def get(self, url):
		self.loads.append(url)

	def execute_script(self, script):
		self.scripts.append(script)
		return self.raw


class ReferralReaderTests(unittest.TestCase):
	def test_frozen_private_repr_unknown_figures_and_one_navigation(self):
		driver = Driver()
		state = referrals.read_referral_page(driver)
		self.assertEqual(state, referrals.ReferralState(LINK, True))
		self.assertEqual(driver.loads, [referrals.TARGET])
		self.assertEqual(driver.scripts, [referrals._SNAPSHOT])
		self.assertIsNone(state.referrals)
		self.assertIsNone(state.friends)
		self.assertNotIn(LINK, repr(state))
		with self.assertRaises(FrozenInstanceError):
			state.link = "other"
		self.assertFalse(referrals.SELECTORS_VERIFIED)

	def test_equivalent_paths_query_fragment_and_no_other_page(self):
		for path in ("/refer", "/refer/", "/referandearn", "/referandearn/"):
			driver = Driver(url="https://rewards.bing.com" + path + "?mkt=SYNTHETIC#SYNTHETIC")
			self.assertTrue(referrals.read_referral_page(driver).link_found)
		for url in ("http://rewards.bing.com/refer", "https://rewards.bing.com/referral", "https://rewards.bing.com/refer/x", "https://rewards.bing.com.evil.invalid/refer", "https://bing.com/refer", "https://user@rewards.bing.com/refer", "https://rewards.bing.com:444/refer", "https://[bad/"):
			driver = Driver(url=url)
			self.assertEqual(referrals.read_referral_page(driver).unreadable, "page_unknown")
			self.assertEqual(driver.scripts, [])

	def test_login_redirects_and_dom_gates_never_return_link(self):
		for host in ("login.live.com", "login.microsoftonline.com", "account.microsoft.com", "account.live.com"):
			driver = Driver(url="https://" + host + "/?code=SYNTHETIC")
			state = referrals.read_referral_page(driver)
			self.assertEqual(state.unreadable, "signed_out")
			self.assertFalse(state.link_found)
			self.assertEqual(driver.scripts, [])
		for code in ("signed_out", "challenge", "page_unknown", "link_missing", "link_ambiguous", "link_invalid", "secret-not-an-error-code"):
			state = referrals.read_referral_page(Driver(raw={"link": LINK, "error": code}))
			self.assertIsNone(state.link)
			self.assertFalse(state.link_found)
			self.assertEqual(state.unreadable, code if code in referrals.UNREADABLE_CODES else "page_unknown")

	def test_invalid_links_never_become_clickable_values(self):
		for value in ("", "javascript:alert(1)", "http://rewards.bing.com/refer?code=SYNTHETIC", "https://rewards.bing.com.evil.invalid/refer", "https://user@rewards.bing.com/refer", "https://rewards.bing.com:444/refer", LINK + "\n", 3):
			state = referrals.read_referral_page(Driver(raw={"link": value}))
			self.assertIsNone(state.link)
			self.assertEqual(state.unreadable, "link_invalid" if value else "link_missing")

	def test_exceptions_never_log_message_or_private_link(self):
		for method in ("get", "execute_script"):
			driver = Driver()
			with patch.object(driver, method, side_effect=RuntimeError(LINK + " private@example.invalid")), patch.object(logging.getLogger(), "handle") as log:
				state = referrals.read_referral_page(driver)
			self.assertEqual(state.unreadable, "read_failed")
			log.assert_not_called()
			self.assertNotIn("SYNTHETIC", repr(state))

	def test_debug_transport_never_logs_link_and_restores_prior_state(self):
		for failing in (False, True):
			driver = Driver()
			loggers = [logging.getLogger(name) for name in ("selenium.webdriver.remote.remote_connection", "urllib3.connectionpool")]
			def extract(script):
				for logger in loggers:
					logger.debug("Private link: %s", LINK)
				if failing:
					raise RuntimeError(LINK)
				return {"link": LINK}
			driver.execute_script = extract
			with contextlib.ExitStack() as stack:
				for logger in loggers:
					stack.enter_context(patch.object(logger, "disabled", False))
					stack.enter_context(patch.object(logger, "level", logging.DEBUG))
				with patch.object(logging.getLogger(), "handle") as handler, patch.object(logging.getLogger(), "callHandlers") as root_handler:
					state = referrals.read_referral_page(driver)
					handler.assert_not_called()
					root_handler.assert_not_called()
				self.assertTrue(all(not logger.disabled for logger in loggers))
			self.assertEqual(state.unreadable, "read_failed" if failing else None)

	def test_location_changes_during_extraction_discard_private_link(self):
		driver = Driver()
		def extract(script):
			driver.current_url = "https://login.live.com/"
			return {"link": LINK}
		driver.execute_script = extract
		self.assertEqual(referrals.read_referral_page(driver), referrals.ReferralState(unreadable="signed_out"))

	def test_real_javascript_uses_copy_label_nearest_input_without_effects(self):
		node = shutil.which("node")
		self.assertIsNotNone(node, "Node required for the synthetic DOM proof")
		wrapper = r"""
const fs=require('fs'), payload=JSON.parse(fs.readFileSync(0,'utf8'));
let effects=0;
const fail=()=>{effects++;throw Error('Forbidden effect')};
const input={value:payload.link};
const root={contains:n=>true,querySelectorAll:s=>s.includes('input')?[]:[copy]};
const parent={parentElement:root,querySelectorAll:s=>payload.ambiguous?[input,{value:'wrong'}]:payload.missing?[]:[input]};
const copy={tagName:'BUTTON',textContent:'Copy link',parentElement:parent,getAttribute:k=>null,click:fail};
const controls=[copy];
if(payload.gate==='password') controls.push({tagName:'INPUT',textContent:'',getAttribute:k=>k==='type'?'password':null});
if(payload.gate==='login') controls.push({tagName:'BUTTON',textContent:'Sign in',getAttribute:k=>null});
if(payload.gate==='challenge') controls.push({tagName:'IFRAME',textContent:'',getAttribute:k=>k==='src'?'https://challenge.invalid/':null});
if(payload.gate==='unknown') controls.push({tagName:'IFRAME',textContent:'',getAttribute:k=>k==='src'?'https://telemetry.invalid/':null});
global.document={readyState:'complete',querySelector:s=>s==='main'?root:null,querySelectorAll:s=>controls};
global.location={assign:fail,replace:fail};
const result=new Function(payload.script)();
process.stdout.write(JSON.stringify({result,effects}));
"""
		for mode, expected in (({}, {"link": LINK}), ({"ambiguous": True}, {"error": "link_ambiguous"}), ({"missing": True}, {"error": "link_missing"}), ({"gate": "password"}, {"error": "signed_out"}), ({"gate": "login"}, {"error": "signed_out"}), ({"gate": "challenge"}, {"error": "challenge"}), ({"gate": "unknown"}, {"link": LINK})):
			payload = {"script": referrals._SNAPSHOT, "link": LINK, **mode}
			env = {key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")}
			result = subprocess.run([node, "-e", wrapper], input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=8, env=env)
			self.assertEqual(result.returncode, 0, result.stderr)
			self.assertEqual(json.loads(result.stdout), {"result": expected, "effects": 0})


class ReferralStoreTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)

	def save(self, account="one", state=None, stamp=AT, identity=RUN):
		referrals.save_referral_state(account, state or referrals.ReferralState(LINK, True), read_at=stamp, run_id=identity, directory=self.home)

	def event(self, **fields):
		run_record.append_event(str(self.home / "runs" / (RUN + ".jsonl")), "referral_state", RUN, account="one", read_at=AT, **fields)

	def test_missing_store_is_unknown_and_read_does_not_create_it(self):
		value = referrals.get_referral_info("one", directory=self.home)
		self.assertEqual(value, {"link": None, "link_found": False, "referrals": None, "friends": None, "read_at": None, "unreadable": "not_read"})
		self.assertEqual(list(self.home.iterdir()), [])

	def test_atomic_roundtrip_bound_to_account_and_matching_event(self):
		self.save()
		self.save("two", referrals.ReferralState("https://rewards.bing.com/refer?code=SYNTHETIC_TWO", True))
		self.event(link_found=True, referrals=None, friends=None, unreadable=None)
		self.assertEqual(referrals.get_referral_info("one", directory=self.home), {"link": LINK, "link_found": True, "referrals": None, "friends": None, "read_at": AT, "unreadable": None})
		self.assertNotEqual(referrals.get_referral_info("two", directory=self.home)["link"], LINK)
		self.assertIsNone(referrals.get_referral_info("other", directory=self.home)["link"])
		self.assertNotIn(LINK, (self.home / "runs" / (RUN + ".jsonl")).read_text())

	def test_atomic_failure_preserves_old_snapshot_and_removes_temp(self):
		self.save()
		path = self.home / "referral.json"
		before = path.read_bytes()
		with patch.object(referrals, "replace_file", side_effect=OSError("synthetic write failed")):
			with self.assertRaises(OSError):
				self.save(state=referrals.ReferralState(unreadable="read_failed"))
		self.assertEqual(path.read_bytes(), before)
		self.assertEqual(list(self.home.iterdir()), [path])

	def test_latest_failure_event_hides_old_link_after_storage_failure(self):
		self.save(stamp="2026-10-25T22:00:00Z")
		self.event(link_found=True, referrals=None, friends=None, unreadable="storage_failed")
		value = referrals.get_referral_info("one", directory=self.home)
		self.assertIsNone(value["link"])
		self.assertEqual(value["read_at"], AT)
		self.assertEqual(value["unreadable"], "storage_failed")

	def test_mismatching_record_never_joins_link_from_another_measurement(self):
		self.save(identity="20261025T220000Z-0000")
		self.event(link_found=True, referrals=None, friends=None, unreadable=None)
		value = referrals.get_referral_info("one", directory=self.home)
		self.assertTrue(value["link_found"])
		self.assertIsNone(value["link"])
		self.assertIsNone(value["referrals"])
		self.assertIsNone(value["friends"])

	def test_malformed_or_future_snapshot_returns_bounded_unreadable(self):
		for value in ("not json", "[]", '{"v":2,"accounts":{}}', '{"v":1,"accounts":{"one":{"link":"private","read_at":"bad"}}}'):
			(self.home / "referral.json").write_text(value, encoding="utf-8")
			self.assertEqual(referrals.get_referral_info("one", directory=self.home)["unreadable"], "storage_unreadable")


class ReferralHookTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		self.stack = contextlib.ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.dict(os.environ, {"REWARDS_FARMER_DATA_HOME": str(self.home), "USER_DATA_DIR": str(self.home / "profiles"), "REWARDS_ACCOUNTS": "one,two"}))
		self.stack.enter_context(patch.object(constants, "RUNS_DIR", str(self.home / "runs")))
		self.stack.enter_context(patch.object(main, "HEADLESS", True))
		self.stack.enter_context(patch.object(main.log_utils, "setup_logging"))
		for name, value in (("prepare_desktop_before_launch", True), ("switch_back_after_launch", None), ("cleanup_virtual_desktop", None), ("reset_virtual_desktop_state", None), ("is_virtual_desktop_enabled", False)):
			self.stack.enter_context(patch.object(main.desktop_utils, name, return_value=value))
		self.driver = Mock()
		self.stack.enter_context(patch.object(main.browser, "start_driver", return_value=self.driver))
		self.tasks = Mock()
		self.stack.enter_context(patch.object(main.rewards_tasks, "RewardsTaskUtils", return_value=self.tasks))
		self.stack.enter_context(patch.object(main.element_selectors, "ElementSelectionUtils", return_value=Mock()))
		self.state = self.stack.enter_context(patch.object(rewards_state, "read_state", return_value=rewards_state.RewardsState(balance=100, open_points=60)))
		self.referral = self.stack.enter_context(patch.object(referrals, "read_referral_page", return_value=referrals.ReferralState(LINK, True)))
		self.recorders = []
		open_run = run_record.open_run
		def record_open(*args, **kwargs):
			recorder = open_run(*args, **kwargs)
			self.recorders.append(recorder)
			return recorder
		self.stack.enter_context(patch.object(run_record, "open_run", side_effect=record_open))

	def events(self):
		# UUID suffix ordering cannot choose the last of two same-second runs.
		path = self.recorders[-1].path
		self.assertEqual(Path(path).parent, self.home / "runs")
		return run_record.read_events(path)

	def test_after_read_once_per_account_event_has_no_private_link(self):
		self.assertEqual(main.main(True, main.RunPlan(read_state=True)), 0)
		self.assertEqual(self.referral.call_count, 2)
		for account in ("one", "two"):
			events = [item for item in self.events() if item.get("account") == account]
			record = next(item for item in events if item["type"] == "referral_state")
			index = events.index(record)
			self.assertEqual((events[index - 1]["type"], events[index - 1]["phase"]), ("state", "after"))
			self.assertEqual({key: record[key] for key in ("link_found", "referrals", "friends", "unreadable")}, {"link_found": True, "referrals": None, "friends": None, "unreadable": None})
			self.assertEqual(referrals.get_referral_info(account, directory=self.home)["link"], LINK)
		self.assertNotIn(LINK, json.dumps(self.events()))
		self.assertEqual(self.driver.quit.call_count, 2)

	def test_default_cli_checks_and_skips_have_no_referral_navigation(self):
		for plan in (main.RunPlan(), main.RunPlan(check_only=True), main.RunPlan(only_if_open=True)):
			self.state.return_value = rewards_state.RewardsState(open_points=0)
			self.assertEqual(main.main(True, plan), 0)
		self.referral.assert_not_called()
		self.assertFalse((self.home / "referral.json").exists())

	def test_root_profile_uses_canonical_root_storage_and_event_identity(self):
		with patch.dict(os.environ, {"REWARDS_ACCOUNTS": ""}):
			self.assertEqual(main.main(True, main.RunPlan(read_state=True)), 0)
		value = referrals.get_referral_info("@root", directory=self.home)
		self.assertEqual(value["link"], LINK)
		self.assertIsNone(referrals.get_referral_info("default", directory=self.home)["link"])
		event = next(item for item in self.events() if item["type"] == "referral_state")
		self.assertEqual(event["account"], "default")
		self.assertEqual(event["account_id"], "@root")

	def test_reader_and_storage_errors_keep_success_and_correlated_unreadable(self):
		for failure in ("reader", "storage"):
			self.referral.side_effect = RuntimeError(LINK) if failure == "reader" else None
			with patch.object(referrals, "save_referral_state", side_effect=OSError(LINK) if failure == "storage" else None):
				self.assertEqual(main.main(True, main.RunPlan(read_state=True)), 0)
			events = self.events()
			self.assertEqual(events[-1]["result"], "ok")
			self.assertEqual([item["unreadable"] for item in events if item["type"] == "referral_state"], ["read_failed" if failure == "reader" else "storage_failed"] * 2)
			self.assertNotIn(LINK, json.dumps(events))
