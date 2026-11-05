import _bootstrap  # noqa: F401

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import shutil
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import identity
import referrals
from rfapp import accounts_admin as admin, invite_onboarding as invite, paths, settings
from rfapp import signin, prefs
from rfapp.bridge import Bridge


class InviteFixture(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		self.profiles = self.home / "profiles"
		self.profiles.mkdir()
		(self.profiles / "account-1").mkdir()
		self.stack = contextlib.ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.dict(os.environ, {"REWARDS_FARMER_DATA_HOME": str(self.home), "USER_DATA_DIR": str(self.profiles)}))
		(self.home / ".env").write_text("REWARDS_ACCOUNTS=account-1\nUSER_DATA_DIR=" + str(self.profiles) + "\n", encoding="utf-8")
		value = identity.load(self.home)
		value["bindings"]["account-1"] = {"account_id": "account-1", "expected_name_hash": identity.name_hash("Synthetic Source"), "confirmed_at": identity.utc()}
		identity.save(value, self.home)
		referrals.save_referral_state("account-1", referrals.ReferralState("https://rewards.bing.com/refer?code=SYNTHETIC_SOURCE", True), read_at="2026-10-29T21:00:00Z", run_id="synthetic", directory=self.home)
		self.api = Bridge(dependencies={"accounts_admin": admin})
		self.addCleanup(self.api._close)

	def good(self, answer):
		self.assertTrue(answer["ok"], answer.get("error"))
		return answer["data"]

	def preview(self):
		return self.good(self.api.preview_invite_account({"source_account_id": "account-1"}))["invitation"]

	def start_owned(self):
		preview = self.preview()
		invite.launch(preview["operation_id"], preview["preview_token"])
		owned = invite.child_begin(preview["operation_id"], preview["target_account_id"])
		opened = invite.child_phase(preview["operation_id"], "invite_open")
		return preview, owned, opened


