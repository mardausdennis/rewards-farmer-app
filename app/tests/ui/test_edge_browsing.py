import _bootstrap  # noqa: F401

import json
import re
import shutil
import subprocess
import unittest

from ui.harness import UI_DIR
from rfapp import runs


class EdgeBrowsingContractTests(unittest.TestCase):
	def test_mock_receipts_match_real_projection_and_run_consumers(self):
		fixture = json.loads((UI_DIR / 'mock/edge/results.json').read_text(encoding='utf-8'))
		self.assertIs(fixture['synthetic'], True)
		for value in fixture['edge_results'].values():
			if value is not None:
				self.assertEqual(value, runs._edge_browsing({'v': 1, **value}))
		node = shutil.which('node')
		self.assertIsNotNone(node)
		program = r"""
		 import fs from 'node:fs/promises';import {pathToFileURL} from 'node:url';
		 const root=process.argv[1];globalThis.window={location:{search:'?mock=edge/results'},addEventListener(){},dispatchEvent(){}};
		 globalThis.fetch=async url=>({ok:true,json:async()=>JSON.parse(await fs.readFile(root+'/mock/'+String(url).split('/mock/')[1],'utf8'))});
		 const mock=await import(pathToFileURL(root+'/js/mock-bridge.js'));await mock.install();
		 const result={};for(const method of ['get_run','get_live','get_history'])result[method]=await window.pywebview.api[method]({run_id:'synthetic-multi-identity'});
		 console.log(JSON.stringify(result));
		 """
		result = subprocess.run([node, '--input-type=module', '-e', program, str(UI_DIR)], capture_output=True, text=True, timeout=20, check=True)
		responses = json.loads(result.stdout)
		for method, response in responses.items():
			self.assertIs(response['ok'], True)
			data = response['data']
			run = data['run'] if method == 'get_live' else data['runs'][0] if method == 'get_history' else data
			self.assertEqual({'account-1', 'account-2'}, {account['account_id'] for account in run['accounts']})
			for account in run['accounts']:
				self.assertEqual(fixture['edge_results'][account['account_id']], account['edge_browsing'])

	def test_shared_line_consumers_and_english_keys_are_live(self):
		for screen in ('history', 'live'):
			text = (UI_DIR / f'js/screens/{screen}.js').read_text(encoding='utf-8')
			self.assertIn('import { edgeBrowsingLine } from "../edge-browsing.js";', text)
			self.assertIn('edgeBrowsingLine(i18n, account)', text)
		catalog = json.loads((UI_DIR / 'i18n/en/common.json').read_text(encoding='utf-8'))['messages']
		for outcome in ('complete', 'incomplete', 'not_available', 'stopped', 'error'):
			self.assertTrue(catalog[f'common.edge_browsing.outcome.{outcome}']['text'])
		self.assertEqual({'before', 'after', 'target', 'outcome'}, set(re.findall(r'\{(\w+)\}', catalog['common.edge_browsing.result']['text'])))
