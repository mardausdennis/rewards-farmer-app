import _bootstrap  # noqa: F401

import json
from pathlib import Path
from unittest.mock import Mock, patch

import identity
from rfapp import accounts_admin as admin, invite_onboarding as invite, paths, prefs, settings, signin
from test_invite_onboarding import InviteFixture


class HouseholdTests(InviteFixture):
	def configure(self, total, paused=0):
		ids = [f"account-{number}" for number in range(1, total+1)]
		active = ids[:total-paused] if paused else ids
		(self.home/".env").write_text("REWARDS_ACCOUNTS="+",".join(active)+"\nREWARDS_PAUSED_ACCOUNTS="+(",".join(ids[total-paused:]) if paused else "")+"\nUSER_DATA_DIR="+str(self.profiles)+"\n")
		value = identity.load(self.home)
		for account in ids:
			(self.profiles/account).mkdir(exist_ok=True)
			value["bindings"][account] = {"account_id":account,"expected_name_hash":identity.name_hash("Synthetic Person "+account),"confirmed_at":identity.utc()}
		identity.save(value,self.home)
		return ids

	def limit(self, call, count=6):
		with self.assertRaises(admin._Error) as failure: call()
		self.assertEqual(failure.exception.code,"accounts.household_limit")
		self.assertEqual(failure.exception.params,{"count":count,"limit":6})
		self.assertNotIn("Synthetic",str(failure.exception))

	def test_normal_add_five_needs_no_flag_six_requires_explicit_true(self):
		for count in (5,6):
			with self.subTest(count=count),patch.object(admin,"_ensure_idle"),patch.object(admin,"_start_signin",return_value={"state":"starting"}) as start:
				self.configure(count)
				if count==5:
					self.assertIn("account_id",admin.add_account())
				else:
					self.limit(lambda:admin.add_account())
					start.assert_not_called()
					self.assertIn("account_id",admin.add_account(household_ack=True))

	def test_preview_and_start_each_require_the_flag_not_a_sticky_preview_ack(self):
		self.configure(6,paused=2)
		with patch.object(admin,"_ensure_idle"),patch.object(admin,"_start_signin",return_value={"state":"starting"}) as start:
			denied=self.api.preview_invite_account({"source_account_id":"account-1"})
			self.assertEqual(denied["error"]["code"],"accounts.household_limit")
			self.assertEqual(denied["error"]["params"],{"count":6,"limit":6})
			self.assertEqual(denied["error"]["message_key"],"errors.app.accounts_household_limit")
			preview=self.good(self.api.preview_invite_account({"source_account_id":"account-1","household_ack":True}))["invitation"]
			request={key:preview[key] for key in ("operation_id","preview_token")}
			self.assertEqual(self.api.start_invite_account(request)["error"]["code"],"accounts.household_limit")
			start.assert_not_called()
			self.assertFalse((self.profiles/preview["target_account_id"]).exists())
			self.good(self.api.start_invite_account({**request,"household_ack":True}))
			start.assert_called_once()

	def test_five_accounts_can_preview_start_and_confirm_sixth_without_flag(self):
		self.configure(5)
		preview,owned,opened=self.start_owned()
		invite.continue_operation(preview["operation_id"],opened["continue_token"],lambda pid:True)
		invite.child_phase(preview["operation_id"],"verifying")
		invite.stage_confirmation(preview["operation_id"],"Synthetic New Household Person")
		pending=identity.pending(preview["target_account_id"],self.home)
		with patch.object(admin,"_ensure_idle"),patch("rfapp.schedule.ensure"):
			admin.confirm_enrollment(preview["target_account_id"],pending["token"],True)
		self.assertEqual(len(settings.active_accounts()),6)

	def test_pending_new_identity_is_a_position_and_confirmation_rechecks_capacity(self):
		self.configure(6)
		pending=identity.stage_enrollment("account-7","Synthetic Seventh",new=True,directory=self.home)
		with patch.object(admin,"_ensure_idle"),patch("rfapp.schedule.ensure"):
			self.limit(lambda:admin.confirm_enrollment("account-7",pending["token"],True))
			self.assertIsNone(identity.binding("account-7",self.home))
			admin.confirm_enrollment("account-7",pending["token"],True,household_ack=True)
		self.assertIsNotNone(identity.binding("account-7",self.home))

	def test_paused_pending_and_open_targets_are_unique_positions_but_notes_root_history_are_not(self):
		self.configure(5,paused=2)
		identity.stage_enrollment("account-8","Synthetic Pending",new=True,directory=self.home)
		value=identity.load(self.home);value["used_ids"].extend(["account-77","account-99"]);identity.save(value,self.home)
		prefs.save_invited_friend("Synthetic Friend","synthetic@example.invalid")
		self.limit(lambda:admin._household_guard(invite_targets=["account-8","@root"]),6)
		self.limit(lambda:admin._household_guard(invite_targets=["account-8","account-9","@root"]),7)
		# Capacity for the same reserved sixth target is five, never six twice.
		admin._household_guard(target="account-8",invite_targets=["account-8","@root"])
		value=identity.load(self.home);value["pending"].clear();identity.save(value,self.home)
		admin._household_guard(invite_targets=[])

	def test_starting_unconfigured_signin_reserves_position_without_lock_recursion(self):
		self.configure(5)
		signin._write_status("account-8","starting")
		self.limit(lambda:admin._household_guard(invite_targets=[]))
		# Expired process-less starts are not counted forever.
		status=json.loads(signin._status_path("account-8").read_text());status["at"]="2020-01-01T00:00:00Z"
		signin._status_path("account-8").write_text(json.dumps(status))
		admin._household_guard(invite_targets=[])
		status["at"]={"private":"synthetic"};signin._status_path("account-8").write_text(json.dumps(status))
		admin._household_guard(invite_targets=[])

	def test_real_open_invitation_reserves_the_sixth_position_without_notes(self):
		self.configure(5)
		preview=self.preview()
		self.assertFalse((self.profiles/preview["target_account_id"]).exists())
		with patch.object(admin,"_ensure_idle"),patch.object(admin,"_start_signin") as start:
			self.limit(lambda:admin.add_account())
			start.assert_not_called()
			# A rejected second preview must not consume a used id or a record.
			store=(self.home/"invite-onboarding.json").read_bytes()
			self.limit(lambda:admin.preview_invite_account("account-1"))
			self.assertEqual((self.home/"invite-onboarding.json").read_bytes(),store)
			self.assertIn("account_id",admin.add_account(household_ack=True))

	def test_false_ack_is_refused_and_true_reaches_bridge_add_without_persisting_flag(self):
		self.configure(6)
		with patch.object(admin,"_ensure_idle"),patch.object(admin,"_start_signin",return_value={"state":"starting"}):
			for request in ({},{"household_ack":False}):
				answer=self.api.add_account(request)
				self.assertEqual(answer["error"]["params"],{"count":6,"limit":6})
				self.assertEqual(answer["error"]["code"],"accounts.household_limit")
			self.good(self.api.add_account({"household_ack":True}))
		self.assertNotIn("household_ack",(self.home/"account-identities.json").read_text())
		self.assertNotIn("household_ack",(self.home/".env").read_text())

	def test_imported_paused_accounts_count_and_root_fallback_does_not(self):
		self.configure(6,paused=5)
		self.limit(lambda:admin._household_guard(invite_targets=[]))
		(self.home/".env").write_text("REWARDS_ACCOUNTS=\nREWARDS_PAUSED_ACCOUNTS=\nUSER_DATA_DIR="+str(self.profiles)+"\n")
		(self.profiles/"Local State").write_text("synthetic")
		self.assertEqual(settings.active_accounts(),["@root"])
		admin._household_guard(invite_targets=[])

	def test_existing_account_enrollment_does_not_consume_another_position(self):
		self.configure(7)
		with patch.object(admin,"_ensure_idle"),patch.object(admin,"_start_signin",return_value={"state":"starting"}) as start:
			admin.begin_enrollment("account-1")
			start.assert_called_once_with("account-1",False,enroll=True)

	def test_windows_case_aliases_do_not_count_one_local_id_twice(self):
		self.configure(5)
		admin._household_guard(invite_targets=["ACCOUNT-1"])
		self.limit(lambda:admin._household_guard(invite_targets=["account-8","ACCOUNT-8"]))
		admin._household_guard(target="ACCOUNT-8",invite_targets=["account-8","ACCOUNT-8"])

	def test_bridge_ack_is_strict_boolean_on_every_entry(self):
		self.configure(6)
		cases=(("add_account",{}),("begin_enrollment",{"account_id":"account-1"}),
			("preview_invite_account",{"source_account_id":"account-1"}),
			("start_invite_account",{"operation_id":"invite-"+"1"*32,"preview_token":"2"*32}),
			("confirm_enrollment",{"account_id":"account-8","confirmation_token":"3"*32,"confirmed":True}))
		for method,request in cases:
			for wrong in (1,"true",None,[]):
				with self.subTest(method=method,wrong=wrong),patch.object(admin,"_ensure_idle"):
					answer=getattr(self.api,method)({**request,"household_ack":wrong})
					self.assertEqual(answer["error"]["code"],"bad_request")


