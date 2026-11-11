import _bootstrap  # noqa: F401

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rfapp import notify, tray


class EdgeReminderTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		self.now = datetime(2026, 11, 11, 22, 0).astimezone()
		self.tray = Mock()
		self.tray.notify.return_value = True
		notify._set_tray(self.tray)
		self.addCleanup(notify._set_tray, None)
		self.t = SimpleNamespace(t=Mock(side_effect=lambda key, **values: {"tray.notify.edge_streak.title": "Edge browsing streak", "tray.notify.edge_streak.body": "{account}: Microsoft shows {minutes} of {target} minutes today."}[key].format(**values)))
		for guard in (patch.dict(os.environ, {"REWARDS_FARMER_DATA_HOME": str(self.home)}), patch.dict(sys.modules, {"rfapp.i18n": SimpleNamespace(current=lambda: self.t)}), patch.object(notify.user_state, "notification_state", return_value=5), patch.object(notify.subprocess, "Popen", side_effect=AssertionError("Reminder must not launch a run or window")), patch.object(notify.requests, "post", side_effect=AssertionError("Reminder must not send a webhook")), patch.object(tray, "_icon", side_effect=AssertionError("No visible tray or toast in tests"))):
			guard.start()
			self.addCleanup(guard.stop)
		self.row = {"account_id": "account-1", "edge_tile": True, "edge_minutes": 12, "edge_target": 30,
			"at": (self.now - timedelta(minutes=4)).isoformat(), "run_id": "last-scheduled",
			"last_scheduled_at": (self.now - timedelta(minutes=1)).isoformat(), "last_scheduled_run_id": "last-scheduled"}
		self.overview = {"accounts": [{"account_id": "account-1", "label": "Synthetic"}], "active_run": None,
			"edge_streak": {"mode": "remind", "auto_off": None, "accounts": [self.row]}}

	@property
	def receipts(self):
		return self.home / "edge-streak-reminders.json"

	def evaluate(self, overview=None, now=None):
		notify.edge_reminder(self.overview if overview is None else overview, now=self.now if now is None else now)

	def test_final_scheduled_measurement_below_target_notifies_once_and_persists_receipt(self):
		self.evaluate()
		self.evaluate()
		self.tray.notify.assert_called_once_with("Edge browsing streak", "Synthetic: Microsoft shows 12 of 30 minutes today.")
		receipt = json.loads(self.receipts.read_text())["days"][self.now.date().isoformat()]["account-1"]
		self.assertEqual(receipt["run_id"], "last-scheduled")
		self.assertEqual(receipt["status"], "accepted")
		self.assertEqual([path for path in self.home.glob(".edge-reminder-*") if path.is_file()], [])

	def test_restart_reloads_disk_receipts_instead_of_repeating(self):
		self.evaluate()
		spec = importlib.util.spec_from_file_location("edge_reminder_restarted", notify.__file__)
		restarted = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(restarted)
		restarted._set_tray(self.tray)
		self.addCleanup(restarted._set_tray, None)
		restarted.edge_reminder(self.overview, now=self.now)
		self.tray.notify.assert_called_once()

	def test_other_process_store_claim_blocks_delivery_without_losing_eligibility(self):
		with patch("run_lock.acquire", return_value=None), patch.object(notify._log, "exception"):
			self.evaluate()
		self.tray.notify.assert_not_called()
		self.assertFalse(self.receipts.exists())
		self.evaluate()
		self.tray.notify.assert_called_once()

	def test_after_auto_off_effective_remind_mode_notifies_but_fill_and_off_never_do(self):
		for mode in ("off", "fill", None, "unknown"):
			self.overview["edge_streak"]["mode"] = mode
			self.evaluate()
		self.tray.notify.assert_not_called()
		self.assertFalse(self.receipts.exists())
		self.overview["edge_streak"].update(mode="remind", auto_off={"reason": "minutes_not_counted", "dates": ["2026-11-10", "2026-11-11"]})
		self.evaluate()
		self.tray.notify.assert_called_once()

	def test_no_notice_for_absent_unknown_invalid_or_complete_measurements(self):
		for changes in ({"edge_tile": False}, {"edge_tile": None}, {"edge_tile": 1}, {"edge_minutes": None}, {"edge_target": None}, {"edge_minutes": 30}, {"edge_minutes": 31}, {"edge_minutes": -1}, {"edge_minutes": True}, {"edge_minutes": "12"}, {"edge_target": False}, {"edge_target": 0}, {"edge_target": -30}, {"edge_target": "30"}, {"edge_minutes": 12.0}):
			with self.subTest(changes=changes):
				view = copy.deepcopy(self.overview)
				view["edge_streak"]["accounts"][0].update(changes)
				self.evaluate(view)
		self.tray.notify.assert_not_called()
		self.assertFalse(self.receipts.exists())

	def test_earlier_run_yesterday_unknown_naive_or_future_timestamps_never_remind(self):
		for changes in ({"run_id": "earlier-run"}, {"run_id": None}, {"last_scheduled_run_id": None}, {"last_scheduled_at": None}, {"at": None}, {"at": "not-a-date"}, {"at": "2026-11-11T21:56:00"}, {"at": (self.now - timedelta(days=1)).isoformat()}, {"last_scheduled_at": (self.now - timedelta(days=1)).isoformat()}, {"last_scheduled_at": (self.now + timedelta(minutes=1)).isoformat()}, {"at": self.now.isoformat()}):
			with self.subTest(changes=changes):
				view = copy.deepcopy(self.overview)
				view["edge_streak"]["accounts"][0].update(changes)
				self.evaluate(view)
		self.tray.notify.assert_not_called()
		self.assertFalse(self.receipts.exists())

	def test_active_final_run_waits_for_finish_instead_of_using_old_measurement(self):
		self.overview["active_run"] = {"run_id": "last-scheduled"}
		self.evaluate()
		self.tray.notify.assert_not_called()
		self.overview["active_run"] = None
		self.evaluate()
		self.tray.notify.assert_called_once()

	def test_each_account_and_local_day_have_their_own_receipt(self):
		self.overview["edge_streak"]["accounts"].append({**self.row, "account_id": "account-2"})
		self.evaluate()
		self.evaluate()
		self.assertEqual(self.tray.notify.call_count, 2)
		self.overview["edge_streak"]["accounts"] = [self.row]
		self.now += timedelta(days=1)
		self.row.update(at=(self.now - timedelta(minutes=4)).isoformat(), last_scheduled_at=(self.now - timedelta(minutes=1)).isoformat(), run_id="next-day", last_scheduled_run_id="next-day")
		self.evaluate()
		self.assertEqual(self.tray.notify.call_count, 3)
		self.assertEqual(len(json.loads(self.receipts.read_text())["days"]), 2)

	def test_local_day_not_raw_utc_day_controls_measurements_and_receipts(self):
		self.now = datetime(2026, 11, 12, 0, 10).astimezone()
		self.row.update(at=(self.now - timedelta(minutes=5)).astimezone(timezone.utc).isoformat(), last_scheduled_at=(self.now - timedelta(minutes=1)).astimezone(timezone.utc).isoformat())
		self.evaluate()
		self.tray.notify.assert_called_once()
		self.assertIn("2026-11-12", json.loads(self.receipts.read_text())["days"])

	def test_busy_or_rejected_notice_is_claimed_and_never_replayed(self):
		with patch.object(notify.user_state, "notification_state", return_value=3):
			self.evaluate()
		self.evaluate()
		self.tray.notify.assert_not_called()
		self.assertEqual(json.loads(self.receipts.read_text())["days"][self.now.date().isoformat()]["account-1"]["status"], "held_busy_3")

	def test_claim_survives_failed_delivery_or_final_receipt_write(self):
		original = notify._reminder_store
		calls = []
		def save(path, value):
			calls.append(1)
			if len(calls) == 2:
				raise OSError("Synthetic post-delivery write failure")
			original(path, value)
		with patch.object(notify, "_reminder_store", side_effect=save), patch.object(notify._log, "exception"):
			self.evaluate()
		self.evaluate()
		self.tray.notify.assert_called_once()
		self.assertEqual(json.loads(self.receipts.read_text())["days"][self.now.date().isoformat()]["account-1"]["status"], "claimed")

	def test_delivery_exception_or_rejection_cannot_repeat_after_restart(self):
		for response in (False, RuntimeError("Synthetic shell failure")):
			with self.subTest(response=type(response).__name__):
				self.tray.notify.reset_mock()
				self.tray.notify.side_effect = response if isinstance(response, Exception) else None
				self.tray.notify.return_value = response
				with patch.object(notify._log, "exception"), patch.object(notify._log, "warning"):
					self.evaluate()
				self.tray.notify.side_effect = None
				self.tray.notify.return_value = True
				self.evaluate()
				self.tray.notify.assert_called_once()
				self.now += timedelta(days=1)
				self.row.update(at=(self.now - timedelta(minutes=4)).isoformat(), last_scheduled_at=(self.now - timedelta(minutes=1)).isoformat())

	def test_corrupt_store_or_preclaim_write_failure_fails_closed_without_notice(self):
		for text in ("{", "[]", '{"v":1,"days":[]}', '{"v":2,"days":{}}'):
			self.receipts.write_text(text)
			with patch.object(notify._log, "exception"):
				self.evaluate()
		self.tray.notify.assert_not_called()
		self.receipts.unlink()
		with patch.object(notify, "_reminder_store", side_effect=OSError("Synthetic disk error")), patch.object(notify._log, "exception"):
			self.evaluate()
		self.tray.notify.assert_not_called()

	def test_no_running_tray_never_uses_temporary_closed_app_toast_fallback(self):
		notify._set_tray(None)
		with patch.object(notify, "toast", side_effect=AssertionError("No closed-app fallback")):
			self.evaluate()
		self.assertFalse(self.receipts.exists())

	def test_concurrent_evaluation_claims_only_once(self):
		threads = [threading.Thread(target=self.evaluate) for _ in range(8)]
		for thread in threads: thread.start()
		for thread in threads: thread.join(2)
		self.assertFalse(any(thread.is_alive() for thread in threads))
		self.tray.notify.assert_called_once()

	def test_tray_rechecks_edge_snapshot_even_when_other_display_fields_are_unchanged(self):
		item = tray.Tray(Mock(), Mock(), Mock(), Mock())
		with patch.object(notify, "edge_reminder") as remind:
			item.update(self.overview)
			item.update(self.overview)
			updated = copy.deepcopy(self.overview)
			updated["edge_streak"]["accounts"][0]["edge_minutes"] = 19
			item.update(updated)
		self.assertEqual(remind.call_count, 3)
		self.assertEqual(item._overview["edge_streak"]["accounts"][0]["edge_minutes"], 19)

	def test_delivery_runs_outside_the_tray_lock_and_uses_existing_notify_method(self):
		item = tray.Tray(Mock(), Mock(), Mock(), Mock())
		notify._set_tray(item)
		def deliver(title, body):
			self.assertFalse(item._lock._is_owned())
			return True
		with patch.object(item, "notify", side_effect=deliver) as deliver_mock:
			clock = Mock(wraps=datetime)
			clock.now.return_value = self.now
			with patch.object(tray, "_dpi", return_value=96), patch.object(tray, "i18n", SimpleNamespace(current=lambda: self.t)), patch.object(notify, "datetime", clock):
				# The real update entrypoint has to reach reminder evaluation, not only a direct helper call.
				item.update(self.overview)
		deliver_mock.assert_called_once()
