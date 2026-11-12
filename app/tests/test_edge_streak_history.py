import _bootstrap  # noqa: F401

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from rfapp import edge_streak, settings
import run_record
import test_settings as fixtures


class EdgeDailyHistoryTests(unittest.TestCase):
	setUp = fixtures.SettingsTests.setUp

	def record(self, at, *, account="account-1", tile=True, minutes=4, target=30,
		finished=True, result="checked", state_at=None, end_at=None):
		run_id = at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-aaaa"
		def event(kind, stamp=None, **fields):
			return {"v":run_record.SCHEMA_VERSION,"run":run_id,"ts":(stamp or at).isoformat(),"type":kind,**fields}
		events = [event("run_start",trigger="scheduled",accounts=[account],plan={}),
			event("edge_streak_plan",slot=at.isoformat(),enabled=False,maximum_minutes=0),
			event("account_start",account=account,profile=str(self.root / "profiles" / account)),
			event("state",state_at or at + timedelta(seconds=10),account=account,phase="after",
				edge_tile=tile,edge_minutes=minutes,edge_target=target),
			event("account_end",account=account,error_code=None)]
		if finished: events.append(event("run_end",end_at or at + timedelta(seconds=20),result=result,exit_code=0))
		folder = self.root / "runs"
		folder.mkdir(exist_ok=True)
		(folder / (run_id + ".jsonl")).write_text("\n".join(map(json.dumps,events)) + "\n",encoding="utf-8")
		return run_id, events

	def clock(self, at):
		class FrozenDateTime(datetime):
			@classmethod
			def now(cls, tz=None):
				return at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)
		return patch.object(edge_streak,"datetime",FrozenDateTime)

	def view(self, at, accounts=("account-1",)):
		with self.clock(at):
			return edge_streak.snapshot({"run.edge_streak":"remind"},accounts,user_data_dir=str(self.root / "profiles"))

	def reads(self, reader):
		return [Path(call.args[0]).stem for call in reader.call_args_list]

	def old_history(self, today, count=400):
		for offset in range(1,count + 1): self.record(today - timedelta(days=offset))

	def test_hundreds_of_old_records_add_no_reads_or_projections_and_shape_is_unchanged(self):
		now = datetime(2026,11,12,23,30).astimezone()
		self.old_history(now)
		older_id, _ = self.record(now.replace(hour=9,minute=45),account="account-2",minutes=8)
		latest_id, latest = self.record(now.replace(hour=21,minute=45),minutes=12)
		with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader, patch.object(edge_streak.account_history,"project_events",wraps=edge_streak.account_history.project_events) as project:
			view = self.view(now,("account-1","account-2","account-3"))
		self.assertEqual(self.reads(reader),[latest_id,older_id])
		self.assertEqual(project.call_count,2)
		self.assertEqual(view["mode"],"remind")
		self.assertIsNone(view["auto_off"])
		self.assertEqual(view["accounts"][0],{"account_id":"account-1","edge_tile":True,"edge_minutes":12,"edge_target":30,
			"at":latest[3]["ts"],"run_id":latest_id,"last_scheduled_at":latest[-1]["ts"],"last_scheduled_run_id":latest_id})
		self.assertEqual(view["accounts"][1]["edge_minutes"],8)
		self.assertEqual(view["accounts"][2],self.unknown("account-3"))

	def unknown(self, account="account-1"):
		return {"account_id":account,"edge_tile":None,"edge_minutes":None,"edge_target":None,"at":None,
			"run_id":None,"last_scheduled_at":None,"last_scheduled_run_id":None}

	def test_yesterdays_tile_is_null_without_opening_yesterdays_record(self):
		now = datetime(2026,11,12,0,10).astimezone()
		self.record(now - timedelta(minutes=20))
		with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
			self.assertEqual(self.view(now),{"mode":"remind","auto_off":None,"accounts":[self.unknown()]})
		reader.assert_not_called()

	def test_newest_first_walk_stops_at_local_midnight_before_reading_boundary_record(self):
		now = datetime(2026,11,12,0,10).astimezone()
		latest, _ = self.record(now - timedelta(minutes=2),minutes=12)
		early, _ = self.record(now - timedelta(minutes=5),minutes=6)
		yesterday, _ = self.record(now - timedelta(minutes=20))
		def newest_first():
			yield latest
			yield early
			yield yesterday
			raise AssertionError("Walk continued beyond local-day boundary")
		with patch.object(run_record,"list_run_ids",return_value=newest_first()), patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
			view = self.view(now)
		self.assertEqual(self.reads(reader),[latest,early])
		self.assertEqual(view["accounts"][0]["edge_minutes"],12)
		self.assertEqual(view["accounts"][0]["last_scheduled_run_id"],latest)

	def test_evening_fill_history_has_same_day_read_bound(self):
		slot = datetime(2026,11,12,21,45).astimezone()
		self.old_history(slot)
		latest, _ = self.record(slot - timedelta(hours=4))
		with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
			self.assertEqual(edge_streak.open_accounts(["account-1"],slot,user_data_dir=self.root / "profiles"),{"account-1"})
		self.assertEqual(self.reads(reader),[latest])

	def test_snapshot_does_not_take_yesterday_or_future_event_times_from_today_record(self):
		now = datetime(2026,11,12,12).astimezone()
		self.record(now.replace(hour=9),state_at=now - timedelta(days=1),end_at=now - timedelta(days=1))
		self.record(now.replace(hour=10),state_at=now + timedelta(days=1),end_at=now + timedelta(days=1))
		self.assertEqual(self.view(now)["accounts"],[self.unknown()])

	def test_future_run_id_is_skipped_and_unfinished_today_keeps_last_finished_today(self):
		now = datetime(2026,11,12,22).astimezone()
		future, _ = self.record(now + timedelta(days=1))
		active, _ = self.record(now - timedelta(minutes=5),finished=False,minutes=20)
		finished, _ = self.record(now - timedelta(hours=4),minutes=12)
		with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
			row = self.view(now)["accounts"][0]
		self.assertEqual(self.reads(reader),[active,finished])
		self.assertNotIn(future,self.reads(reader))
		self.assertEqual(row["edge_minutes"],20)
		self.assertEqual(row["last_scheduled_run_id"],finished)

	def test_auto_off_uses_supplied_events_and_state_without_reading_history(self):
		now = datetime(2026,11,12,22).astimezone()
		self.old_history(now,20)
		run_id, events = self.record(now - timedelta(hours=1),result="partial")
		events[0]["plan"]["edge_topup"] = True
		events[1].update(enabled=True,maximum_minutes=35)
		events.insert(-2,{"v":run_record.SCHEMA_VERSION,"run":run_id,"ts":now.isoformat(),"type":"edge_browsing",
			"account":"account-1","minutes_before":4,"minutes_after":4,"minutes_target":30,"final_read_success":True,
			"outcome":"incomplete","maximum_reached":True,"maximum_minutes":35,"elapsed_seconds":2100})
		with patch.object(run_record,"list_run_ids",side_effect=AssertionError("Auto-off scanned history")), patch.object(run_record,"read_events",side_effect=AssertionError("Auto-off opened history")):
			state = edge_streak.process_run(run_id,events,user_data_dir=str(self.root / "profiles"))
		self.assertEqual(state["accounts"]["account-1"]["counted_dates"],[now.date().isoformat()])

	def test_fill_stops_on_western_local_midnight_not_raw_utc_date(self):
		slot = datetime(2026,11,12,21,45,tzinfo=timezone(timedelta(hours=-4)))
		latest, _ = self.record(slot - timedelta(hours=4))
		# This record shares the UTC date of local midnight, but is yesterday locally.
		self.record(datetime(2026,11,12,1,0,tzinfo=timezone.utc))
		with patch.object(run_record,"read_events",wraps=run_record.read_events) as reader:
			self.assertEqual(edge_streak.open_accounts(["account-1"],slot,user_data_dir=self.root / "profiles"),{"account-1"})
		self.assertEqual(self.reads(reader),[latest])
