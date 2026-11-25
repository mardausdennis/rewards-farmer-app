import _bootstrap  # noqa: F401

import json
import os
import unittest
from pathlib import Path

from ui.harness import UI_DIR, UiHarness

_enabled = os.environ.get('REWARDS_APP_UI_TESTS') == '1'
_harness = None


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


class Account0311SourceTests(unittest.TestCase):
	def test_new_catalog_and_figures_fixture_follow_freeze(self):
		catalog = json.loads((UI_DIR/'i18n/en/accounts.json').read_text(encoding='utf-8'))['messages']
		quote = '"You may open and use only one Rewards Account, even if you have multiple email addresses. Each household is limited to six Rewards Accounts."'
		self.assertEqual(quote, catalog['accounts.household.terms']['text'])
		self.assertEqual('Microsoft Services Agreement, section 14.m.iv (published July 30, 2026)', catalog['accounts.household.source']['text'])
		fixture = json.loads((UI_DIR/'mock/accounts/ui0311-referrals.json').read_text(encoding='utf-8'))
		self.assertIs(fixture['synthetic'], True)
		from test_mock_shapes import SUCCESS_KEYS
		for response in fixture['referral_responses'].values():
			self.assertEqual(SUCCESS_KEYS['get_referral_info'], set(response))
			self.assertIn(response['figures_state'], ('read','unknown','stale'))
			for key in ('referrals','friends','pending_points','earned_points'):
				self.assertTrue(response[key] is None or type(response[key]) is int and response[key] >= 0)
			if response['figures_state']=='unknown':
				self.assertTrue(all(response[key] is None for key in ('referrals','friends','pending_points','earned_points','referral_status','figures_read_at')))


