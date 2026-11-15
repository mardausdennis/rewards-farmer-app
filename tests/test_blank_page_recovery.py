"""A blank mid-run Rewards document is recovered once, never an activity retry."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
import element_selectors
import page_structure
import rewards_state
import rewards_tasks
import run_record
from fakes import FakeElement
from test_task_balances import Clock, HeaderDriver
from test_reader_header import figure, javascript, node, tree


class BlankDriver(HeaderDriver):
	def __init__(self, clock, *, recover=True):
		super().__init__(clock)
		self.pages["main"]=rewards_state.EARN_URL
		self.blank=False
		self.recover=recover
		self.panel=False
		self.search_earned=6
		self.actions=[]
		self.snapshot_error=False
		self.loading=False
		self.signin=False
		self.content_without_header=False

	def get(self,url):
		super().get(url)
		if url==rewards_state.EARN_URL and self.recover:
			self.blank=False

	def execute_script(self,script,*args):
		if script==page_structure.SNAPSHOT:
			if self.snapshot_error: raise ValueError("synthetic snapshot unavailable")
			dom=node("document",children=[node("body")]) if self.blank else tree([figure()])
			if self.signin: dom=node("document",children=[node("body",children=[node("input",attrs={"type":"password"})])])
			if self.content_without_header: dom=tree([],earn=True,section="streaks")
			return javascript(script,dom,url=self.current_url,ready_state="loading" if self.loading else "complete")
		return super().execute_script(script,*args)

	def find_element(self,by,selector):
		found=self.find_elements(by,selector)
		if not found: raise NoSuchElementException("synthetic empty Rewards document")
		return found[0]

	def find_elements(self,by,selector):
		if self.blank or self.signin:
			return []
		if (by,selector)==(By.CSS_SELECTOR,'[id$="-tab-/earn"]'):
			return [FakeElement(text="Earn")]
		if (by,selector)==(By.TAG_NAME,"button"):
			return [FakeElement(text="Points breakdown")]
		if (by,selector)==(By.TAG_NAME,"section") and self.panel:
			return [FakeElement(text=f"Bing search\n{self.search_earned}/60",attributes={"id":"react-aria-panel"},children={(By.TAG_NAME,"button"):[FakeElement(text="Close")]})]
		return super().find_elements(by,selector)


def tasks_for(driver):
	tasks=rewards_tasks.RewardsTaskUtils.__new__(rewards_tasks.RewardsTaskUtils)
	tasks.driver=driver
	tasks.main_window="main"
	tasks.elements=element_selectors.ElementSelectionUtils(driver)
	tasks.balance_reader=lambda:rewards_state.read_balance(driver,tasks.elements)
	tasks.tab_utils=types.SimpleNamespace(ensure_focus=lambda:None,close_all_other_tabs=lambda **kwargs:None)
	def click(element):
		driver.actions.append(element.text)
		if element.text=="Points breakdown": driver.panel=True
		elif element.text=="Close": driver.panel=False
	tasks.mouse=types.SimpleNamespace(move_to_element=lambda element:setattr(tasks,"target",element),human_like_click=lambda:click(tasks.target))
	tasks.progress=None
	tasks._balance_remaining=rewards_tasks.BALANCE_RUN_BUDGET
	tasks._balance_timed_out=False
	return tasks


class BlankRecoveryTests(unittest.TestCase):
	def setUp(self):
		self.clock=Clock()
		self.driver=BlankDriver(self.clock)
		self.tasks=tasks_for(self.driver)

	def read(self):
		return self.tasks._read_balance(prepare=True,clock=self.clock,sleep=self.clock.sleep)

	def test_blank_reloads_once_and_real_quota_read_can_reach_searches(self):
		self.driver.blank=True
		self.assertEqual(self.read(),1000)
		self.assertEqual([url for url,_ in self.driver.navigations],[rewards_state.EARN_URL])
		self.assertEqual(self.tasks._balance_read_diagnostic["reloaded"],True)
		self.assertEqual(self.tasks._balance_read_diagnostic["status"],"readable")
		self.assertEqual(self.tasks.read_search_points(),(6,60))
		self.assertEqual(self.driver.actions,["Earn","Points breakdown","Close"])
		self.assertEqual(self.driver.timeouts.page_load,60)

	def test_still_blank_has_same_failure_no_repeat_after_timeout(self):
		self.driver.blank=True
		self.driver.recover=False
		self.assertIsNone(self.read())
		self.assertTrue(self.tasks._balance_read_diagnostic["reloaded"])
		self.assertEqual(self.tasks._balance_read_diagnostic["status"],"unreadable")
		with self.assertRaises(NoSuchElementException): self.tasks.complete_required_searches()
		self.assertEqual(self.tasks.progress,"sent 0 searches")
		self.assertIsNone(self.read())
		self.assertEqual(len(self.driver.navigations),1)
		self.assertLessEqual(self.clock(),rewards_tasks.BALANCE_READ_TIMEOUT)

	def test_normal_rendered_document_does_not_navigate_or_add_diagnostics(self):
		self.assertEqual(self.read(),1000)
		self.assertEqual(self.driver.navigations,[])
		self.assertIsNone(self.tasks._balance_read_diagnostic)

	def test_header_absence_alone_does_not_authorize_reload(self):
		self.driver.content_without_header=True
		self.driver.ready_at=float("inf")
		self.assertIsNone(self.read())
		self.assertEqual(self.driver.navigations,[])

	def test_loading_signin_unknown_snapshot_or_off_host_is_not_blank_recovery(self):
		for variant in ("loading","signin","snapshot_error","off_host"):
			with self.subTest(variant=variant):
				driver=BlankDriver(Clock(),recover=False)
				driver.blank=True
				if variant=="off_host": driver.pages["main"]="https://login.live.com/"
				else: setattr(driver,variant,True)
				tasks=tasks_for(driver)
				tasks._balance_timed_out=True
				self.assertIsNone(tasks._read_balance(prepare=True,clock=driver.clock,sleep=driver.clock.sleep))
				self.assertEqual(driver.navigations,[])

	def test_previous_balance_timeout_does_not_disable_first_blank_recovery(self):
		self.tasks._balance_timed_out=True
		self.driver.blank=True
		self.assertEqual(self.read(),1000)
		self.assertEqual(len(self.driver.navigations),1)
		self.assertFalse(self.tasks._balance_timed_out)

	def test_initial_or_zero_budget_read_does_not_start_blank_recovery(self):
		self.driver.blank=True
		self.assertIsNone(self.tasks._read_balance(clock=self.clock,sleep=self.clock.sleep))
		self.assertEqual(self.driver.navigations,[])
		self.tasks._balance_remaining=0
		self.assertIsNone(self.read())
		self.assertEqual(self.driver.navigations,[])

	def test_second_blank_document_in_same_run_is_not_reloaded(self):
		self.driver.blank=True
		self.assertEqual(self.read(),1000)
		self.driver.blank=True
		self.assertIsNone(self.read())
		self.assertEqual(len(self.driver.navigations),1)

	def test_reload_navigation_and_failed_return_share_existing_budget(self):
		self.driver.blank=True
		self.driver.get_duration=100
		self.assertIsNone(self.read())
		self.assertLessEqual(self.clock(),rewards_tasks.BALANCE_READ_TIMEOUT)
		self.assertEqual(self.driver.timeouts.page_load,60)
		self.assertEqual(self.tasks._balance_read_diagnostic["navigation"],"timed_out")
		self.assertIsNone(self.read())
		self.assertEqual(len(self.driver.navigations),1)

	def test_expired_budget_before_navigation_does_not_claim_a_reload(self):
		self.driver.blank=True
		snapshot=self.driver.execute_script
		def delayed(script,*args):
			packet=snapshot(script,*args)
			if script==page_structure.SNAPSHOT: self.clock.now+=rewards_tasks.BALANCE_READ_TIMEOUT
			return packet
		self.driver.execute_script=delayed
		self.assertIsNone(self.read())
		self.assertEqual(self.driver.navigations,[])
		self.assertNotIn("reloaded",self.tasks._balance_read_diagnostic)

	def test_real_task_loop_recovers_after_visual_and_preserves_unknown_boundary(self):
		for recover in (True,False):
			with self.subTest(recover=recover),tempfile.TemporaryDirectory() as folder:
				driver=BlankDriver(Clock(),recover=recover)
				tasks=tasks_for(driver)
				def visual(): driver.blank=True
				tasks.complete_visual_search=visual
				search_checks=[]
				# Only the search submission is synthetic; its real quota/earn
				# getter and independent task failure path remain intact.
				def search():
					tasks.progress="sent 0 searches"
					search_checks.append(tasks.read_search_points())
				tasks.complete_required_searches=search
				recorder=run_record.open_run("app",["synthetic"],{},[],run_id="20261115T093000Z-0b59",directory=folder)
				try:
					with run_record.account_context("synthetic"),mock.patch.dict(os.environ,{"REWARDS_SKIP_TASKS":""}):
						tasks.complete_all_tasks(only={"visual_search","searches"},clock=driver.clock,sleep=driver.clock.sleep)
					rows=run_record.read_events(recorder.path)
				finally: run_record.close_run("partial",0)
				ends=[row for row in rows if row["type"]=="task_end"]
				self.assertEqual(ends[0]["outcome"],"ok")
				self.assertEqual(ends[1]["outcome"],"ok" if recover else "fail")
				self.assertEqual(search_checks,[(6,60)] if recover else [])
				self.assertEqual(len(driver.navigations),1)
				self.assertTrue(ends[0]["balance_read"]["after"]["reloaded"])
				if not recover:
					self.assertEqual(ends[1]["reason_key"],"element_missing")
					self.assertEqual(ends[1]["detail"],"sent 0 searches, then the next element never appeared (NoSuchElementException)")
				self.assertEqual(ends[0]["points_after"],1000 if recover else None)
				self.assertEqual(ends[1]["points_before"],ends[0]["points_after"])
				structures=[row for row in rows if row["type"]=="reader_structure"]
				self.assertEqual([(row["attempt"],row["reloaded"]) for row in structures],[(1,False),(2,True)])
				encoded=json.dumps(structures)
				self.assertNotIn("Synthetic",encoded)
				self.assertNotIn("12,345",encoded)

	def test_baseline_blank_quota_fails_at_earn_tab_before_any_bing_navigation(self):
		self.driver.blank=True
		with self.assertRaises(NoSuchElementException): self.tasks.complete_required_searches()
		self.assertEqual(self.tasks.progress,"sent 0 searches")
		self.assertEqual(self.driver.navigations,[])
		self.assertEqual(self.driver.actions,[])


if __name__=="__main__": unittest.main()
