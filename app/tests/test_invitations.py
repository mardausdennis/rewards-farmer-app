import _bootstrap  # noqa: F401

import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rfapp import accounts_admin, paths, prefs, settings
from rfapp.bridge import Bridge
import referrals


class InvitationTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		guard = patch.dict(os.environ, {"REWARDS_FARMER_DATA_HOME": str(self.home)})
		guard.start()
		self.addCleanup(guard.stop)
		self.env = self.home / ".env"
		self.env.write_text("REWARDS_ACCOUNTS=account-1\nREWARDS_PAUSED_ACCOUNTS=account-2\n", encoding="utf-8")
		self.schedule = Mock()
		self.admin = SimpleNamespace(list_accounts=Mock(return_value={"accounts": [{"id": "account-1", "paused": False}]}))
		self.api = Bridge(dependencies={"prefs": prefs, "accounts_admin": self.admin, "schedule": self.schedule})
		self.addCleanup(self.api._close)

	def save(self, **changes):
		return self.api.save_invited_friend({"name": "Synthetic Friend", "email": "friend@example.invalid", **changes})

	def test_crud_exact_note_shape_local_ids_and_timestamp_preserved(self):
		self.assertEqual(self.api.get_invited_friends({}), {"ok": True, "data": {"invited_friends": []}})
		self.assertFalse(paths.prefs_path().exists())
		answer = self.save()
		self.assertTrue(answer["ok"])
		friend = answer["data"]["friend"]
		self.assertEqual(set(friend), {"id", "name", "email", "added_at"})
		self.assertRegex(friend["id"], r"^friend-[0-9a-f]{32}$")
		self.assertTrue(friend["added_at"].endswith("Z"))
		updated = self.save(id=friend["id"], name="Edited", email="edited@example.invalid")["data"]["friend"]
		self.assertEqual(updated["added_at"], friend["added_at"])
		self.assertEqual(updated["name"], "Edited")
		self.assertEqual(self.api.get_invited_friends({})["data"], {"invited_friends": [updated]})
		self.assertEqual(self.api.remove_invited_friend({"id": friend["id"]}), {"ok": True, "data": {"invited_friends": []}})

	def test_crud_never_changes_env_profiles_registration_or_schedule(self):
		before = self.env.read_bytes()
		prefs.save({"account_labels": {"account-1": "Main"}, "goals": {"account-1": 1000}})
		friend = self.save()["data"]["friend"]
		self.save(id=friend["id"], name="Other")
		self.api.get_invited_friends({})
		self.api.remove_invited_friend({"id": friend["id"]})
		self.assertEqual(self.env.read_bytes(), before)
		self.assertEqual(prefs.load()["account_labels"], {"account-1": "Main"})
		self.assertEqual(prefs.load()["goals"], {"account-1": 1000})
		self.assertFalse((self.home / "data-dir").exists())
		self.admin.list_accounts.assert_not_called()
		self.assertEqual(self.schedule.mock_calls, [])

	def test_names_emails_unknown_ids_and_runtime_fields_rejected_without_writes(self):
		for changes in ({"name": ""}, {"name": "x" * 41}, {"name": "bad\nname"}, {"name": None}, {"email": "not-an-email"}, {"email": "a\nb@example.invalid"}, {"email": 4}, {"id": "account-1"}, {"id": None}, {"id": "friend-" + "0" * 32}, {"profile": "Default"}, {"password": "synthetic"}, {"signed_in": True}, {"auto_farm": True}):
			with self.subTest(fields=list(changes)):
				answer = self.save(**changes)
				self.assertFalse(answer["ok"])
				self.assertEqual(answer["error"]["code"], "bad_request")
		self.assertFalse(paths.prefs_path().exists())
		self.assertEqual(self.api.remove_invited_friend({"id": "account-1"})["error"]["code"], "bad_request")

	def test_storage_projection_does_not_expose_unknown_runtime_fields(self):
		friend = self.save()["data"]["friend"]
		prefs.save({"invited_friends": [{**friend, "profile": "private", "password": "secret", "auto_farm": True}, friend, None, {"id": "account-1"}]})
		self.assertEqual(prefs.invited_friends(), [friend])
		for invalid in (None, {}, "wrong"):
			prefs.save({"invited_friends": invalid})
			self.assertEqual(prefs.invited_friends(), [])

	def test_concurrent_friend_updates_preserve_each_entry(self):
		threads = [threading.Thread(target=prefs.save_invited_friend, args=(f"Friend {index}", f"friend{index}@example.invalid")) for index in range(8)]
		for thread in threads: thread.start()
		for thread in threads: thread.join(5)
		self.assertTrue(all(not thread.is_alive() for thread in threads))
		self.assertEqual(len(prefs.invited_friends()), 8)
		self.assertEqual(len({item["id"] for item in prefs.invited_friends()}), 8)

	def test_friend_save_failure_returns_class_only_and_preserves_store(self):
		friend = self.save()["data"]["friend"]
		before = paths.prefs_path().read_bytes()
		with patch.object(prefs, "replace_file", side_effect=OSError("private@example.invalid")):
			answer = self.save(id=friend["id"], name="Edited")
		self.assertFalse(answer["ok"])
		self.assertNotIn("private@example.invalid", json.dumps(answer))
		self.assertEqual(answer["error"]["detail"], "OSError")
		self.assertEqual(paths.prefs_path().read_bytes(), before)

	def test_referral_bridge_account_binding_default_explicit_and_unknown(self):
		backend = SimpleNamespace(get_referral_info=Mock(return_value={"link": None, "link_found": False, "referrals": None, "friends": None, "read_at": None, "unreadable": "not_read"}))
		self.api._dependencies["referrals"] = backend
		self.admin.list_accounts.return_value = {"accounts": [{"id": "account-2", "paused": True}, {"id": "account-1", "paused": False}]}
		self.assertTrue(self.api.get_referral_info({})["ok"])
		backend.get_referral_info.assert_called_with("account-1", directory=self.home)
		self.assertTrue(self.api.get_referral_info({"account_id": "account-2"})["ok"])
		backend.get_referral_info.assert_called_with("account-2", directory=self.home)
		friend = self.save()["data"]["friend"]
		self.assertEqual(self.api.get_referral_info({"account_id": friend["id"]})["error"]["code"], "accounts.unknown")
		self.admin.list_accounts.return_value = {"accounts": []}
		self.assertTrue(self.api.get_referral_info({})["ok"])
		backend.get_referral_info.assert_called_with(None, directory=self.home)

	def test_friends_are_not_real_accounts_and_cannot_start_run(self):
		friend = self.save()["data"]["friend"]
		self.api._dependencies["run_lock"] = SimpleNamespace(status=lambda **kwargs: None)
		self.api._dependencies["settings"] = SimpleNamespace(user_data_dir=lambda: self.home / "profiles")
		with patch.object(self.api, "_spawn") as spawn:
			self.assertEqual(self.api.start_run({"account_ids": [friend["id"]]})["error"]["code"], "run.no_accounts")
		spawn.assert_not_called()
		self.assertEqual(self.api._accounts(), [{"id": "account-1", "paused": False}])
		with patch.object(settings, "paths", paths):
			self.assertEqual(settings.active_accounts(), ["account-1"])
		with patch.object(settings, "user_data_dir", return_value=self.home / "profiles"), patch.object(accounts_admin, "_profile_identity", return_value={"email": None, "name": None}), patch.object(accounts_admin, "_picture_source", return_value=None):
			self.assertEqual([item["id"] for item in accounts_admin.list_accounts()["accounts"]], ["account-1", "account-2"])
