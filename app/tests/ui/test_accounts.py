import _bootstrap  # noqa: F401

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ui.harness import SCREENSHOT_DIR, UI_DIR, UiHarness
from rfapp import signin


_enabled = os.environ.get("REWARDS_APP_UI_TESTS") == "1"
_harness = None
FIXTURES = sorted(path.stem for path in (UI_DIR / "mock/accounts").glob("*.json"))


def setUpModule():
	global _harness
	if _enabled:
		_harness = UiHarness()
		try:
			_harness.start().wait_ready()
		except BaseException:
			_harness.stop()
			raise


def tearDownModule():
	if _harness is not None:
		_harness.stop()


class SourceTests(unittest.TestCase):
	def test_catalog_synthetic_fixtures_and_shared_input(self):
		catalog = json.loads((UI_DIR / "i18n/en/accounts.json").read_text(encoding="utf-8"))
		self.assertEqual("accounts", catalog["namespace"])
		for key, entry in catalog["messages"].items():
			self.assertTrue(entry["note"].strip(), key)
			self.assertNotRegex(json.dumps(entry, ensure_ascii=False), "[\u2013\u2014]")
		self.assertTrue({"multi-account", "unconfirmed", "enrollment"}.issubset(FIXTURES))
		self.assertTrue({"invitations", "invitations-unknown"}.issubset(FIXTURES))
		for fixture in FIXTURES:
			data = json.loads((UI_DIR / "mock/accounts" / (fixture + ".json")).read_text(encoding="utf-8"))
			self.assertTrue(data["synthetic"])
			self.assertIsInstance(data["methods"], dict)
		text = (UI_DIR / "js/screens/accounts.js").read_text(encoding="utf-8")
		self.assertIn("c.textField.create", text)
		self.assertIn("maxLength: 40", text)
		self.assertIn('flow.start("add_account"', text)
		self.assertNotIn('el("input"', text)
		self.assertNotIn(".style", text)
		self.assertNotRegex(text, "[\u2013\u2014]")
		css = (UI_DIR / "css/screens/accounts.css").read_text(encoding="utf-8")
		self.assertNotRegex(css, r"(?m)(?:^|[;{])\s*(?:width|height|left|right|top|bottom|margin-left|padding-right)\s*:")

	def test_f104_invitation_catalog_and_screen_boundary(self):
		catalog=json.loads((UI_DIR/"i18n/en/accounts.json").read_text(encoding="utf-8"))["messages"]
		codes=("not_read","signed_out","challenge","page_unknown","link_missing","link_ambiguous","link_invalid","read_failed","storage_failed","storage_unreadable")
		for code in codes:self.assertIn("accounts.referrals.unreadable."+code,catalog)
		text=(UI_DIR/"js/screens/accounts/invitations.js").read_text(encoding="utf-8")
		self.assertIn("c.accountRow.create",text);self.assertIn("c.switchControl.create",text);self.assertIn("c.textField.create",text)
		self.assertNotIn('el("input"',text);self.assertNotIn("store.set",text)
		for method in ("add_account","sign_in_again","set_account_paused","remove_account","start_run","stop_run","get_account_picture"):
			self.assertNotIn('bridge.call("'+method+'"',text)
		self.assertNotRegex(text,"[\u2013\u2014]")
		self.assertIn("navigator.clipboard.writeText",text)