class FoundationTests(InviteFixture):
	def test_real_bridge_entry_owned_child_and_admission_foundation(self):
		(self.home / "synthetic-foundation.json").write_text('{"synthetic":true}')
		preview = self.preview()
		self.assertEqual(preview["phase"], "preview")
		self.assertEqual(settings.active_accounts(), ["account-1"])
		self.assertFalse((self.profiles / preview["target_account_id"]).exists())
		self.assertNotIn("link", self.good(self.api.list_invite_operations({}))["operations"][0])
		children, commands = [], []
		real_popen = subprocess.Popen
		def launch(command, **kwargs):
			commands.append(command)
			assert all("https://" not in str(part) and "SYNTHETIC_SOURCE" not in str(part) for part in command)
			proc = real_popen([sys.executable, "-B", str(Path(__file__).with_name("invite_child.py")), *command[2:]], cwd=self.home, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
			children.append(proc)
			return proc
		def wait_phase(phase):
			deadline = time.monotonic() + 30
			while time.monotonic() < deadline:
				operation = invite.read(preview["operation_id"])
				if operation["phase"] == phase: return operation
				if children and children[0].poll() is not None:
					out, err = children[0].communicate()
					self.fail("Synthetic child exited before phase " + phase + ": " + out.decode(errors="replace") + err.decode(errors="replace"))
				time.sleep(.05)
			self.fail("Synthetic foundation phase deadline: " + phase)
		with patch.object(admin.subprocess, "Popen", side_effect=launch):
			started = self.good(self.api.start_invite_account({"operation_id": preview["operation_id"], "preview_token": preview["preview_token"]}))
		self.assertEqual(started["account_id"], preview["target_account_id"])
		try:
			opened = wait_phase("invite_open")
			self.assertEqual(json.loads((self.home / "driver-events.json").read_text()), ["invite"])
			self.assertIsNone(identity.pending(preview["target_account_id"], self.home))
			self.assertEqual(settings.active_accounts(), ["account-1"])
			bad = self.api.continue_invite_account({"operation_id": preview["operation_id"], "continue_token": "0" * 32})
			self.assertFalse(bad["ok"])
			self.assertEqual(invite.read(preview["operation_id"])["phase"], "invite_open")
			self.good(self.api.continue_invite_account({"operation_id": preview["operation_id"], "continue_token": opened["continue_token"]}))
			self.assertFalse(self.api.continue_invite_account({"operation_id": preview["operation_id"], "continue_token": opened["continue_token"]})["ok"])
			wait_phase("awaiting_confirmation")
			out, err = children[0].communicate(timeout=30)
			self.assertEqual(children[0].returncode, 0, out.decode(errors="replace") + err.decode(errors="replace"))
			self.assertNotIn(b"SYNTHETIC_SOURCE", out + err)
			self.assertEqual(json.loads((self.home / "driver-events.json").read_text()), ["invite", "dashboard", "quit"])
			self.assertEqual(settings.active_accounts(), ["account-1"])
			pending = self.good(self.api.get_enrollment_status({"account_id": preview["target_account_id"]}))
			self.assertEqual(pending["observed_name"], "Synthetic New Person")
			self.assertFalse(self.api.confirm_enrollment({"account_id": preview["target_account_id"], "confirmation_token": pending["confirmation_token"], "confirmed": False})["ok"])
			with patch("rfapp.schedule.ensure", return_value={"in_sync": True}) as schedule:
				self.good(self.api.confirm_enrollment({"account_id": preview["target_account_id"], "confirmation_token": pending["confirmation_token"], "confirmed": True}))
				schedule.assert_called_once()
			self.assertEqual(settings.active_accounts(), ["account-1", preview["target_account_id"]])
			self.assertEqual(invite.read(preview["operation_id"])["phase"], "registered")
			self.assertEqual(invite.read(preview["operation_id"])["referral_status"], "unknown")
			self.assertIsNotNone(identity.binding(preview["target_account_id"], self.home))
			print("F119_FOUNDATION_PASS disposable_store real_bridge real_entry tracked_child invite_continue_confirm_registration")
		finally:
			for child in children:
				if child.poll() is None:
					child.terminate()
					child.communicate(timeout=10)


class OperationTests(InviteFixture):
	def test_revision_terminal_reconfirm_and_stale_corrupt_metadata_recovery(self):
		preview, owned, opened = self.start_owned()
		op_id, target = preview["operation_id"], preview["target_account_id"]
		invite.continue_operation(op_id, opened["continue_token"], lambda pid: True)
		invite.child_phase(op_id, "verifying")
		invite.stage_confirmation(op_id, "Synthetic New Person")
		pending = identity.pending(target, self.home)
		with patch.object(admin, "_ensure_idle"), patch("rfapp.schedule.ensure"):
			admin.confirm_enrollment(target, pending["token"], True)
			self.assertIsNone(invite.for_target(target))
			# Terminal history cannot veto ordinary sign-in or reconfirmation,
			# including re-enrollment after a lost local binding.
			with patch.object(admin, "_start_signin", return_value={"state": "starting"}) as start:
				admin.begin_enrollment(target)
				start.assert_called_once_with(target, False, enroll=True)
			with patch("run_lock.acquire", return_value=None) as lock:
				self.assertEqual(signin.run(target, False, enroll=True), 3)
				lock.assert_called_once()
			value = identity.load(self.home); value["bindings"].pop(target); identity.save(value, self.home)
			ordinary = identity.stage_enrollment(target, "Synthetic New Person", new=False, directory=self.home)
			self.assertEqual(admin.confirm_enrollment(target, ordinary["token"], True)["identity_status"], "confirmed")
		# A second operation has an owned profile and a pending identity at the
		# old root. Root change exposes only safe metadata and cannot recycle it.
		preview2, owned2, opened2 = self.start_owned()
		op2, target2 = preview2["operation_id"], preview2["target_account_id"]
		invite.continue_operation(op2, opened2["continue_token"], lambda pid: True)
		invite.child_phase(op2, "verifying"); invite.stage_confirmation(op2, "Synthetic Third")
		old_file = owned2["profile"] / "Keep"; old_file.write_bytes(b"synthetic old root bytes")
		signin._write_status(target2, "awaiting_confirmation", invitation=invite.read(op2))
		new_root = self.home / "changed-root"
		with patch.object(settings, "user_data_dir", return_value=new_root), patch.object(admin, "_recycle", side_effect=AssertionError("Never recycle stale root")):
			rows = admin.list_invite_operations()["operations"]
			row = next(row for row in rows if row["operation_id"] == op2)
			self.assertTrue(row["recovery_only"]); self.assertFalse(row["can_resume"]); self.assertTrue(row["can_discard"])
			self.assertEqual(row["error_code"], "accounts.invite_stale")
			self.assertEqual(admin.get_signin_status(target2)["state"], "invite_interrupted")
			with patch.object(admin, "_ensure_idle"):
				self.assertEqual(admin.discard_invite_account(op2)["invitation"]["phase"], "discarded")
		self.assertIsNone(identity.pending(target2, self.home))
		self.assertEqual(old_file.read_bytes(), b"synthetic old root bytes")
		self.assertFalse(new_root.exists())
		# Corrupt envelope has a revision-bound forget handle. Forgetting it
		# restores the operation store, not any browser/profile state.
		(self.home / "invite-onboarding.json").write_text("not JSON synthetic", encoding="utf-8")
		bad = invite.list_operations(lambda pid: False)[0]
		self.assertTrue(bad["recovery_only"])
		self.assertIsNone(invite.for_target(target))
		with patch.object(admin, "_recycle", side_effect=AssertionError("No profile access")):
			invite.discard(bad["operation_id"], admin._recycle, lambda pid: False)
		self.assertEqual(invite.list_operations(lambda pid: False), [])
		# A malformed record cannot abort projection of valid sibling records.
		path = self.home / "invite-onboarding.json"
		value = json.loads(path.read_text())
		value["operations"]["invite-" + "e" * 32] = {"phase": [], "target_account_id": target}
		path.write_text(json.dumps(value))
		self.assertTrue(invite.prepare("account-1", lambda used: "account-9")["phase"] == "preview")
		self.assertEqual(len(invite.list_operations(lambda pid: False)), 2)
		self.assertTrue(json.loads((self.home / "invite-onboarding.json").read_text())["recovery"])

	def test_revision_admission_exception_and_cancel_during_verifying_cleanup(self):
		preview, owned, opened = self.start_owned()
		op_id, target = preview["operation_id"], preview["target_account_id"]
		invite.continue_operation(op_id, opened["continue_token"], lambda pid: True)
		invite.child_phase(op_id, "verifying"); invite.stage_confirmation(op_id, "Synthetic New Person")
		pending = identity.pending(target, self.home)
		signin._write_status(target, "verifying", invitation=invite.read(op_id))
		self.assertEqual(admin.get_signin_status(target)["state"], "awaiting_confirmation")
		before_env = paths.env_path().read_bytes()
		real_save = identity.save
		def broken_commit(value, directory=None):
			if target in value["bindings"]: raise RuntimeError("private continuation payload")
			return real_save(value, directory)
		with patch.object(admin, "_ensure_idle"), patch.object(identity, "save", side_effect=broken_commit), patch("rfapp.schedule.ensure") as schedule:
			with self.assertRaises(ValueError) as error: admin.confirm_enrollment(target, pending["token"], True)
			self.assertEqual(error.exception.code, "accounts.enrollment_save_failed"); schedule.assert_not_called()
		self.assertEqual(paths.env_path().read_bytes(), before_env)
		self.assertIsNone(identity.binding(target, self.home)); self.assertIsNone(identity.pending(target, self.home))
		self.assertEqual(invite.read(op_id)["phase"], "interrupted")
		# Admission can fail after its phase was already atomically replaced.
		# Retaining op_id before that write lets rollback leave it resumable.
		fresh = admin.preview_invite_account("account-1", op_id)["invitation"]
		invite.launch(op_id, fresh["preview_token"]); invite.child_begin(op_id, target)
		opened = invite.child_phase(op_id, "invite_open")
		invite.continue_operation(op_id, opened["continue_token"], lambda pid: True)
		invite.child_phase(op_id, "verifying"); invite.stage_confirmation(op_id, "Synthetic New Person")
		pending = identity.pending(target, self.home)
		real_admit = invite.admit
		def after_admitting(target, token):
			real_admit(target, token)
			raise RuntimeError("synthetic post-replace failure")
		with patch.object(admin, "_ensure_idle"), patch.object(invite, "admit", side_effect=after_admitting), patch("rfapp.schedule.ensure") as schedule:
			with self.assertRaises(ValueError): admin.confirm_enrollment(target, pending["token"], True)
			schedule.assert_not_called()
		self.assertEqual(invite.read(op_id)["phase"], "interrupted")
		self.assertIsNone(identity.pending(target, self.home))
		fresh = admin.preview_invite_account("account-1", op_id)["invitation"]
		invite.launch(op_id, fresh["preview_token"]); invite.child_begin(op_id, target)
		opened = invite.child_phase(op_id, "invite_open")
		invite.continue_operation(op_id, opened["continue_token"], lambda pid: True)
		invite.child_phase(op_id, "verifying")
		# Cancellation wins the state lock before the child stages identity.
		signin._write_status(target, "verifying", invitation=invite.read(op_id))
		admin.cancel_signin(target)
		with self.assertRaises(invite.InviteError): invite.stage_confirmation(op_id, "Synthetic New Person")
		self.assertIsNone(identity.pending(target, self.home))
		self.assertEqual(admin.get_signin_status(target)["state"], "cancelled")
		# If staging wins instead, cancellation still removes its tagged token.
		fresh = admin.preview_invite_account("account-1", op_id)["invitation"]
		invite.launch(op_id, fresh["preview_token"]); invite.child_begin(op_id, target)
		opened = invite.child_phase(op_id, "invite_open")
		invite.continue_operation(op_id, opened["continue_token"], lambda pid: True)
		invite.child_phase(op_id, "verifying")
		signin._write_status(target, "verifying", invitation=invite.read(op_id))
		invite.stage_confirmation(op_id, "Synthetic New Person")
		admin.cancel_signin(target)
		self.assertIsNone(identity.pending(target, self.home))
		self.assertEqual(invite.read(op_id)["phase"], "cancelled")
		# Legacy untagged pending token cannot re-enter after cancel either.
		legacy = identity.stage_enrollment(target, "Synthetic New Person", new=True, directory=self.home)
		store_path = self.home / "invite-onboarding.json"
		stored = json.loads(store_path.read_text())
		stored["operations"][op_id]["enrollment_token_hash"] = invite._digest(legacy["token"])
		store_path.write_text(json.dumps(stored))
		with patch.object(admin, "_ensure_idle"), patch("rfapp.schedule.ensure") as schedule:
			with self.assertRaises(invite.InviteError): admin.confirm_enrollment(target, legacy["token"], True)
			schedule.assert_not_called()
		value = identity.load(self.home); value["pending"].pop(target, None); identity.save(value, self.home)
		with patch.object(admin, "_ensure_idle"), patch.object(admin, "_start_signin", return_value={"state": "starting"}):
			self.assertIn("account_id", admin.add_account())
	def test_symlinked_operation_store_and_profile_root_fail_closed(self):
		with patch.object(Path, "is_symlink", return_value=True):
			with self.assertRaises(invite.InviteError): invite.reserved_targets()
		self.assertFalse((self.home / "invite-onboarding.json").exists())

	def test_mark_creation_interruption_keeps_owned_empty_profile_recoverable(self):
		preview = self.preview()
		invite.launch(preview["operation_id"], preview["preview_token"])
		original = identity.write_document
		def stop_marker(path, value):
			if Path(path).name == ".invite-onboarding-owner.json": raise KeyboardInterrupt()
			return original(path, value)
		with patch.object(identity, "write_document", side_effect=stop_marker):
			with self.assertRaises(KeyboardInterrupt): invite.child_begin(preview["operation_id"], preview["target_account_id"])
		profile = self.profiles / preview["target_account_id"]
		self.assertTrue(profile.is_dir())
		invite.interrupt(preview["operation_id"])
		fresh = admin.preview_invite_account("account-1", preview["operation_id"])["invitation"]
		invite.launch(fresh["operation_id"], fresh["preview_token"])
		self.assertEqual(invite.child_begin(fresh["operation_id"], fresh["target_account_id"])["profile"], profile)
		self.assertTrue((profile / ".invite-onboarding-owner.json").is_file())

	def test_bridge_storage_exception_message_never_logged_or_exported(self):
		import logging
		private = "https://rewards.bing.com/refer?code=SYNTHETIC_SOURCE"
		with patch.object(admin, "list_invite_operations", side_effect=OSError(private)), patch.object(logging.Logger, "exception") as log:
			answer = self.api.list_invite_operations({})
		self.assertEqual(answer["error"]["code"], "accounts.invite_storage")
		self.assertNotIn(private, json.dumps(answer))
		log.assert_not_called()
	def test_explicit_source_a_b_revision_and_no_default_or_arbitrary_url(self):
		(self.profiles / "account-2").mkdir()
		(self.home / ".env").write_text("REWARDS_ACCOUNTS=account-1,account-2\nUSER_DATA_DIR=" + str(self.profiles) + "\n")
		value = identity.load(self.home)
		value["bindings"]["account-2"] = {"account_id": "account-2", "expected_name_hash": identity.name_hash("Synthetic Source Two"), "confirmed_at": identity.utc()}
		identity.save(value, self.home)
		second = "https://rewards.bing.com/refer?code=SYNTHETIC_SECOND"
		referrals.save_referral_state("account-2", referrals.ReferralState(second, True), read_at="2026-10-29T21:00:00Z", run_id="synthetic", directory=self.home)
		for request in ({}, {"source_account_id": None}, {"source_account_id": "friend-" + "0" * 32}, {"source_account_id": "account-1", "link": second}):
			self.assertFalse(self.api.preview_invite_account(request)["ok"])
		preview = self.good(self.api.preview_invite_account({"source_account_id": "account-2"}))["invitation"]
		self.assertEqual(preview["link"], second)
		self.assertEqual(preview["source_account_id"], "account-2")
		self.assertEqual(preview["target_account_id"], "account-3")
		self.assertFalse(self.api.preview_invite_account({"source_account_id": "account-1", "operation_id": preview["operation_id"]})["ok"])

	def test_stale_preview_never_starts_or_falls_back_and_repreview_is_explicit(self):
		preview = self.preview()
		referrals.save_referral_state("account-1", referrals.ReferralState("https://rewards.bing.com/refer?code=SYNTHETIC_NEW", True), read_at="2026-10-29T22:00:00Z", run_id="synthetic-new", directory=self.home)
		with patch.object(admin, "_start_signin") as start:
			answer = self.api.start_invite_account({"operation_id": preview["operation_id"], "preview_token": preview["preview_token"]})
			self.assertEqual(answer["error"]["code"], "accounts.invite_stale")
			start.assert_not_called()
		fresh = self.good(self.api.preview_invite_account({"source_account_id": "account-1", "operation_id": preview["operation_id"]}))["invitation"]
		self.assertEqual(fresh["target_account_id"], preview["target_account_id"])
		self.assertNotEqual(fresh["preview_token"], preview["preview_token"])
		with self.assertRaises(invite.InviteError): invite.launch(fresh["operation_id"], preview["preview_token"])
		self.assertTrue(fresh["link"].endswith("SYNTHETIC_NEW"))

	def test_expired_preview_wrong_token_and_duplicate_launch_denied(self):
		preview = self.preview()
		with self.assertRaises(invite.InviteError): invite.launch(preview["operation_id"], "0" * 32)
		path = self.home / "invite-onboarding.json"
		value = json.loads(path.read_text())
		value["operations"][preview["operation_id"]]["preview_at"] = "2026-01-01T00:00:00Z"
		path.write_text(json.dumps(value))
		with self.assertRaises(invite.InviteError) as failure: invite.launch(preview["operation_id"], preview["preview_token"])
		self.assertEqual(failure.exception.code, "accounts.invite_expired")
		value["operations"][preview["operation_id"]]["preview_at"] = identity.utc()
		path.write_text(json.dumps(value))
		invite.launch(preview["operation_id"], preview["preview_token"])
		with self.assertRaises(invite.InviteError): invite.launch(preview["operation_id"], preview["preview_token"])

	def test_continue_is_serialized_single_use_and_cross_operation_denied(self):
		preview, _, opened = self.start_owned()
		answers = []
		def attempt():
			try: invite.continue_operation(preview["operation_id"], opened["continue_token"], lambda pid: True); answers.append("ok")
			except invite.InviteError: answers.append("denied")
		threads = [threading.Thread(target=attempt) for _ in range(4)]
		for thread in threads: thread.start()
		for thread in threads: thread.join(5)
		self.assertEqual(sorted(answers), ["denied", "denied", "denied", "ok"])
		with self.assertRaises(invite.InviteError): invite.continue_operation("invite-" + "0" * 32, opened["continue_token"], lambda pid: True)
		self.assertEqual(settings.active_accounts(), ["account-1"])
		self.assertIsNone(identity.binding(preview["target_account_id"], self.home))

	def test_interruption_preserves_profile_resume_rotates_token_and_discard_is_owned_only(self):
		preview, owned, opened = self.start_owned()
		marker = owned["profile"] / "synthetic-session"
		marker.write_bytes(b"keep signed-in synthetic bytes")
		operations = invite.list_operations(lambda pid: False)
		self.assertEqual(operations[0]["phase"], "interrupted")
		self.assertEqual(marker.read_bytes(), b"keep signed-in synthetic bytes")
		fresh = admin.preview_invite_account("account-1", preview["operation_id"])["invitation"]
		invite.launch(fresh["operation_id"], fresh["preview_token"])
		invite.child_begin(fresh["operation_id"], fresh["target_account_id"])
		resumed = invite.child_phase(fresh["operation_id"], "invite_open")
		self.assertNotEqual(resumed["continue_token"], opened["continue_token"])
		with self.assertRaises(invite.InviteError): invite.continue_operation(fresh["operation_id"], opened["continue_token"], lambda pid: True)
		invite.interrupt(fresh["operation_id"], cancelled=True)
		with patch.object(admin, "_ensure_idle"), patch.object(admin, "_recycle", side_effect=lambda items: shutil.rmtree(items[0]) or True) as recycle:
			result = admin.discard_invite_account(fresh["operation_id"])
		self.assertEqual(result["invitation"]["phase"], "discarded")
		self.assertEqual(recycle.call_args.args[0], [owned["profile"]])
		self.assertTrue((self.profiles / "account-1").exists())
		self.assertIn(fresh["target_account_id"], identity.load(self.home)["used_ids"])

	def test_profile_collision_tampered_path_or_marker_never_operates_existing_profile(self):
		preview = self.preview()
		invite.launch(preview["operation_id"], preview["preview_token"])
		profile = self.profiles / preview["target_account_id"]
		profile.mkdir()
		(profile / "Keep").write_bytes(b"unowned")
		with self.assertRaises(invite.InviteError): invite.child_begin(preview["operation_id"], preview["target_account_id"])
		self.assertEqual((profile / "Keep").read_bytes(), b"unowned")
		with self.assertRaises(invite.InviteError): invite.child_begin(preview["operation_id"], "account-1")
		for bad in ("../invite", "invite-1", None, ["unsafe"]):
			self.assertFalse(self.api.start_invite_account({"operation_id": bad, "preview_token": "0" * 32})["ok"])
		path = self.home / "invite-onboarding.json"
		value = json.loads(path.read_text())
		value["operations"][preview["operation_id"]]["profile_root"] = str(self.home / "other")
		path.write_text(json.dumps(value))
		with self.assertRaises(invite.InviteError): invite.read(preview["operation_id"])

	def test_retained_link_warning_is_dated_and_unsafe_query_refuses(self):
		referrals.save_referral_state("account-1", referrals.ReferralState(unreadable="page_unknown"), read_at="2026-10-29T22:00:00Z", run_id="synthetic-failure", directory=self.home)
		preview = self.preview()
		self.assertEqual(preview["unreadable"], "page_unknown")
		self.assertEqual(preview["link_read_at"], "2026-10-29T21:00:00Z")
		prefix = "https://rewards.bing.com/refer?"
		for link in ("javascript:alert(1)", "https://rewards.bing.com.evil.invalid/refer", prefix + "code=%0Asecret", prefix + "code=%xx", prefix + "&".join(["code=SYNTHETIC"] * 40)):
			with self.assertRaises(invite.InviteError): invite._link(link)

	def test_root_migration_or_unconfirmed_identity_fails_closed_without_mutation(self):
		before = (self.home / ".env").read_bytes()
		(self.profiles / "Local State").write_bytes(b"synthetic-main")
		self.assertEqual(self.api.preview_invite_account({"source_account_id": "account-1"})["error"]["code"], "accounts.invite_root")
		self.assertEqual((self.home / ".env").read_bytes(), before)
		(self.profiles / "Local State").unlink()
		value = identity.load(self.home); value["bindings"] = {}; identity.save(value, self.home)
		self.assertEqual(self.api.preview_invite_account({"source_account_id": "account-1"})["error"]["code"], "accounts.invite_ineligible")
		self.assertFalse((self.home / "invite-onboarding.json").exists())


class ChildPhaseTests(InviteFixture):
	def invoke(self, mode="continue", identity_status="match"):
		preview = self.preview()
		invite.launch(preview["operation_id"], preview["preview_token"])
		browser = ModuleType("browser")
		browser.build_options = Mock(side_effect=lambda account: account)
		browser.build_service = Mock(return_value=None)
		rewards = ModuleType("rewards_state")
		rewards.read_balance = Mock(side_effect=AssertionError("No balance probe"))
		rewards.read_state = Mock(side_effect=AssertionError("No hidden verification"))
		from selenium import webdriver
		from types import SimpleNamespace
		events, old_sessions = [], []
		clock = SimpleNamespace(now=0)
		continue_count = []
		class Driver:
			def __init__(self, options, service): old_sessions.append(options)
			def set_page_load_timeout(self, value): pass
			def get(self, url):
				events.append("dashboard" if url == "https://rewards.bing.com/" else "invite")
				if mode == "navigate_error": raise RuntimeError(url)
			@property
			def window_handles(self):
				if mode == "closed": return []
				if mode == "cancel": signin._cancel_path(preview["target_account_id"]).touch()
				if mode == "continue" and not continue_count:
					opened = invite.read(preview["operation_id"])
					self_test.assertEqual(opened["phase"], "invite_open")
					self_test.assertEqual(events, ["invite"])
					invite.continue_operation(preview["operation_id"], opened["continue_token"], lambda pid: True)
					continue_count.append(1)
				return ["synthetic"]
			def quit(self): events.append("quit")
		self_test = self
		def tick(seconds): clock.now += 901 if mode == "timeout" else seconds
		identity_read = Mock(return_value=(identity_status, identity.name_hash("Synthetic New Person") if identity_status == "match" else None))
		with patch.dict(sys.modules, {"browser": browser, "rewards_state": rewards}), patch.object(webdriver, "Edge", Driver), patch.object(signin.time, "monotonic", side_effect=lambda: clock.now), patch.object(signin.time, "sleep", side_effect=tick), patch.object(admin, "_profile_identity", return_value={"name": "Synthetic New Person"}), patch.object(identity, "read_dashboard_identity", identity_read), patch("rfapp.schedule.ensure") as schedule:
			result = signin.run(preview["target_account_id"], True, enroll=True, invitation_id=preview["operation_id"])
			schedule.assert_not_called()
		self.assertEqual(settings.active_accounts(), ["account-1"])
		self.assertNotIn("SYNTHETIC_SOURCE", json.dumps(json.loads(signin._status_path(preview["target_account_id"]).read_text())))
		self.assertEqual(old_sessions[0].user_data_dir, str(self.profiles / preview["target_account_id"]))
		return preview, result, events, identity_read, rewards

	def test_close_does_not_probe_dashboard_identity_or_hidden_driver(self):
		preview, result, events, identity_read, rewards = self.invoke("closed")
		self.assertEqual(result, 1)
		self.assertEqual(events, ["invite", "quit"])
		identity_read.assert_not_called()
		rewards.read_balance.assert_not_called(); rewards.read_state.assert_not_called()
		self.assertEqual(invite.read(preview["operation_id"])["phase"], "interrupted")
		self.assertTrue((self.profiles / preview["target_account_id"]).exists())

	def test_revision_invitation_driver_log_is_silent_and_ordinary_setting_unchanged(self):
		from selenium import webdriver
		private_log = str(self.home / "MUST-NOT-CREATE-driver.log")
		with patch.dict(os.environ, {"REWARDS_DRIVER_LOG": private_log}), patch.object(webdriver, "EdgeService", return_value=object()) as service:
			preview, result, events, identity_read, rewards = self.invoke("closed")
			self.assertEqual(os.environ["REWARDS_DRIVER_LOG"], private_log)
		service.assert_called_once_with(executable_path=os.environ.get("MSEDGEDRIVER_PATH") or None, service_args=["--silent"], log_output=os.devnull)
		self.assertFalse(Path(private_log).exists())
		self.assertEqual(events, ["invite", "quit"])
		self.assertEqual(result, 1)
		self.assertEqual(invite.read(preview["operation_id"])["phase"], "interrupted")

	def test_cancel_timeout_and_navigation_exception_keep_profile_unregistered(self):
		# Independent fixture per case so the pending operation cannot silently
		# be replaced by another invitation during the matrix.
		for mode in ("cancel", "timeout", "navigate_error"):
			with self.subTest(mode=mode):
				if mode != "cancel":
					last = invite.list_operations(lambda pid: False)
					for op in last: invite.discard(op["operation_id"], lambda items: shutil.rmtree(items[0]) or True, lambda pid: False)
				preview, result, events, identity_read, rewards = self.invoke(mode)
				self.assertEqual(result, 1)
				self.assertEqual(events, ["invite", "quit"])
				identity_read.assert_not_called()
				self.assertIsNone(identity.binding(preview["target_account_id"], self.home))
				self.assertIsNone(identity.pending(preview["target_account_id"], self.home))

	def test_continue_enters_identity_once_but_cannot_register_automatically(self):
		preview, result, events, identity_read, rewards = self.invoke()
		self.assertEqual(result, 0)
		self.assertEqual(events, ["invite", "dashboard", "quit"])
		identity_read.assert_called_once()
		rewards.read_balance.assert_not_called(); rewards.read_state.assert_not_called()
		self.assertEqual(invite.read(preview["operation_id"])["phase"], "awaiting_confirmation")
		self.assertIsNotNone(identity.pending(preview["target_account_id"], self.home))
		self.assertIsNone(identity.binding(preview["target_account_id"], self.home))

	def test_identity_unknown_and_mismatch_never_create_enrollment(self):
		for status in ("unknown", "mismatch"):
			with self.subTest(status=status):
				if status == "mismatch":
					for op in invite.list_operations(lambda pid: False): invite.discard(op["operation_id"], lambda items: shutil.rmtree(items[0]) or True, lambda pid: False)
				preview, result, events, identity_read, rewards = self.invoke(identity_status=status)
				self.assertEqual(result, 1)
				self.assertEqual(events, ["invite", "dashboard", "quit"])
				self.assertIsNone(identity.pending(preview["target_account_id"], self.home))
				self.assertEqual(invite.read(preview["operation_id"])["referral_status"], "unknown")

	def test_storage_failure_atomic_and_no_link_in_status_errors_or_argv(self):
		preview = self.preview()
		before = (self.home / "invite-onboarding.json").read_bytes()
		with patch.object(identity, "replace_file", side_effect=OSError("synthetic store busy")):
			with self.assertRaises(OSError): invite.launch(preview["operation_id"], preview["preview_token"])
		self.assertEqual((self.home / "invite-onboarding.json").read_bytes(), before)
		for response in (self.api.list_invite_operations({}), self.api.continue_invite_account({"operation_id": preview["operation_id"], "continue_token": "0" * 32})):
			self.assertNotIn("SYNTHETIC_SOURCE", json.dumps(response))

	def test_friends_never_convert_and_ordinary_signin_cannot_bypass_invite_continuation(self):
		friend = prefs.save_invited_friend("Synthetic Friend", "friend@example.invalid")
		before = prefs.invited_friends()
		self.assertFalse(self.api.preview_invite_account({"source_account_id": friend["id"]})["ok"])
		preview = self.preview()
		with patch.object(signin, "_run_invitation", side_effect=AssertionError("Do not enter invite branch")):
			self.assertEqual(signin.run(preview["target_account_id"], True, enroll=True), 2)
		self.assertEqual(prefs.invited_friends(), before)
		self.assertEqual(settings.active_accounts(), ["account-1"])

	def test_cancelled_confirmation_token_cannot_register_and_identity_mismatch_never_rebinds(self):
		preview, _, opened = self.start_owned()
		invite.continue_operation(preview["operation_id"], opened["continue_token"], lambda pid: True)
		invite.child_phase(preview["operation_id"], "verifying")
		pending = identity.stage_enrollment(preview["target_account_id"], "Synthetic New Person", new=True, directory=self.home)
		invite.child_phase(preview["operation_id"], "awaiting_confirmation", pending["token"])
		invite.interrupt(preview["operation_id"], cancelled=True)
		with patch.object(admin, "_ensure_idle"), patch("rfapp.schedule.ensure") as schedule:
			answer = self.api.confirm_enrollment({"account_id": preview["target_account_id"], "confirmation_token": pending["token"], "confirmed": True})
		self.assertFalse(answer["ok"])
		self.assertEqual(settings.active_accounts(), ["account-1"])
		schedule.assert_not_called()

	def test_admission_crash_reconciliation_never_registers_without_complete_commit(self):
		preview, _, opened = self.start_owned()
		invite.continue_operation(preview["operation_id"], opened["continue_token"], lambda pid: True)
		invite.child_phase(preview["operation_id"], "verifying")
		pending = identity.stage_enrollment(preview["target_account_id"], "Synthetic New Person", new=True, directory=self.home)
		invite.child_phase(preview["operation_id"], "awaiting_confirmation", pending["token"])
		invite.admit(preview["target_account_id"], pending["token"])
		self.assertEqual(invite.list_operations(lambda pid: False)[0]["phase"], "interrupted")
		self.assertEqual(settings.active_accounts(), ["account-1"])

	def test_unchanged_normal_add_uses_original_entry_and_can_still_return_migration_card(self):
		with patch.object(admin, "_ensure_idle"), patch.object(admin, "_start_signin", return_value={"state": "starting"}) as start:
			answer = admin.add_account()
		self.assertEqual(answer["account_id"], "account-2")
		start.assert_called_once_with("account-2", True, enroll=True)
		self.assertFalse((self.home / "invite-onboarding.json").exists())
