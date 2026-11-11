import _bootstrap  # noqa: F401

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rfapp import bridge, paths, runs
import run_record
import test_runs as fixtures


class EdgeBrowsingProjectionTests(unittest.TestCase):
	setUp = fixtures.RunReductionTests.setUp
	fixture = fixtures.RunReductionTests.fixture

	def packet(self, **changes):
		return {"minutes_before":4, "minutes_after":12, "minutes_target":30, "activated":True,
			"maximum_reached":False, "final_read_success":True, "outcome":"incomplete", "duration_ms":480000, **changes}

	def add_event(self, run_id, account="one", packet=None, **extra):
		run_record.append_event(str(self.directory / (run_id + ".jsonl")), "edge_browsing", run_id,
			account=account, **(self.packet() if packet is None else packet), **extra)

	def api(self):
		api = bridge.Bridge(dependencies={"runs":runs,
			"prefs":SimpleNamespace(load=lambda:{"account_labels":{}, "history_aliases":[]}),
			"settings":SimpleNamespace(user_data_dir=lambda:"profiles")})
		self.addCleanup(api._close)
		# Keep redaction reads in the same disposable test home as the records.
		self.stack.enter_context(patch.object(paths,"env_path",return_value=self.directory / ".env"))
		return api

	def test_detail_and_history_project_exact_value_free_packet_per_account(self):
		run_id = self.fixture("multi")
		before = runs.load_run(run_id,{},"profiles")
		self.add_event(run_id, detail="SYNTHETIC_SECRET", url="https://invalid.test/private", maximum_minutes=35, elapsed_seconds=480)
		detail = runs.load_run(run_id,{},"profiles")
		self.assertEqual(detail["accounts"][0]["edge_browsing"],self.packet())
		self.assertIsNone(detail["accounts"][1]["edge_browsing"])
		without_added = copy.deepcopy(detail)
		for account in without_added["accounts"]: account.pop("edge_browsing")
		for account in before["accounts"]: account.pop("edge_browsing",None)
		self.assertEqual(without_added,before)
		rows = runs.history(labels={},user_data_dir="profiles")["runs"]
		self.assertEqual(rows[0]["accounts"][0]["edge_browsing"],self.packet())
		self.assertIsNone(rows[0]["accounts"][1]["edge_browsing"])
		self.assertNotIn("tasks",rows[0]["accounts"][0])
		self.assertNotIn("SYNTHETIC_SECRET",json.dumps(detail))

	def test_bridge_get_run_and_get_live_use_same_projection_without_passthrough_change(self):
		run_id = self.fixture("multi")
		self.add_event(run_id)
		self.active.return_value = run_id
		api = self.api()
		for response in (api.get_run({"run_id":run_id}), api.get_live({})):
			self.assertTrue(response["ok"],response)
			value = response["data"].get("run",response["data"])
			self.assertEqual(value["accounts"][0]["edge_browsing"],self.packet())
			self.assertIsNone(value["accounts"][1]["edge_browsing"])
		row = api.get_history({})["data"]["runs"][0]["accounts"][0]
		self.assertEqual(row["edge_browsing"],self.packet())

	def test_absent_event_has_explicit_null_on_every_existing_account(self):
		run_id = self.fixture("multi")
		for account in runs.load_run(run_id,{},"profiles")["accounts"]:
			self.assertIn("edge_browsing",account)
			self.assertIsNone(account["edge_browsing"])
		for account in runs.history(labels={},user_data_dir="profiles")["runs"][0]["accounts"]:
			self.assertIsNone(account["edge_browsing"])

	def test_all_outcomes_and_nullable_minutes_are_preserved_without_inference(self):
		run_id = self.fixture("multi")
		for outcome in ("complete","incomplete","not_available","stopped","error"):
			with self.subTest(outcome=outcome):
				value = self.packet(minutes_before=None,minutes_after=None,minutes_target=None,activated=False,
					maximum_reached=False,final_read_success=False,outcome=outcome,duration_ms=0)
				self.add_event(run_id,packet=value)
				self.assertEqual(runs.load_run(run_id,{},"profiles")["accounts"][0]["edge_browsing"],value)

	def test_malformed_latest_event_is_null_not_stale_or_an_exception(self):
		run_id = self.fixture("multi")
		api = self.api()
		self.active.return_value = run_id
		bad = [(key,value) for key in ("minutes_before","minutes_after","minutes_target")
			for value in (True,"4",4.0,-1,[],{})]
		bad += [(key,value) for key in ("activated","maximum_reached","final_read_success") for value in (None,0,1,"true",[],{})]
		bad += [("outcome",value) for value in (None,"unknown",4,[],{})]
		bad += [("duration_ms",value) for value in (None,True,1.5,-1,"3",[],{})]
		for key,value in bad:
			with self.subTest(key=key,value=value):
				self.add_event(run_id)
				self.add_event(run_id,packet=self.packet(**{key:value}))
				response = api.get_live({})
				self.assertTrue(response["ok"],response)
				self.assertIsNone(response["data"]["run"]["accounts"][0]["edge_browsing"])
		for key in self.packet():
			with self.subTest(missing=key):
				value = self.packet()
				del value[key]
				self.add_event(run_id,packet=value)
				self.assertIsNone(runs.load_run(run_id,{},"profiles")["accounts"][0]["edge_browsing"])

	def test_account_filter_does_not_mix_edge_measurements(self):
		run_id = self.fixture("multi")
		self.add_event(run_id)
		other = self.packet(minutes_before=10,minutes_after=30,outcome="complete")
		self.add_event(run_id,"two",other)
		api = self.api()
		for account,packet in (("one",self.packet()),("two",other)):
			response = api.get_run({"run_id":run_id,"account_id":account})
			self.assertTrue(response["ok"],response)
			self.assertEqual(len(response["data"]["accounts"]),1)
			self.assertEqual(response["data"]["accounts"][0]["edge_browsing"],packet)

	def test_wrong_or_missing_event_version_is_null_and_a_later_valid_receipt_recovers(self):
		run_id = self.fixture("multi")
		path = self.directory / (run_id + ".jsonl")
		for version in (None,True,0,"1"):
			with self.subTest(version=version):
				self.add_event(run_id)
				event = {"type":"edge_browsing","account":"one","run":run_id,**self.packet()}
				if version is not None: event["v"] = version
				with path.open("a",encoding="utf-8") as stream: stream.write(json.dumps(event) + "\n")
				self.assertIsNone(runs.load_run(run_id,{},"profiles")["accounts"][0]["edge_browsing"])
		self.add_event(run_id,packet=self.packet(outcome="complete",minutes_after=30))
		self.assertEqual(runs.load_run(run_id,{},"profiles")["accounts"][0]["edge_browsing"],self.packet(outcome="complete",minutes_after=30))

	def test_live_tail_push_changes_from_null_to_packet_to_malformed_null(self):
		run_id = self.fixture("interrupted")
		self.active.return_value = run_id
		pushes = []
		tail = runs.LiveTail(lambda topic,data:pushes.append((topic,data)),{},"profiles")
		tail.poll()
		self.assertIsNone(pushes[0][1]["accounts"][0]["edge_browsing"])
		pushes.clear()
		self.add_event(run_id,"default")
		tail.poll()
		self.assertEqual([topic for topic,_ in pushes],["run.snapshot"])
		self.assertEqual(pushes[0][1]["accounts"][0]["edge_browsing"],self.packet())
		pushes.clear()
		self.add_event(run_id,"default",self.packet(duration_ms="invalid"))
		tail.poll()
		self.assertEqual([topic for topic,_ in pushes],["run.snapshot"])
		self.assertIsNone(pushes[0][1]["accounts"][0]["edge_browsing"])