class InvitedByTests(InviteFixture):
	def admit(self):
		preview,owned,opened=self.start_owned()
		invite.continue_operation(preview["operation_id"],opened["continue_token"],lambda pid:True)
		invite.child_phase(preview["operation_id"],"verifying");invite.stage_confirmation(preview["operation_id"],"Synthetic New Person")
		pending=identity.pending(preview["target_account_id"],self.home)
		with patch.object(admin,"_ensure_idle"),patch("rfapp.schedule.ensure"):
			admin.confirm_enrollment(preview["target_account_id"],pending["token"],True)
		return preview["target_account_id"]

	def test_invited_admission_persists_only_source_id_and_removed_source_stays_visible(self):
		target=self.admit()
		bound=identity.binding(target,self.home)
		self.assertEqual(bound["invited_by"],"account-1")
		self.assertEqual(set(bound),{"account_id","expected_name_hash","confirmed_at","invited_by"})
		row=next(row for row in admin.list_accounts()["accounts"] if row["id"]==target)
		self.assertEqual(row["invited_by"],"account-1");self.assertFalse(row["invited_by_removed"])
		with patch.object(admin,"_ensure_idle"),patch("rfapp.schedule.ensure"):
			admin.remove_account("account-1",False)
		row=admin.list_accounts()["accounts"][0]
		self.assertEqual(row["invited_by"],"account-1");self.assertTrue(row["invited_by_removed"])
		pending=identity.stage_enrollment(target,"Synthetic New Person",new=False,directory=self.home)
		with patch.object(admin,"_ensure_idle"): admin.confirm_enrollment(target,pending["token"],True)
		self.assertEqual(identity.binding(target,self.home)["invited_by"],"account-1")

	def test_normal_admission_and_old_bindings_without_source_are_null(self):
		pending=identity.stage_enrollment("account-2","Synthetic Ordinary",new=True,directory=self.home)
		with patch.object(admin,"_ensure_idle"),patch("rfapp.schedule.ensure"):
			admin.confirm_enrollment("account-2",pending["token"],True)
		for row in admin.list_accounts()["accounts"]:
			self.assertIsNone(row["invited_by"]);self.assertFalse(row["invited_by_removed"])
		self.assertNotIn("invited_by",identity.binding("account-1",self.home))

	def test_failed_admission_never_leaves_inviter_attribution(self):
		preview,owned,opened=self.start_owned()
		invite.continue_operation(preview["operation_id"],opened["continue_token"],lambda pid:True)
		invite.child_phase(preview["operation_id"],"verifying");invite.stage_confirmation(preview["operation_id"],"Synthetic Rollback")
		target=preview["target_account_id"]
		pending=identity.pending(target,self.home)
		before=identity.load(self.home)
		with patch.object(admin,"_ensure_idle"),patch.object(invite,"finish_admission",side_effect=OSError("synthetic")),patch("rfapp.schedule.ensure") as schedule:
			with self.assertRaises(admin._Error) as failure:admin.confirm_enrollment(target,pending["token"],True)
			self.assertEqual(failure.exception.code,"accounts.enrollment_save_failed")
			schedule.assert_not_called()
		# Existing invitation recovery drops the unconfirmed observation after
		# restoring the documents. No identity or source may survive that path.
		before["pending"].pop(target)
		self.assertEqual(identity.load(self.home),before)
		self.assertIsNone(identity.binding(target,self.home))
		self.assertIsNone(identity.pending(target,self.home))

	def test_invalid_or_self_referral_id_is_not_exposed_from_a_binding(self):
		for source in ("https://private.invalid", "../bad", "account-1", {"name":"Synthetic"}):
			value=identity.load(self.home);value["bindings"]["account-1"]["invited_by"]=source;identity.save(value,self.home)
			with self.assertRaises(identity.IdentityError):identity.binding("account-1",self.home)
			row=admin.list_accounts()["accounts"][0]
			self.assertIsNone(row["invited_by"]);self.assertFalse(row["invited_by_removed"])
