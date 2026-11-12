"""Disposable synthetic home for overview profiling, never operator state."""
import _bootstrap  # noqa: F401

from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import cProfile
import io
import json
import os
import pstats
import statistics
import subprocess
import tempfile
import time
import types
from unittest.mock import patch

import rfapp
from rfapp import bridge, edge_streak, prefs
import run_record

NOW = datetime(2026,11,12,22,30,tzinfo=timezone.utc)


def record(home, at, index, *, capture=False):
	run_id = at.strftime("%Y%m%dT%H%M%SZ") + f"-{index:04x}"
	base = {"v":run_record.SCHEMA_VERSION,"run":run_id}
	def event(kind, seconds=0, **fields):
		return dict(base,type=kind,ts=(at + timedelta(seconds=seconds)).isoformat(),**fields)
	accounts = ["account-1","account-2"]
	events = [event("run_start",trigger="scheduled",accounts=accounts,plan={"edge_topup":False}),
		event("edge_streak_plan",slot=at.isoformat(),enabled=False,maximum_minutes=0)]
	for offset, account in enumerate(accounts):
		balance = 10000 + index * 20 + offset * 1000
		events.extend([event("account_start",account=account,profile=str(home / "profiles" / account)),
			event("state",1,account=account,phase="before",balance=balance,earned_today=0,
				searches_earned=0,searches_max=90,daily_set_open=1,cards_open=2,quests_open=1,open_points=60,
				unreadable=None,edge_tile=True,edge_minutes=4,edge_target=30),
			event("decision",2,account=account,should_run=True,reason="open_points",min_points=1,open_points=60)])
		for task in ("daily_set","misc_cards","quests","searches"):
			events.extend([event("task_start",3,account=account,task=task,points_before=balance),
				event("task_end",4,account=account,task=task,outcome="ok",points_before=balance,points_after=balance+5,duration_ms=1000)])
		# Repeated search receipts match the record shape without storing queries.
		for tick in range(15): events.append(event("search_points",5,account=account,earned=tick,max=90))
		events.append(event("state",10,account=account,phase="after",balance=balance+20,earned_today=20,
			searches_earned=90,searches_max=90,daily_set_open=0,cards_open=0,quests_open=0,open_points=0,
			unreadable=None,edge_tile=True,edge_minutes=12,edge_target=30))
		if capture:
			events.append(event("streak_capture",11,account=account,status="ok",phase="after",sections=[{"id":"streaks","text":"Bing Search Streak Day 2 of 7 streak completed. Complete the next day to earn 100 points. +10 100 Search: 1/1","nodes":[]}]))
		events.append(event("account_end",12,account=account,error_code=None,ran_tasks=True,duration_ms=12000))
	events.append(event("run_end",13,result="ok",exit_code=0,duration_ms=13000))
	path = home / "runs" / (run_id + ".jsonl")
	path.write_text("\n".join(map(json.dumps,events)) + "\n",encoding="utf-8")
	path.with_suffix(".log").write_text("synthetic run\n",encoding="utf-8")
	return run_id


def build_home(home, count=100):
	(home / "runs").mkdir()
	(home / "profiles").mkdir()
	(home / ".env").write_text("QUERY_SOURCE=trends\nREWARDS_ACCOUNTS=account-1,account-2\nREWARDS_EDGE_STREAK=remind\nUSER_DATA_DIR=" + str(home / "profiles") + "\n",encoding="utf-8")
	value = {**prefs._DEFAULTS,"account_labels":{"account-1":"Synthetic one","account-2":"Synthetic two"},
		"history_aliases":[{"from":"@root","to":"account-1","before":"2026-01-01T00:00:00Z"}],
		"goals":{"account-1":15000,"account-2":16000},"language":"en"}
	(home / "app-prefs.json").write_text(json.dumps(value),encoding="utf-8")
	start = NOW - timedelta(days=25)
	ids = [record(home,start + timedelta(hours=6 * index),index,capture=index==0) for index in range(count)]
	return ids


@contextmanager
def synthetic(count=100):
	with tempfile.TemporaryDirectory(prefix="overview-profile-") as folder, ExitStack() as stack:
		home = Path(folder)
		ids = build_home(home,count)
		stack.enter_context(patch.dict(os.environ,{"REWARDS_FARMER_DATA_HOME":str(home)}))
		stack.enter_context(patch("rfapp.autostart.is_enabled",return_value=False))
		stack.enter_context(patch("rfapp.schedule.status",return_value={"in_sync":True,"error_code":None}))
		stack.enter_context(patch("run_lock.status",return_value=None))
		stack.enter_context(patch("rfapp.bridge._utc",return_value=NOW.isoformat()))
		class FixedDateTime(datetime):
			@classmethod
			def now(cls,tz=None): return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)
		stack.enter_context(patch("rfapp.bridge.datetime",FixedDateTime))
		api = bridge.Bridge()
		stack.callback(api._close)
		yield home,ids,api


def measure(api):
	# Imports and catalogs are outside the ten measured steady polling calls.
	first = api.get_overview({})
	assert first["ok"], first
	measurements = []
	for _ in range(10):
		start = time.perf_counter()
		value = api.get_overview({})
		measurements.append((time.perf_counter()-start)*1000)
		assert value == first
	return first,{"median_ms":statistics.median(measurements),"samples_ms":measurements,"equality_10_calls":True}


@contextmanager
def reference(api, commit):
	# Manual comparison only; shallow CI never needs the historical commit.
	modules = {}
	root = Path(__file__).resolve().parents[2]
	for name in ("streaks", "edge_streak", "runs"):
		text = subprocess.run(["git","--no-pager","show",commit + ":app/rfapp/" + name + ".py"],
			cwd=root,stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding="utf-8",timeout=15,check=True).stdout
		module = types.ModuleType("overview_reference_" + name)
		module.__file__ = str(root / "app/rfapp" / (name + ".py"))
		exec(compile(text,commit + ":" + name,"exec"),module.__dict__)
		modules[name] = module
	with patch.object(rfapp,"edge_streak",modules["edge_streak"]), patch.object(modules["runs"],"streaks",modules["streaks"]), patch.dict(api._dependencies,{"runs":modules["runs"]}):
		yield


def benchmark(commit=None):
	with synthetic() as (home,ids,api):
		result = {"synthetic_runs":len(ids),"record_files":len(ids)*2}
		if commit:
			with reference(api,commit): baseline,result["before"] = measure(api)
		first,result["after"] = measure(api)
		if commit:
			assert first == baseline
			# Account-specific detail/chart/streak branches retain exact output too.
			for account in ("account-1","account-2"):
				with reference(api,commit): before = api.get_overview({"account_id":account})
				assert api.get_overview({"account_id":account}) == before
			result["before_commit"] = commit
			result["complete_bridge_equality_all_and_each_account"] = True
		profile = cProfile.Profile()
		with profile: value = api.get_overview({})
		assert value == first
		output = io.StringIO()
		pstats.Stats(profile,stream=output).sort_stats("cumulative").print_stats(30)
		print("OVERVIEW_BENCH=" + json.dumps(result,sort_keys=True))
		print(output.getvalue())


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--compare",help="Optional local historical commit, e.g. 18be5fa")
	benchmark(parser.parse_args().compare)
