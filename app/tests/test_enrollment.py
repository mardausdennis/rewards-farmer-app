import _bootstrap  # noqa: F401

import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import identity
from rfapp import accounts_admin as admin, paths, prefs, settings, signin
from rfapp.bridge import Bridge
from rfapp.envfile import EnvFile


NAME = "Synthetic Person"


class EnrollmentTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		self.profiles = self.home / "profiles"
		self.profiles.mkdir()
		self.stack = contextlib.ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.dict(os.environ, {"REWARDS_FARMER_DATA_HOME": str(self.home), "USER_DATA_DIR": str(self.profiles)}))
		(self.home / ".env").write_text("USER_DATA_DIR=" + str(self.profiles) + "\nREWARDS_ACCOUNTS=account-1\n")
		(self.profiles / "account-1").mkdir()
		self.stack.enter_context(patch.object(admin, "_ensure_idle"))
		self.stack.enter_context(patch.object(admin, "_picture_source", return_value=None))
		self.stack.enter_context(patch.object(admin, "_profile_identity", return_value={"name": NAME, "email": "synthetic@example.invalid"}))
		self.ensure = self.stack.enter_context(patch("rfapp.schedule.ensure"))
		self.start = self.stack.enter_context(patch.object(admin, "_start_signin", return_value={"state": "starting"}))

	def confirm(self, account="account-1", new=False):
		pending = identity.stage_enrollment(account, NAME, new=new, directory=self.home)
		return admin.confirm_enrollment(account, pending["token"], True)

	def test_existing_first_is_unchanged_until_own_explicit_confirmation(self):
		before = (self.home / ".env").read_bytes()
		self.assertFalse(identity.managed(self.home))
		with self.assertRaises(ValueError) as error: admin.add_account()
		self.assertEqual(error.exception.code, "accounts.enrollment_required")
		self.start.assert_not_called()
		self.assertEqual((self.home / ".env").read_bytes(), before)
		admin.begin_enrollment("account-1")
		self.start.assert_called_once_with("account-1", False, enroll=True)
		self.assertIsNone(identity.binding("account-1", self.home))
		self.assertFalse(self.confirm()["managed"])
		self.assertIsNotNone(identity.binding("account-1", self.home))
		self.assertEqual((self.home / ".env").read_bytes(), before)

	def test_second_not_registered_until_token_and_true_confirmation(self):
		self.confirm()
		answer = admin.add_account()
		self.assertEqual(answer["account_id"], "account-2")
		self.start.assert_called_once_with("account-2", True, enroll=True)
		pending = identity.stage_enrollment("account-2", "Synthetic Other", new=True, directory=self.home)
		self.assertEqual(settings.active_accounts(), ["account-1"])
		for token, yes in ((pending["token"], False), ("wrong", True), (pending["token"], 1)):
			with self.assertRaises(ValueError): admin.confirm_enrollment("account-2", token, yes)
			self.assertIsNone(identity.binding("account-2", self.home))
			self.assertEqual(settings.active_accounts(), ["account-1"])
		answer = admin.confirm_enrollment("account-2", pending["token"], True)
		self.assertTrue(answer["managed"])
		self.assertEqual(settings.active_accounts(), ["account-1", "account-2"])
		self.assertTrue(prefs.load()["identity_managed"])
		self.assertTrue(identity.managed(self.home))
		self.assertEqual(set(identity.binding("account-2", self.home)), {"account_id", "expected_name_hash", "confirmed_at"})
		with self.assertRaises(ValueError): admin.confirm_enrollment("account-2", pending["token"], True)

	def test_status_shows_observed_name_only_in_confirmation_surface(self):
		pending = identity.stage_enrollment("account-1", NAME, new=False, directory=self.home)
		api = Bridge(dependencies={"accounts_admin": admin})
		self.addCleanup(api._close)
		status = api.get_enrollment_status({"account_id": "account-1"})
		self.assertTrue(status["ok"])
		self.assertEqual(status["data"]["observed_name"], NAME)
		self.assertEqual(status["data"]["confirmation_token"], pending["token"])
		self.assertTrue(status["data"]["same_name_limit"])
		answer = api.confirm_enrollment({"account_id": "account-1", "confirmation_token": pending["token"], "confirmed": True})
		self.assertTrue(answer["ok"])
		self.assertNotIn(NAME, json.dumps(answer))
		rows = api.list_accounts({})["data"]["accounts"]
		self.assertEqual(rows[0]["identity_status"], "confirmed")
		self.assertIsNotNone(rows[0]["identity_confirmed_at"])

	def test_expired_pending_and_stale_token_never_bind(self):
		pending = identity.stage_enrollment("account-1", NAME, new=False, directory=self.home)
		value = identity.load(self.home)
		value["pending"]["account-1"]["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
		identity.save(value, self.home)
		with self.assertRaises(ValueError): admin.confirm_enrollment("account-1", pending["token"], True)
		self.assertIsNone(identity.binding("account-1", self.home))

	def test_env_failure_restores_binding_and_prefs_with_pending_retry(self):
		self.confirm()
		pending = identity.stage_enrollment("account-2", "Other Person", new=True, directory=self.home)
		before_binding = (self.home / "account-identities.json").read_bytes()
		before_env = (self.home / ".env").read_bytes()
		before_prefs = paths.prefs_path().read_bytes() if paths.prefs_path().exists() else None
		with patch.object(EnvFile, "save", side_effect=OSError(NAME)):
			with self.assertRaises(ValueError) as error: admin.confirm_enrollment("account-2", pending["token"], True)
		self.assertEqual(error.exception.code, "accounts.enrollment_save_failed")
		self.assertEqual((self.home / "account-identities.json").read_bytes(), before_binding)
		self.assertEqual((self.home / ".env").read_bytes(), before_env)
		self.assertEqual(paths.prefs_path().read_bytes() if paths.prefs_path().exists() else None, before_prefs)

	def test_final_binding_failure_restores_registration_and_allows_same_confirmation_retry(self):
		self.confirm()
		pending = identity.stage_enrollment("account-2", "Other Person", new=True, directory=self.home)
		before_env = paths.env_path().read_bytes()
		real_save = identity.save
		attempts = []
		def fail_final(value, directory=None):
			attempts.append(1)
			if len(attempts) == 2: raise OSError("synthetic final binding failure")
			return real_save(value, directory)
		with patch.object(identity, "save", side_effect=fail_final):
			with self.assertRaises(ValueError): admin.confirm_enrollment("account-2", pending["token"], True)
		self.assertEqual(paths.env_path().read_bytes(), before_env)
		self.assertIsNone(identity.binding("account-2", self.home))
		self.assertEqual(identity.pending("account-2", self.home)["token"], pending["token"])
		self.assertTrue(admin.confirm_enrollment("account-2", pending["token"], True)["managed"])

	def test_removed_or_migrated_identity_ids_never_reused(self):
		value = identity.load(self.home)
		value["used_ids"] = ["account-2", "account-3"]
		identity.save(value, self.home)
		prefs.save({"history_aliases": [{"from": "@root", "to": "account-4", "before": "2026-10-26T15:00:00Z"}]})
		self.assertEqual(admin._free_ids(), ["account-5"])

	def test_new_direct_enrollment_cannot_bypass_unconfirmed_existing_account(self):
		pending = identity.stage_enrollment("account-2", "Other Person", new=True, directory=self.home)
		with self.assertRaises(ValueError) as error: admin.confirm_enrollment("account-2", pending["token"], True)
		self.assertEqual(error.exception.code, "accounts.enrollment_required")
		self.assertEqual(settings.active_accounts(), ["account-1"])

	def test_invited_notes_never_enrolled_or_admitted_and_cancel_drops_pending(self):
		friend = prefs.save_invited_friend("Synthetic Friend", "friend@example.invalid")
		with self.assertRaises(ValueError): admin.begin_enrollment(friend["id"])
		with self.assertRaises(ValueError): admin.get_enrollment_status(friend["id"])
		self.start.assert_not_called()
		pending = identity.stage_enrollment("account-2", NAME, new=True, directory=self.home)
		signin._write_status("account-2", "awaiting_confirmation")
		admin.cancel_signin("account-2")
		self.assertIsNone(identity.pending("account-2", self.home))
		self.assertNotIn("account-2", settings.active_accounts())
		self.assertIn("account-2", identity.load(self.home)["used_ids"])

	def test_migration_alias_defaults_and_root_history_never_rewritten(self):
		self.assertEqual(prefs.load()["history_aliases"], [])
		(self.home / "runs").mkdir()
		history = self.home / "runs" / "20261026T100000Z-abcd.jsonl"
		history.write_text('{"type":"state","account":"default","balance":10}\n')
		before_history = history.read_bytes()
		editor = EnvFile.load(self.home / ".env")
		editor.set("REWARDS_ACCOUNTS", "")
		editor.save()
		(self.profiles / "Local State").write_bytes(b"synthetic-session")
		(self.profiles / "Default").mkdir()
		(self.profiles / "Default" / "Cookies").write_bytes(b"synthetic-signed-in-bytes")
		pending = identity.stage_enrollment("@root", NAME, new=False, directory=self.home)
		admin.confirm_enrollment("@root", pending["token"], True)
		with patch("run_lock.status", return_value=SimpleNamespace(owner="signin", pid=os.getpid())):
			target = admin.migrate_root_profile("account-3")
			first = prefs.load()["history_aliases"]
			self.assertEqual(admin.migrate_root_profile("account-3"), target)
		self.assertEqual(first, prefs.load()["history_aliases"])
		self.assertEqual(len(first), 1)
		self.assertEqual(set(first[0]), {"from", "to", "before"})
		self.assertEqual((first[0]["from"], first[0]["to"]), ("@root", target))
		self.assertTrue(first[0]["before"].endswith("Z"))
		self.assertEqual((self.profiles / target / "Default" / "Cookies").read_bytes(), b"synthetic-signed-in-bytes")
		self.assertEqual(history.read_bytes(), before_history)
		self.assertIsNotNone(identity.binding(target, self.home))
		self.assertIsNone(identity.binding("@root", self.home))

	def test_migration_prefs_or_binding_failure_rolls_back_all_documents(self):
		for failure in ("prefs", "binding"):
			with self.subTest(failure=failure):
				editor = EnvFile.load(self.home / ".env")
				editor.set("REWARDS_ACCOUNTS", "")
				editor.save()
				(self.profiles / "Local State").write_bytes(b"synthetic-session")
				before = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
				with patch("run_lock.status", return_value=SimpleNamespace(owner="signin", pid=os.getpid())), patch.object(prefs if failure == "prefs" else identity, "save", side_effect=OSError(NAME)):
					with self.assertRaises(ValueError): admin.migrate_root_profile("account-3")
				self.assertEqual({str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}, before)

	def test_interrupted_migration_resumes_and_adds_alias_once(self):
		editor = EnvFile.load(self.home / ".env")
		editor.set("REWARDS_ACCOUNTS", "")
		editor.save()
		(self.profiles / "Local State").write_bytes(b"synthetic-session")
		(self.profiles / "Default").mkdir()
		(self.profiles / "Default" / "Cookies").write_bytes(b"synthetic-cookies")
		rename = os.rename
		calls = []
		def interrupted(source, target):
			calls.append(str(source))
			if len(calls) == 2: raise KeyboardInterrupt()
			return rename(source, target)
		with patch("run_lock.status", return_value=SimpleNamespace(owner="signin", pid=os.getpid())):
			with patch.object(admin.os, "rename", side_effect=interrupted):
				with self.assertRaises(KeyboardInterrupt): admin.migrate_root_profile("account-3")
			self.assertTrue((self.home / "account-migration.json").exists())
			target = admin.migrate_root_profile("account-3")
			self.assertEqual(admin.migrate_root_profile("account-3"), target)
		self.assertEqual(len(prefs.load()["history_aliases"]), 1)
		self.assertFalse((self.home / "account-migration.json").exists())
		self.assertEqual((self.profiles / target / "Local State").read_bytes(), b"synthetic-session")
		self.assertEqual((self.profiles / target / "Default" / "Cookies").read_bytes(), b"synthetic-cookies")
