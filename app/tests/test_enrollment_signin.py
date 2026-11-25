import _bootstrap  # noqa: F401

import json
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from selenium import webdriver
import identity
from rfapp import accounts_admin as real_admin, signin
import test_signin_flow as flows


class EnrollmentSigninTests(unittest.TestCase):
	setUp = flows.SigninFlowTests.setUp
	state = flows.SigninFlowTests.state

	def dashboard(self, text="Welcome, Synthetic New Person!", *, expected=None):
		self.visible.url = "https://rewards.bing.com/dashboard"
		self.visible.auth["identity"] = True
		self.visible.find_elements = Mock(return_value=[SimpleNamespace(text=text, is_displayed=lambda: True)])
		folder = self.profile / "account-2"
		folder.mkdir(exist_ok=True)
		(folder / "Local State").write_text(json.dumps({"profile":{"info_cache":{"Default":{"gaia_name":expected}}}}))
		self.admin._profile_identity = real_admin._profile_identity
		(self.root / ".env").write_text("REWARDS_ACCOUNTS=account-1\nUSER_DATA_DIR=" + str(self.profile) + "\n")
		value = identity.load(self.root)
		value["bindings"]["account-1"] = {"account_id":"account-1", "expected_name_hash":identity.name_hash("Synthetic Existing"), "confirmed_at":identity.utc()}
		identity.save(value,self.root)
		return folder

	def test_missing_profile_name_stages_dashboard_observation_only_for_confirmation(self):
		self.dashboard()
		self.assertEqual(signin.run("account-2",True,enroll=True),0)
		self.assertEqual(self.state("account-2")["state"],"awaiting_confirmation")
		pending = identity.pending("account-2",self.root)
		self.assertEqual(pending["observed_name"],"Welcome, Synthetic New Person!")
		self.assertIsNone(identity.binding("account-2",self.root))
		self.assertNotIn("account-2",(self.root / ".env").read_text())
		self.assertNotIn("Synthetic",json.dumps(self.state("account-2")))
		self.schedule.ensure.assert_not_called()
		self.lock.release.assert_called_once()
		with patch.object(real_admin,"_ensure_idle"):
			for token,yes in ((pending["token"],False),("wrong",True)):
				with self.assertRaises(ValueError): real_admin.confirm_enrollment("account-2",token,yes)
				self.assertIsNone(identity.binding("account-2",self.root))
			real_admin.confirm_enrollment("account-2",pending["token"],True)
		bound = identity.binding("account-2",self.root)
		self.assertEqual(set(bound),{"account_id","expected_name_hash","confirmed_at"})
		self.assertEqual(bound["expected_name_hash"],identity.name_hash(pending["observed_name"]))
		self.assertNotIn("Synthetic New Person",(self.root/"account-identities.json").read_text())

	def test_invitation_fallback_waits_for_explicit_continue_before_dashboard_read(self):
		self.dashboard()
		self.visible.window_handles = ["synthetic"]
		op = {"operation_id":"invite-"+"1"*32,"target_account_id":"account-2","phase":"invite_open"}
		invite = ModuleType("rfapp.invite_onboarding")
		invite.child_begin = Mock(return_value={"profile":self.profile/"account-2","link":"https://rewards.bing.com/refer?code=SYNTHETIC"})
		invite.child_phase = Mock(side_effect=lambda *args:dict(op))
		invite.interrupt = Mock()
		invite.stage_confirmation = Mock()
		self.visible.find_elements.return_value[0].text = "Welcome, Synthetic New Person!"
		first = [True]
		def read(*args):
			if first[0]:
				first[0] = False
				self.visible.find_elements.assert_not_called()
				self.assertEqual(self.visible.visits,["https://rewards.bing.com/refer?code=SYNTHETIC"])
				return dict(op)
			return {**op,"phase":"continue_requested"}
		invite.read = Mock(side_effect=read)
		import rfapp
		with patch.dict("sys.modules",{"rfapp.invite_onboarding":invite}),patch.object(rfapp,"invite_onboarding",invite),patch.object(webdriver,"EdgeService"):
			self.assertEqual(signin._run_invitation("account-2",op["operation_id"]),0)
		invite.stage_confirmation.assert_called_once_with(op["operation_id"],"Welcome, Synthetic New Person!")
		self.visible.find_elements.assert_called_once()
		self.lock.release.assert_called_once()

	def test_mismatched_profile_name_uses_dashboard_not_windows_identity(self):
		self.dashboard(expected="Synthetic Windows Name")
		self.assertEqual(signin.run("account-2",True,enroll=True),0)
		self.assertEqual(identity.pending("account-2",self.root)["observed_name"],"Welcome, Synthetic New Person!")
		self.assertNotIn("Synthetic Windows Name",json.dumps(identity.load(self.root)))

	def test_existing_identity_waits_for_account_switch_without_staging_it(self):
		self.dashboard("Welcome, Synthetic Existing!",expected="Synthetic Existing")
		original = signin._write_status
		statuses = []
		def status(*args,**kwargs):
			value = original(*args,**kwargs)
			statuses.append(value)
			return value
		def sleep(seconds):
			self.clock += seconds
			if self.clock == 2:
				self.assertEqual(self.state("account-2")["message_key"],"accounts.signin.already_added")
				self.assertIsNone(identity.pending("account-2",self.root))
				self.assertEqual(self.visible.quit_count,0)
				self.visible.find_elements.return_value[0].text = "Welcome, Synthetic New Person!"
		with patch.object(signin,"_write_status",side_effect=status),patch.object(signin.time,"sleep",side_effect=sleep):
			self.assertEqual(signin.run("account-2",True,enroll=True),0)
		self.assertTrue(any(row["error_code"] == "account_already_added" and row["state"] == "waiting" for row in statuses))
		self.assertEqual(identity.pending("account-2",self.root)["observed_name"],"Welcome, Synthetic New Person!")
		self.assertNotIn("Synthetic",json.dumps(statuses))

	def test_existing_identity_can_be_cancelled_and_recycles_only_new_profile(self):
		folder = self.dashboard("Welcome, Synthetic Existing!")
		def sleep(seconds):
			self.clock += seconds
			self.assertEqual(self.state("account-2")["error_code"],"account_already_added")
			signin._cancel_path("account-2").touch()
		with patch.object(signin.time,"sleep",side_effect=sleep):
			self.assertEqual(signin.run("account-2",True,enroll=True),1)
		self.assertEqual(self.state("account-2")["state"],"cancelled")
		self.assertIsNone(identity.pending("account-2",self.root))
		self.admin._recycle.assert_called_once_with([folder])
		self.lock.release.assert_called_once()

	def test_existing_identity_still_has_fifteen_minute_deadline(self):
		self.dashboard("Welcome, Synthetic Existing!")
		self.assertEqual(signin.run("account-2",True,enroll=True),1)
		self.assertEqual(self.clock,900)
		self.assertIsNone(identity.pending("account-2",self.root))
		self.admin._recycle.assert_called_once()
		self.lock.release.assert_called_once()

	def test_paused_existing_identity_is_also_not_a_new_account(self):
		self.dashboard("Welcome, Synthetic Paused!")
		value = identity.load(self.root)
		value["bindings"]["account-3"] = {"account_id":"account-3","expected_name_hash":identity.name_hash("Synthetic Paused"),"confirmed_at":identity.utc()}
		identity.save(value,self.root)
		with (self.root/".env").open("a") as stream: stream.write("REWARDS_PAUSED_ACCOUNTS=account-3\n")
		def sleep(seconds):
			self.clock += seconds
			self.assertEqual(self.state("account-2")["error_code"],"account_already_added")
			signin._cancel_path("account-2").touch()
		with patch.object(signin.time,"sleep",side_effect=sleep):
			self.assertEqual(signin.run("account-2",True,enroll=True),1)
		self.assertIsNone(identity.pending("account-2",self.root))

	def test_existing_binding_never_silently_changes_to_another_dashboard_name(self):
		self.dashboard()
		value = identity.load(self.root)
		value["bindings"]["account-2"] = {"account_id":"account-2","expected_name_hash":identity.name_hash("Synthetic Bound Name"),"confirmed_at":identity.utc()}
		identity.save(value,self.root)
		with (self.root/".env").open("a") as stream: stream.write("REWARDS_PAUSED_ACCOUNTS=account-2\n")
		before = (self.root/"account-identities.json").read_bytes()
		with self.assertLogs("rfapp",logging.ERROR):
			self.assertEqual(signin.run("account-2",False,enroll=True),1)
		self.assertEqual(self.state("account-2")["error_code"],"accounts.identity_mismatch")
		self.assertEqual((self.root/"account-identities.json").read_bytes(),before)
		self.admin._recycle.assert_not_called()
		self.lock.release.assert_called_once()

	def test_same_existing_binding_can_be_reconfirmed_without_profile_metadata(self):
		self.dashboard("Welcome, Synthetic Bound Name!")
		value = identity.load(self.root)
		value["bindings"]["account-2"] = {"account_id":"account-2","expected_name_hash":identity.name_hash("Synthetic Bound Name"),"confirmed_at":identity.utc()}
		identity.save(value,self.root)
		with (self.root/".env").open("a") as stream: stream.write("REWARDS_PAUSED_ACCOUNTS=account-2\n")
		self.assertEqual(signin.run("account-2",False,enroll=True),0)
		self.assertEqual(identity.pending("account-2",self.root)["observed_hash"],value["bindings"]["account-2"]["expected_name_hash"])

	def test_duplicate_on_last_observation_is_not_staged_after_save_grace(self):
		self.dashboard(expected="Synthetic New Person")
		def sleep(seconds):
			self.clock += seconds
			if seconds == 3:
				self.visible.find_elements.return_value[0].text = "Welcome, Synthetic Existing!"
			else:
				signin._cancel_path("account-2").touch()
		with patch.object(signin.time,"sleep",side_effect=sleep):
			self.assertEqual(signin.run("account-2",True,enroll=True),1)
		self.assertIsNone(identity.pending("account-2",self.root))

	def test_consent_on_recognised_dashboard_does_not_hide_signed_in_identity(self):
		self.dashboard()
		self.visible.structure["matched"] = {"consent":1}
		self.assertEqual(signin._detection(self.visible,self.rewards,None)[0],"signed_in")
		self.assertEqual(signin.run("account-2",True,enroll=True),0)
		self.assertEqual(self.state("account-2")["state"],"awaiting_confirmation")
		self.assertFalse(self.visible.structure["rendered"])

	def test_consent_alone_or_with_login_error_or_wrong_host_stays_unknown(self):
		self.dashboard()
		for url,auth,matched,expected in (
			("https://rewards.bing.com/dashboard",False,{"consent":1},"unknown"),
			("https://rewards.bing.com/dashboard",True,{"consent":1,"signin":1},"signed_out"),
			("https://rewards.bing.com/dashboard",True,{"consent":1,"error":1},"unknown"),
			("https://rewards.bing.com/earn",True,{"consent":1},"unknown"),
			("https://rewards.bing.com.evil.invalid/dashboard",True,{"consent":1},"signed_out")):
			with self.subTest(url=url,matched=matched):
				self.visible.url,self.visible.auth["identity"],self.visible.structure["matched"] = url,auth,matched
				self.assertEqual(signin._detection(self.visible,self.rewards,None)[0],expected)

	def test_failure_logs_type_and_frames_without_messages_locals_or_user_paths(self):
		self.dashboard()
		space = {}
		exec(compile("def synthetic_failure(*args,**kwargs):\n private_name='Synthetic Private Person'\n raise AttributeError(private_name+' https://private.invalid/?token=synthetic')\n",
			"C:/Users/Synthetic Operator/private_fault.py","exec"),space)
		self.edge_mock.side_effect = space["synthetic_failure"]
		with self.assertLogs("rfapp",logging.ERROR) as records:
			self.assertEqual(signin.run("account-2",True,enroll=True),1)
		text = "\n".join(records.output)
		self.assertIn('"exception": "AttributeError"',text)
		self.assertIn('"file": "private_fault.py"',text)
		self.assertIn('"function": "synthetic_failure"',text)
		self.assertIn('"line": 3',text)
		for private in ("Synthetic Private Person","Synthetic Operator","https://private.invalid","private_name",str(self.profile)):
			self.assertNotIn(private,text+json.dumps(self.state("account-2")))
		self.admin._recycle.assert_called_once()
		self.lock.release.assert_called_once()

	def test_recycle_failure_log_never_contains_exception_path_or_message(self):
		with patch("ctypes.windll.shell32.SHFileOperationW",side_effect=OSError("C:/Users/Synthetic Secret/Name")),self.assertLogs("rfapp",logging.ERROR) as records:
			self.assertFalse(real_admin._recycle([self.profile/"account-2"]))
		text = "\n".join(records.output)
		self.assertIn('"exception": "OSError"',text)
		self.assertNotIn("Synthetic Secret",text)
		self.assertNotIn(str(self.profile),text)

	def test_signin_spawn_failure_status_and_log_have_no_exception_message(self):
		self.dashboard()
		with patch.object(real_admin.subprocess,"Popen",side_effect=OSError("Synthetic Private /username/path")),self.assertLogs("rfapp",logging.ERROR) as records:
			value = real_admin._start_signin("account-2",True,enroll=True)
		self.assertEqual(value["detail"],"OSError")
		self.assertIn('"exception": "OSError"',"\n".join(records.output))
		self.assertNotIn("Synthetic Private",json.dumps(value)+"\n".join(records.output))

	def test_busy_enrollment_does_not_read_greeting_or_start_browser(self):
		self.dashboard()
		self.locks.acquire.return_value = None
		self.assertEqual(signin.run("account-2",True,enroll=True),3)
		self.edge_mock.assert_not_called()
		self.visible.find_elements.assert_not_called()
		self.assertIsNone(identity.pending("account-2",self.root))

	def test_ambiguous_or_hidden_greeting_cannot_stage_even_with_profile_name(self):
		self.dashboard(expected="Synthetic New Person")
		node = self.visible.find_elements.return_value[0]
		for nodes in ([node,node],[SimpleNamespace(text=node.text,is_displayed=lambda:False)],[]):
			self.edge_mock.side_effect = [self.visible,self.verification]
			self.visible.find_elements.return_value = nodes
			self.assertEqual(signin.run("account-2",True,enroll=True),1)
			self.assertIsNone(identity.pending("account-2",self.root))
			self.assertIsNone(identity.binding("account-2",self.root))

	def test_profile_creation_failure_recycles_after_lock_release_path(self):
		folder = self.dashboard()
		self.edge_mock.side_effect = AttributeError("Synthetic Private Name")
		with self.assertLogs("rfapp",logging.ERROR):
			self.assertEqual(signin.run("account-2",True,enroll=True),1)
		self.admin._recycle.assert_called_once_with([folder])
		self.lock.release.assert_called_once()
		self.assertIsNone(identity.binding("account-2",self.root))


