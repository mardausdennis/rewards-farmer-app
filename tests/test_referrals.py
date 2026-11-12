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
		if script == referrals._READY:
			return {"ready": True}
		return self.raw


class Clock:
	def __init__(self):
		self.now = 0.0
		self.sleeps = []

	def monotonic(self):
		return self.now

	def sleep(self, seconds):
		self.sleeps.append(seconds)
		self.now += seconds


class ReferralReaderTests(unittest.TestCase):
	def test_frozen_private_repr_unknown_figures_and_one_navigation(self):
		driver = Driver()
		state = referrals.read_referral_page(driver)
		self.assertEqual(state, referrals.ReferralState(LINK, True))
		self.assertEqual(driver.loads, [referrals.TARGET])
		self.assertEqual(driver.scripts, [referrals._READY, referrals._SNAPSHOT])
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
				if script == referrals._READY: return {"ready": True}
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
			if script == referrals._READY: return {"ready": True}
			driver.current_url = "https://login.live.com/"
			return {"link": LINK}
		driver.execute_script = extract
		self.assertEqual(referrals.read_referral_page(driver), referrals.ReferralState(unreadable="signed_out", reason="location"))

	def test_waits_for_root_readiness_and_unique_button_before_one_snapshot(self):
		clock, driver = Clock(), Driver()
		answers = iter([{"error": "page_unknown", "reason": reason} for reason in ("no_main", "not_complete", "no_copy_button")] + [{"error": "link_ambiguous", "reason": "copy_button_ambiguous"}, {"ready": True}])
		probes = []
		def extract(script):
			driver.scripts.append(script)
			if script == referrals._READY:
				probes.append(clock.now)
				return next(answers)
			return {"link": LINK}
		driver.execute_script = extract
		with patch.object(referrals, "time", clock):
			state = referrals.read_referral_page(driver)
		self.assertEqual(state, referrals.ReferralState(LINK, True))
		self.assertEqual(driver.loads, [referrals.TARGET])
		self.assertEqual(driver.scripts.count(referrals._SNAPSHOT), 1)
		self.assertEqual(probes, [0, .25, .5, .75, 1])
		self.assertEqual(clock.sleeps, [.25] * 4)

	def test_wait_timeout_is_fifteen_seconds_without_navigation_or_snapshot_retry(self):
		for code, reason in (("page_unknown", "no_main"), ("page_unknown", "not_complete"), ("page_unknown", "no_copy_button"), ("link_ambiguous", "copy_button_ambiguous")):
			with self.subTest(reason=reason):
				clock, driver = Clock(), Driver()
				driver.execute_script = Mock(return_value={"error": code, "reason": reason})
				with patch.object(referrals, "time", clock):
					state = referrals.read_referral_page(driver)
				self.assertEqual((state.unreadable, state.reason), (code, reason))
				self.assertFalse(state.link_found)
				self.assertEqual(clock.now, 15)
				self.assertEqual(clock.sleeps, [.25] * 60)
				self.assertEqual(driver.execute_script.call_count, 60)
				self.assertTrue(all(call.args == (referrals._READY,) for call in driver.execute_script.call_args_list))
				self.assertEqual(driver.loads, [referrals.TARGET])

	def test_slow_readiness_respects_remaining_transport_budget_and_cannot_return_late_success(self):
		clock, driver = Clock(), Driver()
		connection = SimpleNamespace(_client_config=SimpleNamespace(timeout="original"), _conn=SimpleNamespace(request=Mock()))
		driver.command_executor = connection
		budgets = []
		def extract(script):
			budgets.append(connection._client_config.timeout.total)
			clock.now += connection._client_config.timeout.total
			return {"error": "page_unknown", "reason": "not_complete"}
		driver.execute_script = extract
		with patch.object(referrals, "time", clock):
			state = referrals.read_referral_page(driver)
		self.assertEqual((state.unreadable, state.reason), ("page_unknown", "not_complete"))
		self.assertEqual(clock.now, 15)
		self.assertEqual(budgets, [2] * 6 + [1.5])
		self.assertEqual(connection._client_config.timeout, "original")

	def test_command_timeouts_and_no_transport_retries_restore_caller_setup(self):
		driver = Driver()
		request = Mock(return_value="synthetic")
		connection = SimpleNamespace(_client_config=SimpleNamespace(timeout="original"), _conn=SimpleNamespace(request=request))
		driver.command_executor = connection
		budgets = []
		def get(url):
			driver.loads.append(url)
			budgets.append(connection._client_config.timeout.total)
			connection._conn.request("GET", "synthetic", retries=10)
		def extract(script):
			budgets.append(connection._client_config.timeout.total)
			return {"ready": True} if script == referrals._READY else {"link": LINK}
		driver.get, driver.execute_script = get, extract
		self.assertTrue(referrals.read_referral_page(driver).link_found)
		self.assertEqual(budgets, [65, 2, 8])
		self.assertEqual(request.call_args.kwargs["retries"], False)
		self.assertIs(connection._conn.request, request)
		self.assertEqual(connection._client_config.timeout, "original")
		with patch.object(driver, "get", side_effect=RuntimeError(LINK)):
			self.assertEqual(referrals.read_referral_page(driver).unreadable, "read_failed")
		self.assertEqual(connection._client_config.timeout, "original")
		self.assertIs(connection._conn.request, request)

	def test_all_page_unknown_branches_have_bounded_reasons_and_untrusted_details_are_dropped(self):
		for reason in ("too_many_controls", "no_main", "not_complete", "no_copy_button"):
			clock, driver = Clock(), Driver()
			driver.execute_script = Mock(return_value={"error": "page_unknown", "reason": reason, "text": LINK, "count": 9999999})
			with patch.object(referrals, "time", clock):
				state = referrals.read_referral_page(driver)
			self.assertEqual((state.unreadable, state.reason), ("page_unknown", reason))
			self.assertNotIn(LINK, repr(state))
			self.assertNotIn("9999999", repr(state))
		self.assertEqual(referrals.read_referral_page(Driver(url="https://private.invalid/?secret=SYNTHETIC")).reason, "location")
		for raw in ([], {"error": [LINK]}, {"error": LINK, "reason": LINK}):
			driver = Driver(raw=raw)
			state = referrals.read_referral_page(driver)
			self.assertEqual(state.unreadable, "page_unknown")
			self.assertNotIn(LINK, repr(state))

	def test_password_login_challenge_or_excess_controls_stop_wait_without_snapshot(self):
		for raw in ({"error": "signed_out"}, {"error": "challenge"}, {"error": "page_unknown", "reason": "too_many_controls"}):
			clock, driver = Clock(), Driver()
			driver.execute_script = Mock(return_value=raw)
			with patch.object(referrals, "time", clock):
				state = referrals.read_referral_page(driver)
			self.assertEqual(state.unreadable, raw["error"])
			self.assertEqual(driver.execute_script.call_count, 1)
			self.assertEqual(clock.sleeps, [])

	def test_single_snapshot_can_still_report_changed_readiness(self):
		driver = Driver(raw={"error": "page_unknown", "reason": "no_main"})
		self.assertEqual(referrals.read_referral_page(driver).reason, "no_main")
		self.assertEqual(driver.scripts, [referrals._READY, referrals._SNAPSHOT])

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
		for mode, expected in (({}, {"link": LINK}), ({"ambiguous": True}, {"error": "link_ambiguous", "reason": "input_ambiguous"}), ({"missing": True}, {"error": "link_missing", "reason": "input_missing"}), ({"gate": "password"}, {"error": "signed_out"}), ({"gate": "login"}, {"error": "signed_out"}), ({"gate": "challenge"}, {"error": "challenge"}), ({"gate": "unknown"}, {"link": LINK})):
			payload = {"script": referrals._SNAPSHOT, "link": LINK, **mode}
			env = {key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")}
			result = subprocess.run([node, "-e", wrapper], input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True, timeout=8, env=env)
			self.assertEqual(result.returncode, 0, result.stderr)
			self.assertEqual(json.loads(result.stdout), {"result": expected, "effects": 0})

	def test_real_javascript_readiness_and_snapshot_report_each_page_unknown_branch(self):
		node = shutil.which("node")
		self.assertIsNotNone(node)
		wrapper = r"""
const fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
const copy={tagName:'BUTTON',textContent:'Copy link',getAttribute:k=>null};
const root={querySelectorAll:s=>p.branch==='no_copy_button'?[]:[copy]};
global.document={readyState:p.branch==='not_complete'?'interactive':'complete',querySelector:s=>p.branch==='no_main'?null:root,querySelectorAll:s=>p.branch==='too_many_controls'?Array(401).fill(copy):[copy]};
process.stdout.write(JSON.stringify(new Function(p.script)()));
"""
		for script in (referrals._READY, referrals._SNAPSHOT):
			for branch in ("too_many_controls", "no_main", "not_complete", "no_copy_button"):
				result = subprocess.run([node, "-e", wrapper], input=json.dumps({"script": script, "branch": branch}), text=True, encoding="utf-8", capture_output=True, timeout=8,
					env={key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")})
				self.assertEqual(result.returncode, 0, "synthetic JavaScript failed")
				self.assertEqual(json.loads(result.stdout), {"error": "page_unknown", "reason": branch})


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

	def test_failed_and_invalid_reads_retain_good_link_and_its_own_timestamp(self):
		self.save(stamp="2026-10-25T22:00:00Z")
		self.save("two", referrals.ReferralState("https://rewards.bing.com/refer?code=SYNTHETIC_TWO", True))
		for state in (referrals.ReferralState(unreadable="page_unknown", reason="no_copy_button"), referrals.ReferralState("https://private.invalid/", True), referrals.ReferralState(None, True)):
			self.save(state=state)
			stored = json.loads((self.home / "referral.json").read_text())["accounts"]["one"]
			self.assertEqual(stored["link"], LINK)
			self.assertEqual(stored["link_read_at"], "2026-10-25T22:00:00Z")
			self.assertEqual(stored["last_attempt_at"], AT)
			self.assertNotIn("read_at", stored)
			value = referrals.get_referral_info("one", directory=self.home)
			self.assertEqual((value["link"], value["link_found"], value["read_at"]), (LINK, True, "2026-10-25T22:00:00Z"))
			self.assertEqual(value["unreadable"], state.unreadable or ("link_invalid" if state.link else "link_missing"))
		self.assertEqual(referrals.get_referral_info("two", directory=self.home)["link"], "https://rewards.bing.com/refer?code=SYNTHETIC_TWO")
		self.assertIsNone(referrals.get_referral_info("three", directory=self.home)["link"])

	def test_good_read_after_failure_replaces_link_and_clears_attempt_error(self):
		self.save(stamp="2026-10-25T22:00:00Z")
		self.save(state=referrals.ReferralState(unreadable="page_unknown"))
		replacement = "https://rewards.bing.com/refer?code=SYNTHETIC_NEW"
		self.save(state=referrals.ReferralState(replacement, True), stamp="2026-10-26T22:00:00Z")
		value = referrals.get_referral_info("one", directory=self.home)
		self.assertEqual(value, {"link": replacement, "link_found": True, "referrals": None, "friends": None, "read_at": "2026-10-26T22:00:00Z", "unreadable": None})
		stored = json.loads((self.home / "referral.json").read_text())["accounts"]["one"]
		self.assertEqual(stored["link_read_at"], stored["last_attempt_at"])

	def test_020_030_shapes_load_and_upgrade_without_losing_good_link(self):
		for error in (None, "page_unknown"):
			old = {"link": LINK if error is None else None, "link_found": error is None, "referrals": None, "friends": None, "read_at": "2026-10-25T22:00:00Z", "unreadable": error, "run_id": RUN}
			(self.home / "referral.json").write_text(json.dumps({"v": 1, "accounts": {"one": old}}), encoding="utf-8")
			before = (self.home / "referral.json").read_bytes()
			value = referrals.get_referral_info("one", directory=self.home)
			self.assertEqual(value["link"], old["link"])
			self.assertEqual(value["read_at"], old["read_at"])
			self.assertEqual(value["unreadable"], error)
			self.assertEqual((self.home / "referral.json").read_bytes(), before)
			self.save(state=referrals.ReferralState(unreadable="page_unknown", reason="not_complete"))
			upgraded = json.loads((self.home / "referral.json").read_text())["accounts"]["one"]
			self.assertEqual(upgraded["link"], old["link"])
			self.assertEqual(upgraded["link_read_at"], old["read_at"] if error is None else None)
			self.assertEqual(upgraded["last_attempt_at"], AT)

	def test_legacy_failed_snapshot_does_not_invent_a_verified_link(self):
		old = {"link": LINK, "link_found": True, "read_at": AT, "unreadable": "storage_failed"}
		(self.home / "referral.json").write_text(json.dumps({"v": 1, "accounts": {"one": old}}))
		self.assertIsNone(referrals.get_referral_info("one", directory=self.home)["link"])

	def test_first_failure_and_later_event_never_read_another_accounts_link(self):
		self.save("two")
		self.save(state=referrals.ReferralState(unreadable="page_unknown", reason="no_main"))
		self.event(link_found=False, unreadable="page_unknown", referrals=None, friends=None)
		value = referrals.get_referral_info("one", directory=self.home)
		self.assertIsNone(value["link"])
		self.assertFalse(value["link_found"])
		self.assertEqual(value["read_at"], AT)
		self.assertEqual(value["unreadable"], "page_unknown")

	def test_atomic_failure_preserves_old_snapshot_and_removes_temp(self):
		self.save()
		path = self.home / "referral.json"
		before = path.read_bytes()
		with patch.object(referrals, "replace_file", side_effect=OSError("synthetic write failed")):
			with self.assertRaises(OSError):
				self.save(state=referrals.ReferralState(unreadable="read_failed"))
		self.assertEqual(path.read_bytes(), before)
		self.assertEqual(list(self.home.iterdir()), [path])

	def test_latest_failure_event_preserves_old_link_after_storage_failure(self):
		self.save(stamp="2026-10-25T22:00:00Z")
		self.event(link_found=True, referrals=None, friends=None, unreadable="storage_failed")
		value = referrals.get_referral_info("one", directory=self.home)
		self.assertEqual(value["link"], LINK)
		self.assertTrue(value["link_found"])
		self.assertEqual(value["read_at"], "2026-10-25T22:00:00Z")
		self.assertEqual(value["unreadable"], "storage_failed")

	def test_mismatching_record_keeps_independently_dated_link_without_claiming_freshness(self):
		self.save(identity="20261025T220000Z-0000")
		self.event(link_found=True, referrals=None, friends=None, unreadable=None)
		value = referrals.get_referral_info("one", directory=self.home)
		self.assertTrue(value["link_found"])
		self.assertEqual(value["link"], LINK)
		self.assertEqual(value["read_at"], AT)
		self.assertEqual(value["unreadable"], "storage_unreadable")
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
		self.stack.enter_context(patch.dict(os.environ, {"REWARDS_HEADLESS": "true"}))
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

	def test_referral_hook_leaves_refer_page_after_read_on_success_or_failure(self):
		for failure in (False, True):
			order = []
			with patch.object(referrals, "read_referral_page", side_effect=lambda driver: order.append("read") or referrals.ReferralState(unreadable="read_failed") if failure else order.append("read") or referrals.ReferralState(LINK, True)), patch.object(referrals, "save_referral_state"), patch.object(self.driver, "get", side_effect=lambda url: order.append(url)):
				main._record_referrals(self.driver, main.accounts.Account("one", "synthetic", "Default"))
			self.assertEqual(order, ["read", "https://rewards.bing.com/"])

	def test_return_navigation_error_does_not_fail_run_and_is_correlated(self):
		with patch.object(self.driver, "get", side_effect=RuntimeError(LINK)):
			self.assertEqual(main.main(True, main.RunPlan(read_state=True)), 0)
		self.assertEqual(self.events()[-1]["result"], "ok")
		self.assertTrue(any(event.get("code") == "referral_return_failed" for event in self.events()))
		self.assertNotIn(LINK, json.dumps(self.events()))

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

	def test_failed_read_retains_last_good_links_and_emits_value_free_reason(self):
		for account in ("one", "two"):
			referrals.save_referral_state(account, referrals.ReferralState(LINK, True), read_at=AT, run_id=RUN, directory=self.home)
		self.referral.return_value = referrals.ReferralState(unreadable="page_unknown", reason="no_copy_button")
		self.assertEqual(main.main(True, main.RunPlan(read_state=True)), 0)
		for record in (event for event in self.events() if event["type"] == "referral_state"):
			self.assertFalse(record["link_found"])
			self.assertEqual(record["unreadable"], "page_unknown")
			self.assertEqual(record["reason"], "no_copy_button")
			value = referrals.get_referral_info(record["account"], directory=self.home)
			self.assertEqual(value["link"], LINK)
			self.assertEqual(value["read_at"], AT)
			self.assertEqual(value["unreadable"], "page_unknown")
		self.assertNotIn(LINK, json.dumps(self.events()))

	def test_every_branch_reason_survives_record_and_unknown_reason_is_never_exported(self):
		for reason in ("location", "too_many_controls", "no_main", "not_complete", "no_copy_button", LINK, [LINK]):
			self.referral.return_value = referrals.ReferralState(unreadable="page_unknown", reason=reason)
			with patch.object(main.run_record, "emit") as emit:
				main._record_referrals(self.driver, accounts.Account("one", "synthetic", "Default"))
			fields = next(call.kwargs for call in emit.call_args_list if call.args == ("referral_state",))
			self.assertEqual(fields["reason"], reason if isinstance(reason, str) and reason in referrals.REASON_CODES else None)
			self.assertNotIn(LINK, json.dumps(fields))