@unittest.skipUnless(_enabled, "Set REWARDS_APP_UI_TESTS=1 for isolated Accounts UI tests")
class AccountsTests(unittest.TestCase):
	def test_f108_rev1_enrollment_has_one_confirmation_surface(self):
		from selenium.webdriver.common.keys import Keys
		self.open('multi-account', 'light')
		self.js("""
		 const calls=[],api=window.pywebview.api;
		 for(const method of ['cancel_signin','confirm_enrollment']){const original=api[method];api[method]=async request=>{calls.push({method,request:structuredClone(request)});return original(request)}}
		 window.rev1EnrollmentCalls=calls;
		 await addWithoutInvitation();await wait(()=>document.getElementById('dialog').open);
		 assert(document.getElementById('dialog').textContent.includes('Morgan Synthetic'),'rev1 observed name retained');
		 assert(!document.querySelector('.accounts-signin')&&!document.getElementById('accounts-confirm-enrollment')&&!document.getElementById('accounts-cancel-enrollment'),'rev1 no duplicate banner or actions behind modal');
		 assert(s.get('accounts').length===2,'rev1 observation does not register');
		""")
		_harness.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
		self.js("""
		 await wait(()=>!document.getElementById('dialog').open&&document.getElementById('accounts-confirm-enrollment'));
		 assert(document.querySelector('[data-signin-state=awaiting_confirmation]'),'rev1 Escape keeps pending confirmation');
		 assert(window.rev1EnrollmentCalls.length===0&&s.get('accounts').length===2,'rev1 Escape neither cancels nor registers');
		 assert(document.activeElement.id==='accounts-confirm-enrollment'&&document.getElementById('accounts-cancel-enrollment'),'rev1 pending actions recover keyboard focus');
		 document.getElementById('accounts-confirm-enrollment').click();await wait(()=>document.getElementById('dialog').open);
		 assert(!document.querySelector('.accounts-signin'),'rev1 reopened dialog replaces banner');
		 const dialog=document.getElementById('dialog'),closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));dialog.querySelector('.button-ghost').click();await closed;
		 await wait(()=>window.rev1EnrollmentCalls.some(call=>call.method==='cancel_signin')&&!document.getElementById('accounts-add-another').disabled);
		 assert(s.get('accounts').length===2&&!document.getElementById('accounts-confirm-enrollment')&&!document.getElementById('accounts-cancel-enrollment'),'rev1 explicit Cancel discards pending registration');
		 await addWithoutInvitation();await wait(()=>document.getElementById('dialog').open);
		 assert(!document.querySelector('.accounts-signin'),'rev1 no duplicate surface on next enrollment');
		 document.querySelector('#dialog .button-primary').click();await wait(()=>s.get('accounts').length===3&&!document.getElementById('accounts-add-another').disabled);
		 assert(window.rev1EnrollmentCalls.filter(call=>call.method==='confirm_enrollment').length===1&&s.get('accounts')[2].identity_status==='confirmed','rev1 exactly one explicit confirmation registers');
		""")
		self.assertEqual([], _harness.errors())

	def test_f108_confirm_cancel_and_existing_first(self):
		self.open("unconfirmed")
		self.js("""
		 const api=window.pywebview.api,calls=[];
		 for(const method of ['add_account','begin_enrollment','confirm_enrollment','cancel_signin']){const original=api[method];api[method]=async request=>{calls.push({method,request:structuredClone(request)});return original(request)}}
		 await addWithoutInvitation();await wait(()=>document.getElementById('dialog').open);
		 assert(calls[0].method==='begin_enrollment'&&calls[0].request.account_id==='account-1','F108 existing first');
		 assert(!calls.some(call=>call.method==='add_account'),'F108 no add before enrollment');
		 const dialog=document.getElementById('dialog');assert(dialog.textContent.includes('Jamie Synthetic')&&dialog.textContent.includes(i.t('accounts.enrollment.same_name_limit')),'F108 observed name and same-name limitation');
		 dialog.querySelector('.button-primary').click();await wait(()=>s.get('accounts')[0].identity_status==='confirmed'&&!document.getElementById('accounts-add-another').disabled);
		 const confirmed=calls.find(call=>call.method==='confirm_enrollment').request;
		 assert(confirmed.account_id==='account-1'&&confirmed.confirmed===true&&confirmed.confirmation_token,'F108 token bound');
		 await addWithoutInvitation();await wait(()=>dialog.open);
		 assert(dialog.textContent.includes('Morgan Synthetic'),'F108 new observed name');
		 dialog.querySelector('.button-ghost').click();await wait(()=>calls.some(call=>call.method==='cancel_signin')&&!document.getElementById('accounts-add-another').disabled);
		 assert(s.get('accounts').length===1&&calls.filter(call=>call.method==='confirm_enrollment').length===1,'F108 cancel never registers');
		 await addWithoutInvitation();await wait(()=>dialog.open);dialog.querySelector('.button-primary').click();
		 await wait(()=>s.get('accounts').length===2&&!document.getElementById('accounts-add-another').disabled);
		 assert(s.get('accounts')[1].identity_status==='confirmed','F108 second confirmed');
		 const router=await import('./js/router.js');router.navigate('#/overview');await wait(()=>document.getElementById('overview-account')?.options.length===3);
		 assert(document.getElementById('overview-account').value==='all'&&document.querySelectorAll('[data-overview-account]').length===2,'F108 newly registered account appears without restarting app');
		 """)
		self.assertEqual([], _harness.errors())

	def test_f108_migration_identity_and_failure(self):
		self.open("multi-account")
		self.js("""
		 assert(document.querySelectorAll('[data-identity=confirmed]').length===2,'F108 identity states');
		 assert(document.querySelector('[data-identity-check=match] time')&&document.querySelector('[data-identity-check=mismatch] time'),'F108 check times');
		 assert(document.querySelector('[data-invited]')&&!document.querySelector('[data-invited] [data-identity]'),'F108 friend note not enrolled');
		 const api=window.pywebview.api,original=api.add_account,calls=[];
		 api.add_account=async request=>{calls.push(request);return request.confirm_migration?original(request):{ok:false,error:{code:'accounts.migration_needed',message_key:'errors.app.accounts_migration_needed',params:{},detail:null,fields:null}}};
		 await addWithoutInvitation();await wait(()=>document.getElementById('dialog').open);
		 assert(document.getElementById('dialog').textContent.includes(i.t('errors.app.accounts_migration_needed')),'F108 migration preview');
		 document.querySelector('#dialog .button-primary').click();await wait(()=>calls.length===2&&document.getElementById('dialog').open&&document.getElementById('dialog').textContent.includes('Morgan Synthetic'));
		 assert(!calls[0].confirm_migration&&calls[1].confirm_migration===true,'F108 migration only after choice');
		 api.confirm_enrollment=async()=>({ok:false,error:{code:'accounts.enrollment_expired',message_key:'errors.app.accounts_enrollment_expired',params:{},detail:null,fields:null}});
		 document.querySelector('#dialog .button-primary').click();await wait(()=>document.querySelector('.accounts-screen>.banner'));
		 assert(s.get('accounts').length===2&&document.querySelector('.accounts-screen>.banner').textContent.includes(i.t('errors.app.accounts_enrollment_expired')),'F108 expired token never registers');
		 document.getElementById('accounts-confirm-enrollment').click();await wait(()=>document.getElementById('dialog').open);document.querySelector('#dialog .button-ghost').click();await wait(()=>!document.getElementById('dialog').open&&!document.getElementById('accounts-add-another').disabled);
		 """)
		self.open("multi-account")
		self.js("""
		 const api=window.pywebview.api,accounts=structuredClone(s.get('accounts'));accounts[0].identity_status='unknown';accounts[0].identity_last_status='unknown';accounts[1].identity_status='unconfirmed';
		 api.list_accounts=async()=>({ok:true,data:{accounts}});
		 const r=await import('./js/router.js');r.navigate('#/overview');await wait(()=>document.querySelector('.overview-screen'));r.navigate('#/accounts');await wait(()=>document.querySelector('[data-identity=unknown]'));
		 assert(document.querySelector('[data-identity-check=unknown]')&&document.querySelector('[data-identity=unconfirmed]'),'F108 unknown not match and unconfirmed distinct');
		 api.get_enrollment_status=async()=>({ok:true,data:{account_id:'foreign',state:'awaiting_confirmation',observed_name:'Wrong name',confirmation_token:'wrong',same_name_limit:true}});
		 await addWithoutInvitation();await wait(()=>document.querySelector('.accounts-screen>.banner'));
		 assert(!document.getElementById('dialog').open&&s.get('accounts').length===2,'F108 foreign observation not offered');
		 """)

	def test_f108_accounts_captures(self):
		for code in ('enrollment_save_failed', 'enrollment_pending', 'identity_mismatch', 'migration_pending'):
			with self.subTest(code=code):
				self.open('multi-account')
				self.js('const code='+json.dumps(code)+';'+"""
				 const api=window.pywebview.api;
				 if(code==='enrollment_save_failed')api.confirm_enrollment=async()=>({ok:false,error:{code:'accounts.'+code,message_key:'errors.app.accounts_'+code,params:{},detail:null,fields:null}});
				 else api.add_account=async()=>({ok:false,error:{code:'accounts.'+code,message_key:'errors.app.accounts_'+code,params:{},detail:null,fields:null}});
				 await addWithoutInvitation();
				 if(code==='enrollment_save_failed'){await wait(()=>document.getElementById('dialog').open);const d=document.getElementById('dialog'),closed=new Promise(resolve=>d.addEventListener('close',resolve,{once:true}));d.querySelector('.button-primary').click();await closed}
				 await wait(()=>document.querySelector('.accounts-screen>.banner'));
				 assert(document.querySelector('.accounts-screen>.banner').textContent.includes(i.t('errors.app.accounts_'+code))&&s.get('accounts').length===2,'F108 failure stays causal without registering');
				""")
		for theme in ("light","dark"):
			for width,height in ((1280,800),(860,600)):
				_harness.viewport(width,height);self.open("multi-account",theme)
				self.js("assert(document.querySelectorAll('[data-identity]').length===2&&document.querySelectorAll('[data-invited]').length===1,'F108 two real accounts one note');assert(document.documentElement.scrollWidth<=innerWidth+1,'F108 accounts fits')")
				if os.environ.get('RF_F108_CAPTURE'):_harness.driver.save_screenshot(str(Path(os.environ['RF_F108_CAPTURE'])/f'accounts-{theme}-{width}x{height}.png'))
				self.js("await addWithoutInvitation();await wait(()=>document.getElementById('dialog').open);assert(document.getElementById('dialog').textContent.includes('Morgan Synthetic'),'F108 observed name shown');assert(!document.querySelector('.accounts-signin'),'rev1 enrollment capture has only one confirmation surface')")
				if os.environ.get('RF_F108_CAPTURE'):_harness.driver.save_screenshot(str(Path(os.environ['RF_F108_CAPTURE'])/f'enrollment-{theme}-{width}x{height}.png'))
				self.js("document.querySelector('#dialog .button-ghost').click();await wait(()=>!document.getElementById('dialog').open)")
		_harness.viewport(1280,800)

	def test_r4_l10_secondary_action_style_and_keyboard(self):
		from selenium.webdriver.common.keys import Keys
		for lang in ("en", "de", "ar"):
			_harness.viewport(1131, 643)
			self.open("list", lang=lang)
			self.js("document.getElementById('accounts-more-account-1').open=true;await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)))")
			if os.environ.get("RF_F76_R4_MUTATION") == "L10":
				_harness.driver.execute_script("document.getElementById('accounts-remove-account-1').classList.remove('button-ghost')")
			self.js("""
			 const buttons=['rename','pause','remove'].map(action=>document.getElementById('accounts-'+action+'-account-1'));
			 const keys=['backgroundColor','borderTopColor','borderTopWidth','borderTopStyle','borderRadius','paddingBlockStart','paddingInlineStart','fontSize','fontWeight','lineHeight','boxShadow'];
			 const signature=node=>keys.map(key=>getComputedStyle(node)[key]),first=JSON.stringify(signature(buttons[0]));
			 assert(buttons.every(node=>node.classList.contains('button-ghost')&&JSON.stringify(signature(node))===first),'R4 L10 all secondary actions share ghost style');
			 assert(getComputedStyle(buttons[2]).color===getComputedStyle(document.documentElement).getPropertyValue('--c-danger').trim()||buttons[2].classList.contains('button-danger'),'R4 L10 Remove retains danger tone');
			 assert(getComputedStyle(buttons[0]).color===getComputedStyle(buttons[1]).color&&getComputedStyle(buttons[2]).color!==getComputedStyle(buttons[0]).color,'R4 L10 color is the destructive difference');
			 document.getElementById('accounts-more-toggle-account-1').focus();
			 """)
			for action in ("rename", "pause", "remove"):
				_harness.driver.switch_to.active_element.send_keys(Keys.TAB)
				self.assertEqual("accounts-" + action + "-account-1", _harness.driver.switch_to.active_element.get_attribute("id"))
			_harness.driver.switch_to.active_element.send_keys(Keys.ENTER)
			self.js("await wait(()=>document.getElementById('dialog').open);assert(document.querySelectorAll('#dialog .button-danger').length===2,'R4 L10 both original removal confirmation choices retained')")
			_harness.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
			self.js("await wait(()=>!document.getElementById('dialog').open);await new Promise(done=>requestAnimationFrame(done))")
			self.assertEqual("accounts-remove-account-1", _harness.driver.switch_to.active_element.get_attribute("id"), "R4 L10 cancellation restores Remove focus")
			self.assertEqual([], _harness.errors())

	def test_r3_l2_disclosure_geometry_and_native_keyboard(self):
		from selenium.webdriver.common.keys import Keys
		mutation = os.environ.get("RF_F76_R3_MUTATION") == "L2"
		for lang in ("en", "de"):
			for width, height in ((1131, 643), (860, 600), (1280, 800)):
				_harness.viewport(width, height)
				self.open("list", lang=lang)
				if mutation:
					_harness.driver.execute_cdp_cmd("DOM.enable", {})
					_harness.driver.execute_cdp_cmd("CSS.enable", {})
					frame = _harness.driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]["frame"]["id"]
					sheet = _harness.driver.execute_cdp_cmd("CSS.createStyleSheet", {"frameId": frame})["styleSheetId"]
					_harness.driver.execute_cdp_cmd("CSS.setStyleSheetText", {"styleSheetId": sheet, "text": ".accounts-entry .accounts-disclosure>summary{padding-block:24px!important}.accounts-screen .account-row .control-actions{align-items:center!important}"})
				self.js("""
				 const entry=document.querySelector('.accounts-entry'),details=entry.querySelector('details'),summary=details.querySelector('summary'),signin=entry.querySelector('[id^=accounts-signin-]');
				 const box=n=>{const r=n.getBoundingClientRect();return{x:r.x,y:r.y,w:r.width,h:r.height,b:r.bottom}};
				 const first={summary:box(summary),signin:box(signin)};
				 assert(Math.abs(first.summary.h-first.signin.h)<1&&Math.abs(first.summary.y-first.signin.y)<1,'R3 L2 controls same height and top');
				 details.open=true;await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
				 const opened={summary:box(summary),signin:box(signin)};
				 assert(Math.abs(opened.summary.w-first.summary.w)<1&&Math.abs(opened.signin.y-first.signin.y)<1&&Math.abs(opened.signin.x-first.signin.x)<1,'R3 L2 opening preserves width and Sign in position');
				 assert([...details.querySelectorAll('button')].every(n=>box(n).y>=Math.max(opened.summary.b,opened.signin.b)),'R3 L2 actions below control row');
				 details.open=false;summary.focus();
				 """)
				_harness.driver.switch_to.active_element.send_keys(Keys.ENTER)
				self.assertTrue(_harness.driver.execute_script("return document.querySelector('.accounts-entry details').open"))
				_harness.driver.switch_to.active_element.send_keys(Keys.TAB)
				self.assertEqual("accounts-rename-account-1", _harness.driver.execute_script("return document.activeElement.id"))
				_harness.driver.execute_script("document.querySelector('.accounts-entry summary').focus()")
				_harness.driver.switch_to.active_element.send_keys(Keys.SPACE)
				self.assertFalse(_harness.driver.execute_script("return document.querySelector('.accounts-entry details').open"))
				self.assertEqual([], _harness.errors())

	def js(self, body):
		result = _harness.driver.execute_async_script("""
			const done=arguments[arguments.length-1];
			(async()=>{
				const assert=(ok,label)=>{if(!ok)throw Error(label)};
				const wait=async test=>{for(let n=0;n<100;n++){if(test())return;await new Promise(r=>setTimeout(r,10))}throw Error('condition timeout')};
				const addWithoutInvitation=async()=>{
					document.getElementById('accounts-add-another').click();
					await wait(()=>document.getElementById('accounts-invite-normal')||document.getElementById('dialog').open||document.querySelector('.accounts-screen>.banner')||document.getElementById('accounts-confirm-enrollment'));
					if(document.getElementById('accounts-invite-normal')){
						assert(document.getElementById('accounts-invite-source')&&document.getElementById('accounts-invite-preview').disabled,'F119 explicit source choice, preview disabled without source');
						document.getElementById('accounts-invite-normal').click();
					}
				};
				const c=await import('./js/components/index.js');const i=await import('./js/i18n.js');const s=await import('./js/store.js');
			""" + body + """
				return {ok:true};
			})().then(done).catch(e=>done({ok:false,error:String(e),stack:e.stack}));
		""")
		self.assertTrue(result["ok"], result)

	def open(self, fixture, theme="dark", lang="en"):
		url = _harness.url("#/accounts", "accounts/" + fixture, theme, lang)
		same_url = _harness.driver.current_url == url
		_harness.open("#/accounts", "accounts/" + fixture, theme, lang).wait_ready()
		if same_url:
			print("accounts same-URL fixture before explicit refresh: " + str(_harness.driver.execute_script("return [...document.querySelectorAll('#view [data-account]')].map(n=>n.dataset.account)")), flush=True)
			# Reopening a mutated fixture must replace its page, not only its fragment.
			_harness.driver.refresh()
			_harness.wait_ready()
		if fixture in ("signin", "signin-failed"):
			from selenium.webdriver.support.ui import WebDriverWait
			expected = "waiting" if fixture == "signin" else "failed"
			WebDriverWait(_harness.driver, 5).until(lambda driver: driver.execute_script("return document.querySelector('[data-signin-state]')?.dataset.signinState") == expected)

	def test_f104_invited_rows_are_notes_not_accounts(self):
		self.open("invitations")
		self.js("""
		 const row=document.querySelector('[data-friend]'),friend=row.dataset.friend,sw=row.querySelector('[role=switch]');
		 assert(row.closest('.accounts-list')===document.querySelector('[data-account]').closest('.accounts-list'),'F104 one list');
		 assert(row.querySelector('.account-row-label').textContent==='Synthetic Friend'&&row.querySelector('.account-row-email bdi').textContent==='friend@example.invalid','F104 entered name and email');
		 assert(row.querySelector('.accounts-invited-chip').textContent===i.t('accounts.invited.status'),'F104 invited chip');
		 assert(sw.disabled&&sw.tabIndex===-1&&sw.getAttribute('aria-disabled')==='true'&&sw.getAttribute('aria-checked')==='false','F104 disabled off switch');
		 assert(document.getElementById(sw.getAttribute('aria-labelledby')).textContent===i.t('accounts.invited.auto_farm'),'F104 accessible No label');
		 assert(document.getElementById(sw.getAttribute('aria-describedby')).textContent===i.t('accounts.invited.auto_farm_disabled'),'F104 accessible reason');
		 sw.click();assert(sw.getAttribute('aria-checked')==='false','F104 switch never changes');
		 assert(!row.hasAttribute('data-account')&&!row.querySelector('[id^=accounts-signin-],[id^=accounts-pause-],[id^=accounts-rename-],[id^=accounts-remove-]'),'F104 no account actions');
		 assert(!s.get('accounts').some(account=>account.id===friend),'F104 friend never enters account store');
		 const actionIds=[...row.querySelectorAll('button:not(:disabled)')].map(button=>button.id);
		 assert(actionIds.length===2&&actionIds.every(id=>id.startsWith('accounts-friend-edit-')||id.startsWith('accounts-friend-remove-')),'F104 only note edit and remove');
		 assert([...document.querySelectorAll('[data-referral-figure]')].every(node=>node.textContent===i.t('accounts.referrals.unknown')),'F104 null referral figures stay unknown');
		 assert(document.querySelector('.accounts-referral-read time').dateTime==='2026-10-25T22:00:00Z','F104 read timestamp');
		 """)
		self.assertEqual([],_harness.errors())

	def test_f104_add_edit_remove_and_cancel_use_mock_notes(self):
		self.open("invitations-unknown")
		self.js("""
		 const api=window.pywebview.api,mutations=[];
		 for(const method of ['save_invited_friend','remove_invited_friend']){const original=api[method];api[method]=async request=>{mutations.push({method,request:structuredClone(request)});return original(request)}}
		 for(const method of ['add_account','sign_in_again','set_account_paused','remove_account','start_run','stop_run','get_account_picture'])api[method]=async()=>{throw Error('F104 forbidden friend account action '+method)};
		 const type=(id,value)=>{const node=document.getElementById(id);node.value=value;node.dispatchEvent(new Event('input',{bubbles:true}))};
		 document.getElementById('accounts-friend-add').click();assert(document.activeElement.id==='accounts-friend-name','F104 add focuses name');
		 assert(document.getElementById('accounts-friend-name').maxLength===40&&document.getElementById('accounts-friend-email').maxLength===254,'F104 contract limits');
		 document.getElementById('accounts-friend-save').click();assert(!mutations.length,'F104 invalid blank form never saved');
		 type('accounts-friend-name','  Jamie  ');type('accounts-friend-email','jamie@example.invalid');document.getElementById('accounts-friend-save').click();
		 await wait(()=>document.querySelectorAll('[data-friend]').length===1&&!document.querySelector('.accounts-friend-editor'));
		 const row=[...document.querySelectorAll('[data-friend]')].find(node=>node.querySelector('.account-row-label').textContent==='Jamie'),id=row.dataset.friend;
		 assert(mutations[0].request.name==='Jamie'&&mutations[0].request.email==='jamie@example.invalid'&&!Object.hasOwn(mutations[0].request,'id'),'F104 add exact note payload');
		 assert((await api.get_invited_friends({})).data.invited_friends.length===1,'F104 mock persisted add');
		 document.getElementById('accounts-friend-edit-'+id).click();type('accounts-friend-name','<script>Jamie</script>');type('accounts-friend-email','jamie.new@example.invalid');
		 document.getElementById('accounts-friend-save').click();await wait(()=>!document.querySelector('.accounts-friend-editor'));
		 assert(mutations[1].request.id===id&&document.querySelector('[data-friend="'+id+'"] .account-row-label').textContent==='<script>Jamie</script>'&&!document.querySelector('#view script'),'F104 edit is literal safe text');
		 const remove=()=>document.getElementById('accounts-friend-remove-'+id).click();remove();await wait(()=>document.getElementById('dialog').open);
		 assert(document.getElementById('dialog').textContent.includes(i.t('accounts.invited.remove_body')),'F104 remove explains local note only');
		 await new Promise(resolve=>{document.getElementById('dialog').addEventListener('close',resolve,{once:true});document.querySelector('#dialog .button-ghost').click()});assert(mutations.length===2,'F104 cancel no write');
		 remove();await wait(()=>document.getElementById('dialog').open);document.querySelector('#dialog .button-danger').click();
		 await wait(()=>!document.querySelector('[data-friend="'+id+'"]'));assert(mutations[2].method==='remove_invited_friend'&&Object.keys(mutations[2].request).join(',')==='id'&&mutations[2].request.id===id,'F104 exact remove note payload');
		 assert((await api.get_invited_friends({})).data.invited_friends.length===0&&document.querySelector('.accounts-invited-empty')&&s.get('accounts').length===1,'F104 local removal empty state and no account changes');
		 """)
		self.assertEqual([],_harness.errors())

	def test_f104_referral_copy_user_gesture_and_selection_fallback(self):
		from selenium.webdriver.support.ui import WebDriverWait
		self.open("invitations")
		self.js("""
		 const input=document.getElementById('accounts-referral-link');assert(input.readOnly&&input.dir==='ltr','F104 read-only link');
		 Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.f104Copied={text,active:navigator.userActivation.isActive}}}});
		 """)
		_harness.driver.find_element("id","accounts-referral-copy").click()
		WebDriverWait(_harness.driver,5).until(lambda driver:driver.execute_script("return Boolean(window.f104Copied)"))
		self.js("""
		 assert(window.f104Copied.active&&window.f104Copied.text===document.getElementById('accounts-referral-link').value,'F104 clipboard in real gesture');
		 assert(document.querySelector('.accounts-referral-feedback').textContent===i.t('accounts.referrals.copied'),'F104 copied only after success');
		 navigator.clipboard.writeText=async()=>{throw Error('synthetic clipboard denial')};
		 """)
		_harness.driver.find_element("id","accounts-referral-copy").click()
		self.js("""
		 await wait(()=>document.querySelector('.accounts-referral-feedback').textContent===i.t('accounts.referrals.select'));
		 const input=document.getElementById('accounts-referral-link');assert(document.activeElement===input&&input.selectionStart===0&&input.selectionEnd===input.value.length,'F104 denied clipboard selects exact link');
		 """)
		self.assertEqual([],_harness.errors())

	def test_f104_unreadable_codes_empty_state_and_null_vs_zero(self):
		self.open("invitations-unknown")
		self.js("""
		 const {referralUnreadableCodes}=await import('./js/screens/accounts/invitations.js'),router=await import('./js/router.js');
		 assert(document.querySelector('.accounts-invited-empty').textContent===i.t('accounts.invited.empty')&&!document.querySelector('[data-friend]'),'F104 empty local notes');
		 let info;window.pywebview.api.get_referral_info=async()=>({ok:true,data:structuredClone(info)});
		 const remount=async()=>{router.navigate('#/overview');await wait(()=>document.querySelector('.overview-screen'));router.navigate('#/accounts');await wait(()=>document.querySelector('.accounts-referrals')&&!document.querySelector('.accounts-referrals .spinner'))};
		 for(const code of referralUnreadableCodes){
		  info={link:'https://rewards.bing.com/refer?code=SYNTHETIC',link_found:true,referrals:null,friends:null,read_at:'2026-10-25T22:00:00Z',unreadable:code};await remount();
		  const link=document.getElementById('accounts-referral-link'),copy=document.getElementById('accounts-referral-copy');
		  assert(link&&link.value===info.link&&link.readOnly&&link.dir==='ltr'&&copy&&!copy.disabled,'F111 last verified link remains copyable '+code);
		  assert(document.querySelector('[data-referral-unreadable]').textContent===i.t('accounts.referrals.unreadable.'+code),'F104 exact unreadable key '+code);
		  assert([...document.querySelectorAll('[data-referral-figure]')].every(node=>node.textContent===i.t('accounts.referrals.unknown')),'F104 null not zero '+code);
		  assert(document.querySelector('.accounts-referral-read time').dateTime===info.read_at,'F111 retained link keeps its own read time '+code);
		 }
		 info={link:null,link_found:false,referrals:0,friends:[],read_at:null,unreadable:null};await remount();
		 assert([...document.querySelectorAll('[data-referral-figure]')].every(node=>node.textContent===i.formatNumber(0)),'F104 observed zero is distinct from unknown');
		 assert(!document.querySelector('.accounts-referral-read')&&!document.getElementById('accounts-referral-copy'),'F104 absent timestamp and link not invented');
		 assert(!document.getElementById('accounts-referral-link')&&document.querySelector('.accounts-referral-unavailable').textContent===i.t('accounts.referrals.link_unknown'),'F111 null link keeps exact unavailable text');
		 """)
		self.assertEqual([],_harness.errors())

	def test_f111_retained_referral_link_with_failed_attempt_capture(self):
		_harness.viewport(860,600)
		try:
			self.open('invitations','light')
			self.js("""
			 const api=window.pywebview.api,original=api.get_referral_info,response=await original({account_id:'account-1'});
			 assert(response.ok&&response.data.link==='https://rewards.bing.com/refer?code=SYNTHETIC'&&response.data.unreadable==='page_unknown','F111 synthetic retained-link producer fixture');
			 api.get_referral_info=async()=>structuredClone(response);
			 const router=await import('./js/router.js');router.navigate('#/overview');await wait(()=>document.querySelector('.overview-screen'));router.navigate('#/accounts');
			 await wait(()=>document.querySelector('[data-referral-unreadable=page_unknown]')&&document.getElementById('accounts-referral-link'));
			 const section=document.querySelector('.accounts-referrals'),link=document.getElementById('accounts-referral-link');
			 assert(link.value===response.data.link&&link.readOnly&&!document.getElementById('accounts-referral-copy').disabled,'F111 retained link field and Copy');
			 assert(section.querySelector('[data-referral-unreadable]').textContent===i.t('accounts.referrals.unreadable.page_unknown'),'F111 exact latest failed-attempt status');
			 assert([...section.querySelectorAll('[data-referral-figure]')].every(node=>node.textContent===i.t('accounts.referrals.unknown')),'F111 failed attempt never invents figures');
			 const time=section.querySelector('time');assert(time.dateTime===response.data.read_at&&time.textContent===i.t('accounts.referrals.read_at',{date:i.formatDate(response.data.read_at),time:i.formatTime(response.data.read_at)}),'F111 link own read time unchanged');
			 section.scrollIntoView({block:'start'});document.activeElement?.blur();await document.fonts.ready;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
			 assert(section.getBoundingClientRect().top>=0&&section.getBoundingClientRect().bottom<=innerHeight,'F111 complete referral section visible');
			 """)
			folder=os.environ.get('RF_F108_CAPTURE')
			if folder:
				path=Path(folder)/'f111';path.mkdir(parents=True,exist_ok=True)
				self.assertTrue(_harness.driver.save_screenshot(str(path/'referral-retained-link-page-unknown-light-860x600.png')))
			self.assertEqual([],_harness.errors())
		finally:
			_harness.viewport(1280,800)

	def test_f104_friend_save_failure_and_remove_failure_retain_note(self):
		self.open("invitations")
		self.js("""
		 const api=window.pywebview.api,bad={ok:false,error:{code:'internal',message_key:'errors.app.internal',params:{},detail:null,fields:null}};
		 api.save_invited_friend=async()=>bad;
		 document.querySelector('[id^=accounts-friend-edit-]').click();document.getElementById('accounts-friend-save').click();
		 await wait(()=>document.querySelector('.accounts-list .banner.tone-error'));
		 assert(document.querySelector('.accounts-friend-editor')&&!document.getElementById('accounts-friend-save').disabled&&document.querySelector('[data-friend]'),'F104 save error keeps input and note');
		 document.getElementById('accounts-friend-cancel').click();api.remove_invited_friend=async()=>bad;
		 document.querySelector('[id^=accounts-friend-remove-]').click();await wait(()=>document.getElementById('dialog').open);document.querySelector('#dialog .button-danger').click();
		 await wait(()=>document.querySelector('.accounts-list .banner.tone-error'));assert(document.querySelector('[data-friend]'),'F104 remove error never loses note');
		 """)
		self.assertEqual([],_harness.errors())

	def test_f104_failed_note_and_referral_loads_keep_account_list(self):
		self.open("invitations")
		self.js("""
		 const router=await import('./js/router.js'),bad={ok:false,error:{code:'accounts_unknown',message_key:'errors.app.accounts_unknown',params:{},detail:null,fields:null}};
		 window.pywebview.api.get_invited_friends=async()=>bad;window.pywebview.api.get_referral_info=async()=>bad;
		 router.navigate('#/overview');await wait(()=>document.querySelector('.overview-screen'));router.navigate('#/accounts');
		 await wait(()=>document.querySelectorAll('.accounts-screen .banner.tone-error').length===2);
		 assert(document.querySelector('[data-account]')&&document.querySelectorAll('.accounts-screen .banner-message').length===2,'F104 read errors preserve real accounts');
		 assert([...document.querySelectorAll('.accounts-screen .banner-message')].every(node=>node.textContent===i.t('errors.app.accounts_unknown')),'F104 real envelope error message');
		 assert(!document.getElementById('accounts-referral-copy')&&!document.getElementById('accounts-referral-link'),'F104 failed refresh does not offer old referral link');
		 """)
		self.assertEqual([],_harness.errors())

	def test_f104_layout_long_rtl_and_parent_captures(self):
		for width,height in ((1280,800),(860,600)):
			_harness.viewport(width,height)
			for theme in ("light","dark"):
				for lang in ("en","x-long","x-rtl"):
					with self.subTest(width=width,theme=theme,lang=lang):
						self.open("invitations",theme,lang)
						self.js("assert(!document.querySelector('[style]'),'F104 CSP');assert(document.documentElement.scrollWidth<=innerWidth+1&&document.getElementById('view').scrollWidth<=document.getElementById('view').clientWidth+1,'F104 list no overflow');document.getElementById('accounts-friend-add').click();assert(document.documentElement.scrollWidth<=innerWidth+1,'F104 editor no overflow');document.getElementById('accounts-friend-cancel').click();")
						if lang=="en":
							folder=os.environ.get("RF_F104_CAPTURE")
							if folder:
								from PIL import Image
								import base64,io
								_harness.driver.execute_async_script("const done=arguments[0];document.fonts.ready.then(()=>requestAnimationFrame(()=>requestAnimationFrame(done)))")
								_harness.driver.execute_script("document.getElementById('view').scrollTop=0;document.activeElement?.blur()")
								png=base64.b64decode(_harness.driver.execute_cdp_cmd('Page.captureScreenshot',{'format':'png','fromSurface':True,'captureBeyondViewport':False})['data']);self.assertEqual((width,height),Image.open(io.BytesIO(png)).size)
								path=Path(folder).resolve();self.assertTrue(path.is_relative_to(Path(tempfile.gettempdir()).resolve()));path.mkdir(parents=True,exist_ok=True);(path/f'accounts-{theme}-{width}x{height}.png').write_bytes(png)
						self.assertEqual([],_harness.errors())

	def test_footer_and_account_row_share_identity_initial_priority(self):
		self.open("list")
		self.js("""
			const {accountInitial}=await import('./js/components/kit.js');
			const host=document.createElement('section');document.querySelector('#view').append(host);
			const cases=[
				{label:'Label',name:'  Terry Example ',email:'chris@example.com',initial:'T'},
				{label:'Label',name:'  ',email:' chris@example.com ',initial:'C'},
				{label:' Sample account ',name:null,email:null,initial:'S'},
				{label:'fallback',name:String.fromCodePoint(0x10428)+' Sample',email:'mail@example.com',initial:String.fromCodePoint(0x10400)},
				{label:'  ',name:null,email:' ',initial:''},
			];
			for(const sample of cases){
				const account={...s.get('accounts')[0],...sample};delete account.initial;
				s.set('accounts',[account]);
				const footer=document.querySelector('.nav-account .avatar');
				const component=c.accountRow.create({account});host.replaceChildren(component);
				const row=component.querySelector('.avatar');
				assert(accountInitial(account)===sample.initial,'helper expected identity initial');
				assert(footer.textContent===sample.initial && row.textContent===sample.initial,'footer and row share helper priority');
			}
			assert(accountInitial(null)==='' && accountInitial(undefined)==='','no account is empty');
			s.set('accounts',[]);assert(document.querySelector('.nav-account .avatar').textContent==='','empty footer uses helper');
		""")
		self.assertEqual([], _harness.errors())

	def test_f68_account_rows_use_synthetic_picture_and_keep_initial_fallback(self):
		_harness.viewport(1131, 643)
		_harness.open("#/accounts", "pictures/accounts").wait_ready()
		self.js("""
			await wait(()=>document.querySelector('[data-account="account-1"] .avatar img')?.naturalWidth>0);
			const first=document.querySelector('[data-account="account-1"] .avatar'),second=document.querySelector('[data-account="account-2"] .avatar'),image=first.querySelector('img');
			assert(image.alt==='' && image.getAttribute('src').startsWith('data:image/png;base64,') && image.naturalWidth<=128 && image.naturalHeight<=128,'F68 decorative local PNG');
			assert(!second.querySelector('img') && second.textContent==='C','F68 no picture initial');
			assert(document.querySelector('.nav-account .avatar img').getAttribute('src')===image.getAttribute('src'),'row and sidebar share picture');
			const account={...s.get('accounts')[0],picture_rev:null};const plain=c.accountRow.create({account});document.querySelector('#view').append(plain);
			assert(plain.querySelector('.avatar').textContent==='T' && !plain.querySelector('img'),'F68 initial retained for null rev');
			const before=first.getBoundingClientRect(),after=plain.querySelector('.avatar').getBoundingClientRect();assert(Math.abs(before.width-after.width)<1 && Math.abs(before.height-after.height)<1,'F68 avatar dimensions unchanged');
		""")
		self.assertEqual([], _harness.errors())

	def test_signin_cancel_all_states_and_foreign_push(self):
		self.open("signin")
		self.js("""
			let req;window.pywebview.api.cancel_signin=async value=>{req=value;return {ok:true,data:{}}};
			document.getElementById('accounts-cancel-signin').click();await wait(()=>req);assert(req.account_id==='jamie','cancel id');
			assert(document.querySelector('[data-signin-state]').dataset.signinState==='waiting','request not terminal');
			assert(document.querySelectorAll('.accounts-signin .banner').length===1 && document.querySelector('.accounts-signin .banner-message').textContent===i.t('accounts.signin.cancelling'),'F58 cancellation wins');
			window.rf.receive([{topic:'signin.status',data:{account_id:'jamie',state:'busy',error_code:null,message_key:'accounts.signin.busy',detail:null,at:'2026-10-14T20:00:00Z'}}]);
			window.rf.receive([{topic:'signin.status',data:{account_id:'foreign',state:'signed_in',error_code:null,message_key:'accounts.signin.signed_in',detail:null,at:'2026-10-14T20:00:00Z'}}]);
			assert(document.querySelector('[data-signin-state]').dataset.signinState==='busy','foreign push ignored');
            assert(!document.getElementById('accounts-add-another').disabled,'F108 add available after terminal status');
		""")
		self.assertEqual([], _harness.errors())

	def test_producer_statuses_have_one_banner_and_account_identity(self):
		with tempfile.TemporaryDirectory(prefix="accounts-status-proof-") as directory, patch.object(signin.paths, "runs_dir", return_value=Path(directory)):
			statuses = [signin._write_status("account-1", state) for state in ("starting", "waiting", "verifying", "signed_in", "failed", "timed_out", "cancelled", "busy")]
			statuses.extend(signin._write_status("account-1", state, code) for state, code in (("failed", "edge_missing"), ("failed", "crashed"), ("signed_in", "terms_of_use")))
		_harness.viewport(1100, 760)
		for theme, lang in (("dark", "en"), ("light", "en"), ("dark", "de")):
			self.open("list", theme, lang)
			self.js("""
				const statuses=""" + json.dumps(statuses) + """;
				for(const status of statuses){
					window.rf.receive([{topic:'signin.status',data:status}]);
					const panel=document.querySelector('.accounts-signin');const banners=panel.querySelectorAll('.banner');
					assert(banners.length===1,'F58 Accounts one status banner');
					assert(banners[0].querySelector('.banner-message').textContent===i.t(status.message_key,{account:'Alex (synthetic)'}),'F58 producer text');
					const tone=status.error_code==='terms_of_use'?'warning':status.state==='signed_in'?'success':['failed','busy','timed_out'].includes(status.state)?'warning':'info';
					assert(banners[0].classList.contains('tone-'+tone),'F58 state tone');
					assert(Boolean(panel.querySelector('.spinner'))===['starting','waiting','verifying'].includes(status.state),'spinner preserved');
				}
				window.rf.receive([{topic:'signin.status',data:{...statuses[0],message_key:null}}]);
				assert(document.querySelectorAll('.accounts-signin .banner').length===1 && document.querySelector('.accounts-signin .banner-message').textContent===i.t('accounts.signin.starting'),'absent message fallback');
			""")
			self.open("list", theme, lang)
			self.js("""
				const first=document.querySelector('[data-account="account-1"]');const second=document.querySelector('[data-account="account-2"]');const third=document.querySelector('[data-account="account-3"]');
				assert(first.querySelector('.account-row-email bdi').textContent==='alex@example.com' && first.querySelector('.account-row-email bdi').dir==='auto','F59 identity with isolation');
				assert(first.querySelector('.avatar').textContent==='T' && second.querySelector('.avatar').textContent==='C' && third.querySelector('.avatar').textContent==='S','F59 name email label avatar priority');
				assert(!third.querySelector('.account-row-email'),'unknown identity has no invented address');
				assert(!second.querySelector('.account-row-balance'),'F76 unknown points omitted');
				assert(first.querySelector('.account-row-balance').textContent===i.t('common.points.total',{count:4200}),'known points unchanged');
				assert(document.documentElement.scrollWidth<=innerWidth+1,'no row overflow');
			""")
			self.assertTrue(Path(_harness.screenshot(f"accounts-f59-identity-{theme}-{lang}-1100x760")).is_file())
			self.assertEqual([], _harness.errors())

	def test_rename_pause_resume_and_label_safety(self):
		self.open("list")
		self.js("""
            const add=document.getElementById('accounts-add-another');assert(!add.disabled,'F108 add available');
            assert(add.closest('.accounts-list'),'F108 add belongs to same list');
			const header=document.querySelector('.accounts-screen .screen-header');assert(header.children.length===1 && header.querySelector('h1') && !header.querySelector('p'),'F76 heading only');
			assert(!document.querySelector('.accounts-preview'),'F76 preview caption removed');
			assert(document.querySelector('[data-account="account-2"]').textContent.includes(i.t('common.account.signed_out')) && i.t('common.account.signed_out')==='Signed out','B5 status is not action');
			let rename;window.pywebview.api.rename_account=async req=>{rename=req;const a=s.get('accounts').find(a=>a.id===req.account_id);return {ok:true,data:{account:{...a,label:req.label}}}};
			const more=document.getElementById('accounts-more-account-1');assert(!more.open,'F76 secondary actions initially folded');more.querySelector('summary').click();assert(more.open&&more.querySelector('summary .icon'),'F76 same chevron disclosure');
			document.getElementById('accounts-rename-account-1').click();assert(document.activeElement.id==='accounts-name','inline focus');
			const input=document.getElementById('accounts-name');assert(input.maxLength===40 && input.labels.length,'shared labeled field');
			input.value='<script>synthetic</script>';input.dispatchEvent(new Event('input',{bubbles:true}));
			document.getElementById('accounts-save-name').click();await wait(()=>rename && !document.getElementById('accounts-name'));
			assert(rename.account_id==='account-1' && rename.label==='<script>synthetic</script>','rename payload');
			assert(!document.querySelector('#view script') && document.getElementById('view').textContent.includes(rename.label),'label stays text');
			let pause;window.pywebview.api.set_account_paused=async req=>{pause=req;const a=s.get('accounts').find(a=>a.id===req.account_id);return {ok:true,data:{account:{...a,paused:req.paused}}}};
			document.getElementById('accounts-pause-account-1').click();await wait(()=>pause && s.get('accounts')[0].paused);
			assert(pause.paused===true,'pause');document.getElementById('accounts-pause-account-1').click();await wait(()=>!s.get('accounts')[0].paused);assert(pause.paused===false,'resume');
		""")
		self.assertEqual([], _harness.errors())

	def test_f76_secondary_actions_are_folded_but_keyboard_reachable(self):
		self.open("list")
		self.js("""
		 assert(!document.querySelector('.accounts-intro,.accounts-preview'),'F76 no framing paragraphs');
		 const rows=[...document.querySelectorAll('.accounts-entry')];assert(rows.length===s.get('accounts').length,'F76 every account retained');
		 for(const entry of rows){
		  const more=entry.querySelector('.accounts-disclosure'),summary=more.querySelector('summary');
		  assert(!more.open&&summary.querySelector('.icon'),'F76 one closed chevron for secondary actions');
		  assert(entry.querySelector('.account-row button').textContent===i.t('accounts.action.signin_again'),'F76 sign in remains immediate');
		  summary.click();assert(more.open&&more.querySelectorAll('button').length>=2,'F76 rename pause remove still reachable');
		  const id=entry.querySelector('.account-row').dataset.account,account=s.get('accounts').find(a=>a.id===id);
		  if(account.balance===null)assert(!entry.querySelector('.account-row-balance'),'F76 no unknown number label');
		 }
         assert(!document.getElementById('accounts-add-another').disabled,'F108 replaces unsupported action with enrollment-aware add');
		 """)
		self.assertEqual([], _harness.errors())

	def test_f76_more_actions_native_keyboard_and_focus(self):
		from selenium.webdriver.common.keys import Keys
		self.open("list")
		summary=_harness.driver.find_element("css selector",".accounts-entry .accounts-disclosure > summary")
		summary.click()
		summary.send_keys(Keys.ENTER)
		self.assertFalse(_harness.driver.execute_script("return arguments[0].parentElement.open",summary))
		summary.send_keys(Keys.SPACE)
		self.assertTrue(_harness.driver.execute_script("return arguments[0].parentElement.open",summary))
		summary.send_keys(Keys.TAB)
		self.assertEqual("accounts-rename-account-1",_harness.driver.execute_script("return document.activeElement.id"))
		_harness.driver.switch_to.active_element.send_keys(Keys.ENTER)
		self.assertEqual("accounts-name",_harness.driver.execute_script("return document.activeElement.id"))
		_harness.driver.find_element("id","accounts-cancel-name").click()
		self.assertEqual("accounts-rename-account-1",_harness.driver.execute_script("return document.activeElement.id"))
		self.assertEqual([],_harness.errors())

	def test_remove_choices_warning_and_root_failure(self):
		for delete in (False, True):
			self.open("recycle-warning")
			self.js("""
				const removeData=""" + json.dumps(delete) + """;let req;const old=window.pywebview.api.remove_account;window.pywebview.api.remove_account=async value=>{req=value;return old(value)};
				document.getElementById('accounts-remove-jamie').click();await wait(()=>document.getElementById('dialog').open);
				const dialog=document.getElementById('dialog');const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
				const buttons=[...dialog.querySelectorAll('button')];assert(buttons.length===3,'cancel and both choices');buttons[removeData?2:1].click();await closed;
				await wait(()=>document.querySelector('[data-account="@root"]'));
				assert(req.account_id==='jamie' && req.delete_data===removeData,'exact remove choice');
				assert(document.getElementById('view').textContent.includes('C:\\\\Synthetic\\\\jamie'),'warning path');
				assert(location.hash==='#/accounts','remaining root stays');
				assert(document.querySelectorAll('#view .banner.tone-warning').length===1 && document.querySelectorAll('#toasts .tone-warning').length===0,'N3 retained account warning appears once inline');
				assert(getComputedStyle(document.querySelector('.accounts-help')).borderBlockEndWidth==='0px','B4 older-account note has no short rule');
			""")
		self.open("root-error")
		self.js("""
			let req;const old=window.pywebview.api.remove_account;window.pywebview.api.remove_account=async value=>{req=value;return old(value)};
			document.getElementById('accounts-remove-%40root').click();await wait(()=>document.getElementById('dialog').open);
			const dialog=document.getElementById('dialog');const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
			assert(dialog.querySelectorAll('button').length===2,'root no keep');dialog.querySelector('.button-danger').click();await closed;
			await wait(()=>document.querySelector('#view [role=alert]'));assert(req.delete_data===true,'root deletion required');
			assert(s.get('accounts')[0].id==='@root' && document.querySelector('[data-account="@root"]'),'root retained');
			assert(document.getElementById('view').textContent.includes(i.t('errors.app.accounts_root_recycle_failed')),'F20 error');
		""")
		self.assertEqual([], _harness.errors())

	def test_last_removal_warning_survives_once_in_setup(self):
		_harness.viewport(1280, 800)
		for theme in ("dark", "light"):
			for lang in ("en", "x-long"):
				self.open("last-removed-warning", theme, lang)
				self.js("""
					const dialog=document.getElementById('dialog');document.getElementById('accounts-remove-jamie').click();await wait(()=>dialog.open);
					const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));dialog.querySelectorAll('.button-danger')[1].click();await closed;
					await wait(()=>location.hash==='#/setup/1' && document.querySelector('.setup-screen'));
					assert(document.querySelectorAll('#toasts .tone-warning').length===1 && document.querySelectorAll('#view .banner.tone-warning').length===0,'N3 last-account warning appears once as toast');
					const warning=document.querySelector('#toasts .tone-warning');assert(warning.textContent.includes('C:\\\\Synthetic\\\\jamie') && warning.checkVisibility({checkVisibilityCSS:true}),'redirect warning visible');
				""")
				self.assertTrue(Path(_harness.screenshot(f"accounts-last-removal-warning-{theme}-{lang}")).is_file())
				self.assertEqual([], _harness.errors())

	def test_last_removal_routes_setup_and_cancel_does_nothing(self):
		self.open("last-removed")
		self.js("""
			let called=0;const old=window.pywebview.api.remove_account;window.pywebview.api.remove_account=async req=>{called++;return old(req)};
			const dialog=document.getElementById('dialog');
			document.getElementById('accounts-remove-jamie').click();await wait(()=>dialog.open);
			let closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));dialog.querySelector('.button-ghost').click();await closed;await new Promise(requestAnimationFrame);assert(called===0,'cancel no mutation');
			document.getElementById('accounts-remove-jamie').click();await wait(()=>dialog.open);
			closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));dialog.querySelector('.button-danger').click();await closed;
			await wait(()=>location.hash==='#/setup/1');assert(s.get('accounts').length===0 && !s.get('appInfo').has_accounts,'empty account state');
		""")

	def prepare(self, fixture):
		if fixture == "rename-error":
			self.js("document.getElementById('accounts-rename-jamie').click();document.getElementById('accounts-save-name').click();await wait(()=>document.querySelector('#view [role=alert]'));")
		elif fixture == "pause-error":
			self.js("document.getElementById('accounts-pause-jamie').click();await wait(()=>document.querySelector('#view [role=alert]'));")
		elif fixture in ("recycle-warning", "root-error", "remove-busy"):
			identity = "%40root" if fixture == "root-error" else "jamie"
			self.js(f"document.getElementById('accounts-remove-{identity}').click();await wait(()=>document.getElementById('dialog').open);const dialog=document.getElementById('dialog');const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{{once:true}}));dialog.querySelectorAll('.button-danger')[" + ("0" if fixture == "root-error" else "1") + "].click();await closed;await wait(()=>document.querySelector('#view [role=alert]'));")

	def test_invite_paragraphs_wrap_in_narrow_long_and_rtl_states(self):
		for lang in ("x-long", "x-rtl"):
			for theme in ("dark", "light"):
				with self.subTest(lang=lang, theme=theme):
					_harness.viewport(860, 600)
					self.open("invite-onboarding", theme, lang)
					self.js("""
					 const measurements=[],fits=stage=>{
					  const paragraphs=[...document.querySelectorAll('.accounts-editor > p,.accounts-signin > p')];
					  assert(paragraphs.length>=2,'recovery and limitation paragraphs exist');
					  assert(paragraphs.every(p=>getComputedStyle(p).overflowWrap==='anywhere'&&p.scrollWidth<=p.clientWidth),'all invite paragraphs wrap within their containers');
					  assert(document.documentElement.scrollWidth<=document.documentElement.clientWidth&&view.scrollWidth<=view.clientWidth,'narrow long and RTL layout stays within client width');
					  measurements.push({stage,view:{scroll:view.scrollWidth,client:view.clientWidth},paragraphs:paragraphs.map(p=>({scroll:p.scrollWidth,client:p.clientWidth}))});
					 };
					 fits('recovery');
					 const api=window.pywebview.api,confirmed=s.get('accounts').map(account=>({...account,identity_status:'confirmed'}));
					 api.list_accounts=async()=>({ok:true,data:{accounts:confirmed}});
					 const router=await import('./js/router.js');router.navigate('#/overview');await wait(()=>document.querySelector('.overview-screen'));
					 router.navigate('#/accounts');await wait(()=>document.querySelectorAll('[data-identity=confirmed]').length===confirmed.length);
					 document.getElementById('accounts-add-another').click();await wait(()=>document.getElementById('accounts-invite-source'));
					 fits('choice');
					 const source=document.getElementById('accounts-invite-source');assert(source.options.length>1,'fixture offers an existing source account');source.value=source.options[1].value;source.dispatchEvent(new Event('change',{bubbles:true}));
					 await wait(()=>!document.getElementById('accounts-invite-preview').disabled);document.getElementById('accounts-invite-preview').click();
					 await wait(()=>document.getElementById('accounts-invite-link'));fits('preview');
					 document.getElementById('accounts-invite-open').click();await wait(()=>document.querySelector('[data-signin-state=invite_waiting]')&&document.getElementById('accounts-invite-continue'));
					 assert(document.querySelector('.accounts-signin > p'),'waiting panel limitation uses the same wrapping class');fits('waiting');
					 window.inviteParagraphMeasurements=measurements;
					""")
					print(json.dumps({"lang": lang, "theme": theme, "width": 860, "measurements": _harness.driver.execute_script("return window.inviteParagraphMeasurements")}), flush=True)
					self.assertEqual([], _harness.errors())
		_harness.viewport(1280, 800)

	def test_z_fixture_matrix_keyboard_and_screenshots(self):
		for width, height in ((1280, 800), (860, 600)):
			_harness.viewport(width, height)
			for lang in ("en", "x-long", "x-rtl"):
				for theme in ("dark", "light"):
					for fixture in FIXTURES:
						with self.subTest(fixture=fixture, theme=theme, lang=lang, width=width):
							self.open(fixture, theme, lang)
							self.prepare(fixture)
							self.js("assert(document.title===i.t('accounts.title'),'title contract');assert(!document.querySelector('[style]'),'CSP');assert(document.documentElement.scrollWidth<=innerWidth+1 && document.getElementById('view').scrollWidth<=document.getElementById('view').clientWidth+1,'no overflow');")
							self.js("if(document.querySelector('.accounts-entry'))assert(document.getElementById('accounts-add-another'),'F108 add exists');assert(!document.querySelector('.accounts-limit'),'old footnote removed');")
							self.js("for(const help of document.querySelectorAll('.accounts-help'))assert(getComputedStyle(help).borderBlockEndWidth==='0px','B4 no short rule');if(document.querySelector('.accounts-help') && document.querySelector('#view .banner.tone-warning'))assert(document.querySelectorAll('#view .banner.tone-warning').length===1 && !document.querySelector('#toasts .tone-warning'),'N3 warning once on retained Accounts');")
							if width == 1280:
								ids = _harness.driver.execute_script("return [...document.querySelectorAll('#view button,#view input,#view select,#view summary,#view a[href]')].filter(n=>n.tabIndex>=0 && !n.matches(':disabled') && !n.closest('[inert],[hidden]') && n.checkVisibility({checkVisibilityCSS:true}) && (!n.closest('details:not([open])') || n.matches('summary'))).map((n,j)=>{n.id ||= 'accounts-tab-'+j;return n.id})")
								order = _harness.tab_order(len(ids) + 2)
								self.assertTrue(set(ids).issubset(order), {"missing": sorted(set(ids) - set(order)), "seen": sorted(set(ids) & set(order))})
								_harness.driver.execute_script("document.getElementById('view').scrollTop=0;document.getElementById('view').focus()")
								self.assertTrue(Path(_harness.screenshot(f"accounts-{fixture}-{theme}-{lang}")).is_file())
							self.assertEqual([], _harness.errors())
					print(f"accounts matrix {width} {lang} {theme}: {len(FIXTURES)}", flush=True)
		print(f"accounts screenshots: {SCREENSHOT_DIR}; signed_in={_harness.signed_in}", flush=True)
