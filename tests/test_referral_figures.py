"""Only explicitly labelled numeric referral observations may leave the DOM."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import referrals
from test_referrals import Driver, LINK, AT, RUN


DOM = r"""
const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
let effects=0; const fail=()=>{effects++;throw Error('effect forbidden')};
class Node {
 constructor(tag,text='',attrs={},children=[]){this.tagName=tag.toUpperCase();this.own=text;this.attrs=attrs;this.children=children;this.hidden=false;for(const n of children)n.parentElement=this;}
 get textContent(){return this.own+this.children.map(n=>n.textContent).join(' ')}
 get nextElementSibling(){const a=this.parentElement?.children||[];return a[a.indexOf(this)+1]||null}
 getAttribute(key){return this.attrs[key]??null}
 getClientRects(){return this.attrs.hidden?[]:[{}]}
 contains(n){return n===this||this.children.some(c=>c.contains(n))}
 matches(s){if(s==='[role="heading"]')return this.attrs.role==='heading';if(s==='[aria-label]')return 'aria-label'in this.attrs;return s.toUpperCase()===this.tagName}
 querySelectorAll(selector){return this.children.flatMap(n=>[...(selector.split(',').some(s=>n.matches(s))?[n]:[]),...n.querySelectorAll(selector)])}
 click(){fail()}
}
const input=new Node('input','',{type:'text'});input.value=p.link||'https://rewards.bing.com/refer?code=SYNTHETIC';
const copy=new Node('button','Copy link');const linkBox=new Node('div','',{},[input,copy]);
// The old link reader uses a CSS input query, independent of the figures.
const old=linkBox.querySelectorAll;linkBox.querySelectorAll=function(s){return s.includes('input')?[input]:old.call(this,s)};
const inside=(p.rows||[]).map(row=>row.pair?new Node('div','',{},[new Node('dt',row.label),new Node('dd',row.value)]):new Node(row.tag||'p',row.text||'',row.attrs||{}));
const heading=new Node('h2',p.section||'Your referrals');
const area=new Node('section','',{},[heading,...inside]);
const names=new Node('div','Synthetic Private Person private@example.invalid 12345');
const outside=new Node('p','Friends: 88');
const summary=(p.summary||[]).flatMap(row=>[new Node(row.tag||'p',row.label),new Node(row.valueTag||'p',row.value,row.attrs||{})]);
const otherHeading=new Node('h2',p.secondHeading||'Your friends');
const otherArea=new Node('section','',{},[otherHeading]);
const summaryBox=new Node('div','',{},summary);
const root=new Node('main','',{},[new Node('h1','Refer and earn'),linkBox,area,names,outside,otherArea,summaryBox]);
const rootQuery=root.querySelectorAll.bind(root);root.querySelectorAll=s=>s==='a,button,input[type="button"],input[type="submit"]'?[copy]:rootQuery(s);
global.document={readyState:'complete',querySelector:s=>s==='main'?root:null,querySelectorAll:s=>[copy]};
global.location={assign:fail,replace:fail};
const result=new Function(p.script)();
process.stdout.write(JSON.stringify({result,effects}));
"""


class ReferralFiguresTests(unittest.TestCase):
	def dom(self,rows,**other):
		node=shutil.which('node');self.assertIsNotNone(node)
		packet=dict(rows=rows,script=referrals._SNAPSHOT,**other)
		result=subprocess.run([node,'-e',DOM],input=json.dumps(packet),text=True,encoding='utf-8',capture_output=True,timeout=8,
			env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH')})
		self.assertEqual(result.returncode,0,result.stderr)
		value=json.loads(result.stdout);self.assertEqual(value['effects'],0)
		self.assertNotIn('Synthetic Private',result.stdout);self.assertNotIn('private@example.invalid',result.stdout)
		return value['result']['figures']

	def test_labelled_figures_and_status_are_read_without_names_or_clicks(self):
		figures=self.dom([{'text':'Referrals: 3'},{'text':'Friends joined: 2'},{'pair':True,'label':'Pending points','value':'1,200'},
			{'text':'Earned referral points: 500'},{'text':'Status: pending'}])
		self.assertEqual(figures,dict(referrals=3,friends=2,pending_points=1200,earned_points=500,referral_status='pending'))
		self.assertFalse(referrals.SELECTORS_VERIFIED)
		self.assertEqual(referrals.FIGURE_SELECTORS_VERIFIED,{"referrals":True,"friends":False,
			"pending_points":True,"earned_points":True,"referral_status":False})

	def test_absent_unknown_ambiguous_or_unlabelled_numbers_never_become_zero(self):
		for rows,section in (([], 'Your referrals'),([{'text':'Unlabelled summary'},{'text':'12345'}],'Your friends'),([{'text':'Friends: 3'}],'Unverified section'),
			([{'text':'Friends: 3'},{'text':'Friends: 4'}],'Your friends'),([{'text':'Friends: -1'},{'text':'Points: 99'}],'Your referrals'),
			([{'text':'Freunde: 3'}],'Your friends'),([{'text':'Pending points: 20 private@example.invalid'}],'Your referrals')):
			with self.subTest(rows=rows,section=section):self.assertEqual(self.dom(rows,section=section),{})

	def test_explicit_zero_is_known_and_hidden_numbers_are_not_read(self):
		self.assertEqual(self.dom([{'text':'Friends: 0'},{'text':'Referrals: 0'},{'text':'Pending points: 0'},{'text':'Earned points: 0'}]),
			dict(friends=0,referrals=0,pending_points=0,earned_points=0))
		self.assertEqual(self.dom([{'text':'Friends: 9','attrs':{'hidden':True}}]),{})

	def test_number_immediately_under_recognised_heading_is_a_labelled_figure(self):
		self.assertEqual(self.dom([{'tag':'p','text':'3'}],section='Your referrals'),{'referrals':3})
		self.assertEqual(self.dom([{'tag':'p','text':'0'}],section='Your friends'),{'friends':0})

	def test_unrelated_aria_label_needs_an_explicit_referral_meaning(self):
		self.assertEqual(self.dom([{'text':'','attrs':{'aria-label':'Pending points: 4'}}],section='Other'),{})
		self.assertEqual(self.dom([{'text':'','attrs':{'aria-label':'Pending referral points: 4'}}],section='Other'),{'pending_points':4})

	def test_observed_referral_summary_paragraph_pairs_are_explicit_figures(self):
		# Synthetic values only. The real-page probe observed these exact label
		# codes and P/P adjacency; its private digits never enter this fixture.
		self.assertEqual(self.dom([],summary=[{'label':'Referrals','value':'2'},
			{'label':'Earned points','value':'100'},{'label':'Pending points','value':'0'}]),
			dict(referrals=2,earned_points=100,pending_points=0))

	def test_summary_pair_needs_referral_context_exact_label_and_numeric_value(self):
		for summary,other in (([{'label':'Pending points','value':'3'}],{'secondHeading':'Other'}),
			([{'label':'Balance','value':'100'}],{}),([{'label':'Referrals','value':'Synthetic Private Person'}],{}),
			([{'label':'Earned points','value':'3','valueTag':'div'}],{}),
			([{'label':'Earned points','value':'3','attrs':{'hidden':True}}],{})):
			with self.subTest(summary=summary,other=other):self.assertEqual(self.dom([],summary=summary,**other),{})

	def test_later_event_without_matching_storage_makes_existing_numbers_stale(self):
		import run_record
		with tempfile.TemporaryDirectory() as folder:
			referrals.save_referral_state('one',referrals.ReferralState(LINK,True,referrals=2),read_at=AT,run_id=RUN,directory=folder)
			new_run='20261026T233000Z-0001'
			run_record.append_event(str(Path(folder)/'runs'/(new_run+'.jsonl')),'referral_state',new_run,account='one',read_at='2026-10-26T23:30:00Z',link_found=False,unreadable='storage_failed')
			value=referrals.get_referral_info('one',directory=folder)
			self.assertEqual((value['referrals'],value['figures_read_at'],value['figures_state']),(2,AT,'stale'))

	def test_link_absence_does_not_discard_an_independently_labelled_figure(self):
		state=referrals.read_referral_page(Driver(raw={'error':'link_missing','reason':'input_missing','figures':{'friends':0}}))
		self.assertEqual(state.friends,0);self.assertEqual(state.unreadable,'link_missing')
		with tempfile.TemporaryDirectory() as folder:
			referrals.save_referral_state('one',state,read_at=AT,run_id=RUN,directory=folder)
			value=referrals.get_referral_info('one',directory=folder)
			self.assertEqual((value['friends'],value['figures_state'],value['figures_read_at']),(0,'read',AT))
			self.assertIsNone(value['link'])

	def test_numeric_boundary_drops_strings_booleans_lists_and_extra_private_fields(self):
		raw={'link':LINK,'figures':{'referrals':True,'friends':['Private Friend'],'pending_points':'5','earned_points':7,
			'referral_status':'Private Name','email':'private@example.invalid'}}
		state=referrals.read_referral_page(Driver(raw=raw))
		self.assertEqual(state.earned_points,7)
		self.assertIsNone(state.referrals);self.assertIsNone(state.friends);self.assertIsNone(state.pending_points)
		with tempfile.TemporaryDirectory() as folder:
			referrals.save_referral_state('one',state,read_at=AT,run_id=RUN,directory=folder)
			text=(Path(folder)/'referral.json').read_text()
			for private in ('Private','private@example.invalid','email'):self.assertNotIn(private,text)

	def test_account_local_snapshot_has_own_time_and_survives_unknown_failure_as_stale(self):
		with tempfile.TemporaryDirectory() as folder:
			state=referrals.ReferralState(LINK,True,referrals=0,friends=2,pending_points=100,earned_points=50)
			referrals.save_referral_state('one',state,read_at=AT,run_id=RUN,directory=folder)
			info=referrals.get_referral_info('one',directory=folder)
			self.assertEqual((info['figures_state'],info['figures_read_at'],info['referrals'],info['friends']),('read',AT,0,2))
			later='2026-10-26T23:00:00Z'
			referrals.save_referral_state('one',referrals.ReferralState(LINK,True),read_at=later,run_id='later',directory=folder)
			info=referrals.get_referral_info('one',directory=folder)
			self.assertEqual((info['figures_state'],info['figures_read_at'],info['read_at']),('stale',AT,later))
			self.assertEqual(info['pending_points'],100)
			referrals.save_referral_state('one',referrals.ReferralState(unreadable='read_failed'),read_at='2026-10-27T23:00:00Z',run_id='failure',directory=folder)
			self.assertEqual(referrals.get_referral_info('one',directory=folder)['friends'],2)
			unknown=referrals.get_referral_info('two',directory=folder)
			self.assertEqual(unknown['figures_state'],'unknown');self.assertIsNone(unknown['figures_read_at'])

	def test_new_observation_is_one_snapshot_never_mixed_with_old_missing_fields(self):
		with tempfile.TemporaryDirectory() as folder:
			referrals.save_referral_state('one',referrals.ReferralState(LINK,True,referrals=3,friends=4),read_at=AT,run_id=RUN,directory=folder)
			referrals.save_referral_state('one',referrals.ReferralState(LINK,True,friends=0),read_at='2026-10-26T23:00:00Z',run_id='later',directory=folder)
			info=referrals.get_referral_info('one',directory=folder)
			self.assertEqual(info['friends'],0);self.assertIsNone(info['referrals']);self.assertEqual(info['figures_state'],'read')
