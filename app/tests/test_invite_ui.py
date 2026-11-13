import _bootstrap  # noqa: F401

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


class InviteUiTests(unittest.TestCase):
	def test_revision_pending_confirmation_and_resume_discard_dom_transitions(self):
		import hashlib
		catalogs = Path(__file__).resolve().parents[1] / "ui/i18n"
		source = json.loads((catalogs / "en/accounts.json").read_text(encoding="utf-8"))["messages"]
		for locale in ("de", "es", "fr", "it", "pt-BR", "ja", "zh-Hans", "ar"):
			messages = json.loads((catalogs / locale / "accounts.json").read_text(encoding="utf-8"))["messages"]
			for key in ("accounts.invite.recovery", "accounts.invite.forget_body"):
				self.assertEqual(messages[key]["src"], hashlib.sha256(json.dumps({"text": source[key]["text"]}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:8])
		fixture = json.loads((catalogs.parent / "mock/accounts/invite-onboarding.json").read_text(encoding="utf-8"))
		recovered = fixture["methods"]["list_invite_operations"]["data"]["operations"][0]
		self.assertTrue(recovered["recovery_only"]); self.assertFalse(recovered["can_resume"]); self.assertTrue(recovered["can_discard"])
		node = shutil.which("node")
		self.assertIsNotNone(node)
		ui = Path(__file__).resolve().parents[1] / "ui"
		result = subprocess.run([node, str(Path(__file__).with_name("invite_ui.mjs")), str(ui), "--recovery-only"], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=8,
			env={key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertTrue(all(json.loads(result.stdout).values()))

	def test_real_accounts_screen_and_signin_flow_in_browser_free_dom(self):
		node = shutil.which("node")
		self.assertIsNotNone(node)
		ui = Path(__file__).resolve().parents[1] / "ui"
		result = subprocess.run([node, str(Path(__file__).with_name("invite_ui.mjs")), str(ui)], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=20,
			env={key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertTrue(all(json.loads(result.stdout).values()))

	def test_nine_catalogs_have_all_invite_keys_source_hashes_and_identical_parameters(self):
		from tools.i18n_report import source_hash
		import re
		root = Path(__file__).resolve().parents[1] / "ui/i18n"
		locales = [item["code"] for item in json.loads((root / "index.json").read_text())["locales"]]
		self.assertEqual(len(locales), 9)
		for namespace, prefix in (("accounts", "accounts.invite."), ("errors", "errors.app.accounts_invite_")):
			source = json.loads((root / "en" / (namespace + ".json")).read_text(encoding="utf-8"))["messages"]
			keys = [key for key in source if key.startswith(prefix)]
			self.assertEqual(len(keys), 17 if namespace == "accounts" else 12)
			for locale in locales:
				catalog = json.loads((root / locale / (namespace + ".json")).read_text(encoding="utf-8"))["messages"]
				for key in keys:
					self.assertIn(key, catalog)
					self.assertEqual(set(re.findall(r"\{(\w+)\}", catalog[key]["text"])), set(re.findall(r"\{(\w+)\}", source[key]["text"])))
					if locale != "en": self.assertEqual(catalog[key]["src"], source_hash(source[key]))
					self.assertNotRegex(catalog[key]["text"], "[\u2013\u2014]")

	def test_fixture_and_consumers_keep_referral_success_unknown(self):
		ui = Path(__file__).resolve().parents[1] / "ui"
		fixture = json.loads((ui / "mock/accounts/invite-onboarding.json").read_text(encoding="utf-8"))
		self.assertTrue(fixture["synthetic"])
		for answer in fixture["methods"].values():
			packet = answer["data"].get("invitation", answer["data"].get("signin", {}).get("invitation"))
			if packet is not None: self.assertEqual(packet["referral_status"], "unknown")
		text = (ui / "js/screens/accounts/invitations.js").read_text(encoding="utf-8")
		self.assertNotIn("start_invite_account", text)
		self.assertIn("checked: false, disabled: true", text)

	def test_production_has_no_test_driver_or_mutation_entrypoint(self):
		root = Path(__file__).resolve().parents[2]
		for relative in ("app/launcher.py", "app/rfapp/signin.py", "app/rfapp/invite_onboarding.py", "app/rfapp/accounts_admin.py"):
			text = (root / relative).read_text(encoding="utf-8")
			for forbidden in ("invite_child", "synthetic-foundation", "RF_FAKE", "--fake", "test_invite_onboarding"):
				self.assertNotIn(forbidden, text, relative)

	def test_mock_fixture_invite_methods_match_current_envelopes_and_stay_unknown(self):
		node = shutil.which("node")
		self.assertIsNotNone(node)
		ui = Path(__file__).resolve().parents[1] / "ui"
		program = r"""
import fs from 'node:fs/promises';import {pathToFileURL,fileURLToPath} from 'node:url';
const root=process.argv[1];globalThis.window={location:{search:'?mock=accounts/invite-onboarding'},addEventListener(){},dispatchEvent(){}};
globalThis.fetch=async url=>({ok:true,json:async()=>JSON.parse(await fs.readFile(fileURLToPath(url),'utf8'))});
const module=await import(pathToFileURL(root+'/js/mock-bridge.js'));await module.install();const result={};
for(const name of ['preview_invite_account','start_invite_account','continue_invite_account','discard_invite_account','list_invite_operations'])result[name]=await window.pywebview.api[name]({});
console.log(JSON.stringify(result));
"""
		result = subprocess.run([node, "--input-type=module", "-e", program, str(ui)], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=20,
			env={key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")})
		self.assertEqual(result.returncode, 0, result.stderr)
		answers = json.loads(result.stdout)
		for method, answer in answers.items():
			self.assertTrue(answer["ok"], method)
			invitation = answer["data"].get("invitation", answer["data"].get("signin", {}).get("invitation"))
			if invitation: self.assertEqual(invitation["referral_status"], "unknown")