class EnrollmentObservationTests(unittest.TestCase):
	def test_dashboard_fallback_keeps_normal_identity_api_hash_only(self):
		# Missing expected names still never turn the runtime check into discovery.
		driver = SimpleNamespace(get=Mock(),current_url="https://rewards.bing.com/dashboard",find_elements=Mock())
		self.assertEqual(identity.read_dashboard_identity(driver),("unknown",None))
		driver.find_elements.assert_not_called()

	def test_duplicate_is_rejected_again_at_the_identity_stage_boundary(self):
		import tempfile
		with tempfile.TemporaryDirectory() as folder:
			value = identity.load(folder)
			value["bindings"]["account-1"] = {"account_id":"account-1","expected_name_hash":identity.name_hash("Synthetic Existing"),"confirmed_at":identity.utc()}
			identity.save(value,folder)
			before = (Path(folder)/"account-identities.json").read_bytes()
			with self.assertRaises(identity.IdentityError) as caught:
				identity.stage_enrollment("account-2"," synthetic  EXISTING ",new=True,directory=folder)
			self.assertEqual(caught.exception.code,"accounts.account_already_added")
			self.assertEqual((Path(folder)/"account-identities.json").read_bytes(),before)

	def test_single_bounded_dashboard_field_never_guesses_localised_greeting(self):
		import tempfile
		with tempfile.TemporaryDirectory() as folder:
			for text in ("¡Hola, Persona Sintética!","こんにちは、合成の名前さん"," SYNTHETIC  PERSON "):
				node = SimpleNamespace(text=text,is_displayed=lambda:True)
				driver = SimpleNamespace(current_url="https://rewards.bing.com/dashboard",find_elements=Mock(return_value=[node]),get=Mock())
				with patch.object(logging.Logger,"handle") as logger:
					self.assertEqual(identity.observe_enrollment(driver,account_id="account-2",directory=folder),("observed"," ".join(text.split())))
					logger.assert_not_called()
				self.assertEqual(identity.read_dashboard_identity(driver,expected_name_hash=identity.name_hash(text))[0],"match")
			for text in ("", "x"*257, "x"*513):
				driver.find_elements.return_value[0].text = text
				self.assertEqual(identity.observe_enrollment(driver,account_id="account-2",directory=folder),("unknown",None))
			driver.current_url = "https://rewards.bing.com.evil.invalid/dashboard"
			driver.find_elements.reset_mock()
			self.assertEqual(identity.observe_enrollment(driver,account_id="account-2",directory=folder),("unknown",None))
			driver.find_elements.assert_not_called()

	def test_confirmation_rechecks_a_duplicate_added_after_the_observation(self):
		from test_enrollment import EnrollmentTests
		fixture = EnrollmentTests()
		fixture.setUp()
		try:
			pending = identity.stage_enrollment("account-2","Synthetic Later Duplicate",new=True,directory=fixture.home)
			value = identity.load(fixture.home)
			value["bindings"]["account-1"] = {"account_id":"account-1","expected_name_hash":identity.name_hash("Synthetic Later Duplicate"),"confirmed_at":identity.utc()}
			identity.save(value,fixture.home)
			before = (fixture.home/"account-identities.json").read_bytes()
			with self.assertRaises(ValueError) as caught:
				real_admin.confirm_enrollment("account-2",pending["token"],True)
			self.assertEqual(caught.exception.code,"accounts.enrollment_required")
			self.assertEqual(json.loads(signin._status_path("account-2").read_text())["message_key"],"accounts.signin.already_added")
			self.assertEqual((fixture.home/"account-identities.json").read_bytes(),before)
			fixture.ensure.assert_not_called()
		finally: fixture.doCleanups()

	def test_already_added_wait_message_exists_in_every_catalog(self):
		from tools.i18n_report import source_hash
		root = Path(__file__).resolve().parents[1]/"ui/i18n"
		key = "accounts.signin.already_added"
		source = json.loads((root/"en/accounts.json").read_text(encoding="utf-8"))["messages"][key]
		for locale in json.loads((root/"index.json").read_text())["locales"]:
			entry = json.loads((root/locale["code"]/"accounts.json").read_text(encoding="utf-8"))["messages"][key]
			self.assertTrue(entry["text"].strip())
			if locale["code"] != "en": self.assertEqual(entry["src"],source_hash(source))