@unittest.skipUnless(_enabled, 'Set REWARDS_APP_UI_TESTS=1 for isolated 0311 UI proof')
class Account0311Tests(unittest.TestCase):
	def open(self, fixture='ui0311-referrals'):
		_harness.open('#/accounts','accounts/'+fixture,'light').wait_ready()

	def js(self, source):
		result = _harness.driver.execute_async_script("""
		 const done=arguments[arguments.length-1];(async()=>{
		 const assert=(ok,message)=>{if(!ok)throw Error(message)},wait=async fn=>{for(let n=0;n<250;n++){if(fn())return;await new Promise(resolve=>setTimeout(resolve,10))}throw Error('0311 timeout')};
		 const s=await import('./js/store.js'),i=await import('./js/i18n.js'),router=await import('./js/router.js');
		 const close=async action=>{const d=document.getElementById('dialog'),closed=new Promise(resolve=>d.addEventListener('close',resolve,{once:true}));d.querySelector('[data-dialog-action="'+action+'"]').click();await closed};
		 const agree=async()=>{await wait(()=>document.getElementById('accounts-household-confirm'));const d=document.getElementById('dialog');assert(document.activeElement.dataset.dialogAction==='cancel','Cancel default');assert(d.querySelector('[data-dialog-action=add]').disabled&&!document.getElementById('accounts-household-confirm').checked,'unchecked Add disabled');document.getElementById('accounts-household-confirm').click();assert(!d.querySelector('[data-dialog-action=add]').disabled,'explicit check enables Add');await close('add')};
		 const record=()=>{const calls=[];for(const name of ['add_account','begin_enrollment','preview_invite_account','start_invite_account','continue_invite_account','confirm_enrollment','cancel_signin']){const original=window.pywebview.api[name];window.pywebview.api[name]=async request=>{calls.push({name,request:structuredClone(request)});return original(request)}}return calls};
		 const remount=async()=>{await router.navigate('#/overview');await wait(()=>document.querySelector('.overview-screen'));await router.navigate('#/accounts');await wait(()=>document.querySelector('.accounts-list')&&document.getElementById('accounts-referral-account'))};
		 """+source+"""
		 return {ok:true};})().then(done,error=>done({ok:false,error:String(error),stack:error.stack}));
		 """)
		self.assertTrue(result['ok'], result)

	def test_referral_read_unknown_stale_paused_scope_and_race(self):
		self.open()
		self.js("""
		 const select=()=>document.getElementById('accounts-referral-account'),figures=()=>Object.fromEntries([...document.querySelectorAll('[data-referral-figure]')].map(node=>[node.dataset.referralFigure,node.textContent]));
		 assert(select().options.length===3&&select().value==='account-1','all real accounts including paused');
		 assert(JSON.stringify(figures())===JSON.stringify({referrals:i.formatNumber(4),friends:i.formatNumber(3),pending_points:i.formatNumber(250),earned_points:i.formatNumber(1000)}),'read figures exact');
		 assert(document.querySelector('[data-referral-status]').textContent===i.t('accounts.referrals.status.mixed'),'mixed status');
		 const figuresTime=document.querySelector('[data-figures-state=read] time'),linkTime=document.querySelector('.accounts-referral-read time');
		 assert(figuresTime.dateTime==='2026-11-25T17:00:00Z'&&figuresTime.textContent===i.t('accounts.referrals.figures_read_at',{date:i.formatDate(figuresTime.dateTime),time:i.formatTime(figuresTime.dateTime)}),'figures own labelled read time');
		 assert(linkTime.dateTime==='2026-11-25T16:00:00Z'&&linkTime.textContent===i.t('accounts.referrals.link_read_at',{date:i.formatDate(linkTime.dateTime),time:i.formatTime(linkTime.dateTime)}),'link independent labelled read time');
		 assert(document.querySelector('.accounts-referral-source').textContent===i.t('accounts.referrals.microsoft_figures'),'Microsoft attribution not confirmation');
		 select().value='account-2';select().dispatchEvent(new Event('change'));await wait(()=>document.querySelector('.accounts-referrals').getAttribute('aria-busy')==='false');
		 assert(Object.values(figures()).every(text=>text===i.t('accounts.referrals.unknown'))&&!document.querySelector('[data-figures-state]'),'unknown never zero');
		 assert(document.querySelector('[data-referral-status]').textContent===i.t('accounts.referrals.unknown'),'unknown status');
		 select().value='account-3';select().dispatchEvent(new Event('change'));await wait(()=>document.querySelector('[data-figures-state=stale]'));
		 assert(figures().pending_points===i.formatNumber(0)&&figures().earned_points===i.formatNumber(500),'known zero distinct');
		 assert(document.querySelector('[data-figures-state=stale] time').textContent===i.t('accounts.referrals.last_read',{date:i.formatDate('2026-11-24T10:00:00Z'),time:i.formatTime('2026-11-24T10:00:00Z')}),'stale original timestamp explicitly older');
		 const original=window.pywebview.api.get_referral_info;let release;window.pywebview.api.get_referral_info=async request=>request.account_id==='account-1'?new Promise(resolve=>{release=()=>original(request).then(resolve)}):original(request);
		 select().value='account-1';select().dispatchEvent(new Event('change'));await wait(()=>release);assert(!document.querySelector('[data-referral-figure]'),'old figures hidden during switch');
		 select().value='account-2';select().dispatchEvent(new Event('change'));await wait(()=>document.querySelector('[data-referral-figure]'));release();await new Promise(resolve=>setTimeout(resolve,30));
		 assert(select().value==='account-2'&&Object.values(figures()).every(text=>text===i.t('accounts.referrals.unknown')),'late response cannot borrow another account figures');
		 """)
		self.assertEqual([], _harness.errors())

	def test_invited_by_uses_current_label_removed_id_and_null_is_absent(self):
		self.open()
		self.js("""
		 assert(document.querySelector('[data-account=account-2] [data-invited-by]').textContent===i.t('accounts.invited_by.label',{account:'Jamie'}),'source label resolved');
		 assert(document.querySelector('[data-account=account-3] [data-invited-by]').textContent===i.t('accounts.invited_by.removed',{account:'removed-source'}),'removed source keeps id');
		 assert(!document.querySelector('[data-account=account-1] [data-invited-by]'),'null hidden');
		 assert(!document.querySelector('[data-invited-by] a')&&!document.querySelector('[data-invited-by] [role=status]'),'no link or status claim');
		 const rows=structuredClone(s.get('accounts'));rows[0].label='<New source label>';window.pywebview.api.list_accounts=async()=>({ok:true,data:{accounts:rows}});await remount();
		 assert(document.querySelector('[data-account=account-2] [data-invited-by]').textContent===i.t('accounts.invited_by.label',{account:'<New source label>'})&&!document.querySelector('[data-invited-by] script'),'current escaped local label');
		 """)

	def test_household_cancel_default_explicit_check_and_normal_flow_scope(self):
		self.open('ui0311-household')
		self.js("""
		 const calls=record();document.getElementById('accounts-add-another').click();await wait(()=>document.getElementById('dialog').open);
		 assert(document.activeElement.dataset.dialogAction==='cancel','default Cancel');assert(calls.length===0,'no request before choice');
		 const d=document.getElementById('dialog');assert(d.querySelector('blockquote').textContent===i.t('accounts.household.terms'),'exact Microsoft quotation');assert(d.querySelector('.accounts-household-source').textContent.includes('https://www.microsoft.com/servicesagreement')&&!d.querySelector('a'),'canonical source without a blocked external link');
		 assert(d.querySelector('#dialog-body').textContent===i.t('errors.app.accounts_household_limit',{count:6,limit:6}),'canonical backend warning reused');assert(d.textContent.includes(i.t('accounts.household.source')),'source line');assert(d.querySelector('[data-dialog-action=add]').disabled,'Add disabled');await close('cancel');assert(calls.length===0&&!document.getElementById('accounts-invite-source'),'Cancel ends flow');
		 document.getElementById('accounts-add-another').click();await agree();await wait(()=>document.getElementById('accounts-invite-normal'));document.getElementById('accounts-invite-normal').click();await wait(()=>calls.some(call=>call.name==='add_account'));
		 await wait(()=>document.querySelector('[data-signin-state=starting]'));const add=calls.find(call=>call.name==='add_account');assert(add.request.household_ack===true,'ack add');
		 s.set('signin',{account_id:'account-7',state:'awaiting_confirmation',message_key:'accounts.signin.awaiting_confirmation'});await wait(()=>document.getElementById('dialog').open);await close('confirm');await wait(()=>s.get('accounts').length===7);
		 assert(calls.find(call=>call.name==='confirm_enrollment').request.household_ack===true,'ack confirm');
		 assert(!document.querySelector('[data-account=account-7] [data-invited-by]'),'ordinary admission has no invented inviter');
		 document.getElementById('accounts-add-another').click();await wait(()=>document.getElementById('accounts-household-confirm'));assert(!document.getElementById('accounts-household-confirm').checked,'new flow asks again');await close('cancel');
		 """)
		self.assertEqual([], _harness.errors())

	def test_household_error_at_five_retry_preview_start_and_no_new_flow_ack(self):
		self.open()
		self.js("""
		 const accountList=window.pywebview.api.list_accounts,rows=structuredClone(s.get('accounts'));while(rows.length<5)rows.push({...rows[0],id:'synthetic-'+rows.length,label:'Synthetic '+rows.length});window.pywebview.api.list_accounts=async()=>({ok:true,data:{accounts:rows}});await remount();
		 const calls=record();document.getElementById('accounts-add-another').click();await wait(()=>document.getElementById('accounts-invite-source'));assert(!document.getElementById('dialog').open,'five no popup');
		 const original=window.pywebview.api.preview_invite_account;let first=true;window.pywebview.api.preview_invite_account=async request=>{if(first){first=false;return {ok:false,error:{code:'accounts.household_limit',message_key:'errors.app.accounts_household_limit',params:{count:6,limit:6},detail:null,fields:null}}}return original(request)};
		 const source=document.getElementById('accounts-invite-source');source.value='account-1';source.dispatchEvent(new Event('change'));document.getElementById('accounts-invite-preview').click();await agree();await wait(()=>document.getElementById('accounts-invite-open'));
		 assert(calls.find(call=>call.name==='preview_invite_account').request.household_ack===true,'error retry ack preview');document.getElementById('accounts-invite-open').click();await wait(()=>document.getElementById('accounts-invite-continue'));
		 assert(calls.find(call=>call.name==='start_invite_account').request.household_ack===true,'ack start invite');
		 window.pywebview.api.list_accounts=accountList;s.set('signin',{account_id:'account-4',state:'awaiting_confirmation',message_key:'accounts.signin.awaiting_confirmation'});await wait(()=>document.getElementById('dialog').open);await close('confirm');await wait(()=>document.querySelector('[data-account=account-4] [data-invited-by]'));
		 assert(calls.find(call=>call.name==='confirm_enrollment').request.household_ack===true,'same error-path ack through invited admission');
		 assert(document.querySelector('[data-account=account-4] [data-invited-by]').dataset.invitedBy==='account-1','invited admission retains selected source');
		 window.pywebview.api.list_accounts=async()=>({ok:true,data:{accounts:rows}});await remount();
		 document.getElementById('accounts-add-another').click();await wait(()=>document.getElementById('accounts-invite-normal'));document.getElementById('accounts-invite-normal').click();await wait(()=>calls.some(call=>call.name==='add_account'));
		 assert(!Object.hasOwn(calls.find(call=>call.name==='add_account').request,'household_ack'),'cancelled flow ack does not leak');
		 """)

	def test_household_ack_applies_to_existing_enrollment(self):
		self.open('ui0311-household')
		self.js("""
		 const rows=structuredClone(s.get('accounts'));rows[0].identity_status='unconfirmed';window.pywebview.api.list_accounts=async()=>({ok:true,data:{accounts:rows}});await remount();const calls=record();
		 document.getElementById('accounts-add-another').click();await agree();await wait(()=>calls.some(call=>call.name==='begin_enrollment'));
		 assert(calls.find(call=>call.name==='begin_enrollment').request.household_ack===true&&!calls.some(call=>call.name==='add_account'),'existing enrollment first with same ack');
		 s.set('signin',{account_id:'account-1',state:'awaiting_confirmation',message_key:'accounts.signin.awaiting_confirmation'});await wait(()=>document.getElementById('dialog').open);await close('confirm');await wait(()=>calls.some(call=>call.name==='confirm_enrollment'));
		 assert(calls.find(call=>call.name==='confirm_enrollment').request.household_ack===true,'same acknowledgment through enrollment confirmation');
		 """)

	def test_household_reset_cancels_late_decisions_and_protected_call_retries(self):
		self.open()
		self.js("""
		 const {householdFlow}=await import('./js/screens/accounts/household.js'),components=await import('./js/components/index.js');
		 const methods=['add_account','begin_enrollment','preview_invite_account','start_invite_account','confirm_enrollment'],calls=[];
		 let rows=Array.from({length:5},(_,n)=>({id:'account-'+n}));rows.push({id:'ACCOUNT-0'},{id:'@ROOT'});
		 let handler=async(method,request)=>{calls.push({method,request});return {}};
		 const flow=householdFlow({i18n:i,components,bridge:{call:(method,request)=>handler(method,request)}},()=>rows);
		 assert(await flow.ensure()&&!document.getElementById('dialog').open,'deduplicated five and root excluded');
		 rows.push({id:'account-5'});let decision=flow.ensure();await agree();assert(await decision,'explicit decision accepted');
		 for(const method of methods)await flow.call(method,{synthetic:true});await flow.call('get_referral_info',{account_id:'account-1'});
		 assert(calls.slice(0,5).every(call=>call.request.household_ack===true)&&!Object.hasOwn(calls[5].request,'household_ack'),'every protected request and no unrelated request');
		 flow.reset();decision=flow.ensure();await wait(()=>document.getElementById('accounts-household-confirm'));flow.reset();await agree();assert(!await decision,'reset invalidates open decision');
		 rows=rows.slice(0,5);await flow.call('add_account',{});assert(!Object.hasOwn(calls.at(-1).request,'household_ack'),'reset drops earlier acknowledgement');
		 const failure={code:'accounts.household_limit',params:{count:6,limit:6}};let reject;
		 handler=()=>new Promise((resolve,no)=>{reject=no});const pending=flow.call('add_account',{}).then(()=>null,error=>error);
		 flow.reset();reject(failure);assert((await pending).householdCancelled&&!document.getElementById('dialog').open,'late refusal cannot restart an ended flow');
		 handler=async()=>{throw failure};const retry=flow.call('confirm_enrollment',{}).then(()=>null,error=>error);
		 await wait(()=>document.getElementById('accounts-household-confirm'));await close('cancel');assert((await retry).householdCancelled,'decline never retries the protected effect');
		 """)
		self.assertEqual([], _harness.errors())

	def test_three_state_and_household_captures(self):
		_harness.viewport(860,600)
		try:
			self.open()
			for account, state in (('account-1','read'),('account-2','unknown'),('account-3','stale')):
				self.js("const field=document.getElementById('accounts-referral-account');field.value="+json.dumps(account)+";field.dispatchEvent(new Event('change'));await wait(()=>document.querySelector('.accounts-referrals').getAttribute('aria-busy')==='false');document.querySelector('.accounts-referrals').scrollIntoView({block:'start'});await document.fonts.ready;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));assert(document.documentElement.scrollWidth<=innerWidth+1,'no horizontal overflow')")
				if os.environ.get('RF_0311_CAPTURE'):_harness.driver.save_screenshot(str(Path(os.environ['RF_0311_CAPTURE'])/f'referral-{state}-light-860x600.png'))
			self.open('ui0311-household');self.js("document.getElementById('accounts-add-another').click();await wait(()=>document.getElementById('dialog').open)")
			if os.environ.get('RF_0311_CAPTURE'):_harness.driver.save_screenshot(str(Path(os.environ['RF_0311_CAPTURE'])/'household-light-860x600.png'))
			self.js("await close('cancel')")
		finally:
			_harness.viewport(1280,800)
