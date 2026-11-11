import _bootstrap  # noqa: F401

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rfapp import prefs, window


PRIMARY = (0, 0, 1920, 1040)
LEFT = (-1280, 0, 1280, 984)


def rectangle(x=120, y=100, width=1100, height=760):
	return SimpleNamespace(X=x, Y=y, Width=width, Height=height)


class GeometryRecoveryTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.home = Path(self.temp.name)
		for guard in (patch.dict(os.environ, {"REWARDS_FARMER_DATA_HOME": str(self.home)}), patch.object(window, "_work_areas", return_value=[PRIMARY, LEFT], create=True), patch.object(window, "_dpi_scale", return_value=1.0, create=True), patch.object(window, "_theme", return_value="light")):
			guard.start()
			self.addCleanup(guard.stop)
		self.api = SimpleNamespace(_module=lambda name: prefs if name == "prefs" else SimpleNamespace(current=lambda: SimpleNamespace(t=lambda key: key)))
		self.webview = SimpleNamespace(create_window=Mock(return_value=Mock()))
		guard = patch.dict(sys.modules, {"webview": self.webview})
		guard.start()
		self.addCleanup(guard.stop)

	def create(self, saved, hidden=False):
		prefs.save({"window": saved, "setup_done": True, "future_key": {"keep": 1}})
		window._create_window(self.api, start_hidden=hidden, page="synthetic.html")
		return self.webview.create_window.call_args.kwargs

	def test_old_minimized_position_heals_on_launch_without_losing_other_prefs(self):
		for x, y in ((-32000, -32000), (0, 1030), (3200, 200), (None, 50)):
			with self.subTest(x=x, y=y):
				kwargs = self.create({"x": x, "y": y, "width": 1100, "height": 760, "maximized": True, "future": "kept"})
				self.assertEqual(tuple(kwargs[key] for key in ("x", "y", "width", "height")), (410, 140, 1100, 760))
				self.assertTrue(kwargs["maximized"])
				stored = prefs.load()
				self.assertEqual(stored["window"]["x"], 410)
				self.assertEqual(stored["window"]["future"], "kept")
				self.assertTrue(stored["setup_done"])
				self.assertEqual(stored["future_key"], {"keep": 1})

	def test_valid_negative_monitor_position_is_kept_and_size_clamped_to_its_workarea(self):
		kwargs = self.create({"x": -1200, "y": 100, "width": 5000, "height": 5000})
		self.assertEqual(tuple(kwargs[key] for key in ("x", "y", "width", "height")), (-1280, 0, 1280, 984))

	def test_small_workarea_wins_over_min_size_and_hidden_start_stays_hidden(self):
		with patch.object(window, "_work_areas", return_value=[(0, 40, 800, 500)]):
			kwargs = self.create({}, hidden=True)
		self.assertEqual(tuple(kwargs[key] for key in ("x", "y", "width", "height")), (0, 40, 800, 500))
		self.assertEqual(kwargs["min_size"], (800, 500))
		self.assertTrue(kwargs["hidden"])

	def test_tiny_positive_size_is_raised_to_minimum_without_a_spurious_position_reset(self):
		kwargs = self.create({"x": 20, "y": 50, "width": 1, "height": 1})
		self.assertEqual(tuple(kwargs[key] for key in ("x", "y", "width", "height")), (20, 50, 860, 600))

	def test_titlebar_must_be_reachable_not_just_a_strip_of_the_window_body(self):
		for x, y in ((1910, 200), (100, -720), (100, 1020)):
			kwargs = self.create({"x": x, "y": y, "width": 1100, "height": 760})
			self.assertEqual((kwargs["x"], kwargs["y"]), (410, 140))

	def test_bad_nested_preferences_are_not_passed_to_webview(self):
		for saved in (None, [], "broken", {"width": None, "height": "760", "x": True, "y": float("inf"), "maximized": "false"}):
			with self.subTest(saved=saved):
				kwargs = self.create(saved)
				self.assertEqual(tuple(kwargs[key] for key in ("x", "y", "width", "height")), (410, 140, 1100, 760))
				self.assertFalse(kwargs["maximized"])

	def test_prefs_write_failure_cannot_prevent_recovery_window(self):
		prefs.save({"window": {"x": -32000, "y": -32000}})
		with patch.object(prefs, "save", side_effect=PermissionError("synthetic readonly prefs")), patch.object(window._log, "exception") as logged:
			window._create_window(self.api)
		kwargs = self.webview.create_window.call_args.kwargs
		self.assertEqual((kwargs["x"], kwargs["y"]), (410, 140))
		logged.assert_called_once()

	def save_native(self, state, bounds, restore=None, scale=1):
		native = SimpleNamespace(WindowState=state, Bounds=bounds, RestoreBounds=restore or bounds, InvokeRequired=False, _scale=scale)
		runtime = window._Runtime.__new__(window._Runtime)
		runtime._bridge = self.api
		runtime._window = SimpleNamespace(native=native, x=int(bounds.X / scale), y=int(bounds.Y / scale), width=int(bounds.Width / scale), height=int(bounds.Height / scale))
		runtime._save_window()

	def test_save_minimized_keeps_last_normal_geometry_even_with_sentinel_or_icon_bounds(self):
		original = {"x": 120, "y": 100, "width": 1100, "height": 760, "maximized": True}
		for bounds in (rectangle(-32000, -32000, 160, 28), rectangle(0, 1004, 160, 28)):
			prefs.save({"window": original})
			self.save_native("Minimized", bounds, rectangle())
			self.assertEqual(prefs.load()["window"], original)

	def test_save_maximized_uses_normal_restore_bounds_not_maximized_dimensions(self):
		self.save_native("Maximized", rectangle(-8, -8, 1936, 1056), rectangle())
		self.assertEqual(prefs.load()["window"], {"x": 120, "y": 100, "width": 1100, "height": 760, "maximized": True})

	def test_native_physical_bounds_are_saved_in_pywebview_logical_units(self):
		self.save_native("Normal", rectangle(180, 150, 1650, 1140), scale=1.5)
		self.assertEqual(prefs.load()["window"], {"x": 120, "y": 100, "width": 1100, "height": 760, "maximized": False})

	def test_save_offscreen_skips_without_overwriting_the_last_reachable_geometry(self):
		prefs.save({"window": {"x": 50, "y": 50, "width": 900, "height": 650}})
		before = prefs.load()["window"]
		self.save_native("Normal", rectangle(6000, 100))
		self.assertEqual(prefs.load()["window"], before)

	def test_monitor_disconnect_and_dpi_change_heal_an_existing_tray_window_on_show(self):
		for state in ("Normal", "Maximized", "Minimized"):
			with self.subTest(state=state):
				win = Mock()
				win.native.WindowState = state
				win.native.InvokeRequired = False
				runtime = window._Runtime.__new__(window._Runtime)
				runtime._quitting = False
				runtime._stop = threading.Event()
				runtime._window = win
				runtime._web_sleep = Mock()
				runtime._condition = threading.Condition()
				runtime._tray = Mock()
				runtime._wake_observer = Mock()
				runtime.push = Mock()
				order = []
				runtime._web_sleep.resume.side_effect = lambda: order.append("resume")
				win.restore.side_effect = lambda: order.append("restore")
				win.show.side_effect = lambda: order.append("show")
				def dispatch(win, action):
					self.assertFalse(runtime._condition._is_owned())
					action()
				with patch.object(window, "_on_ui", side_effect=dispatch), patch.object(window, "_recover_window", side_effect=lambda win: order.append("recover")), patch.object(window, "_focus_window", return_value=True):
					runtime.show("#/overview")
				self.assertEqual(order, ["resume"] + (["restore"] if state == "Minimized" else []) + ["recover", "show"])
				self.assertTrue(runtime._visible)
				runtime.push.assert_called_once_with("navigate", {"route": "#/overview"})

	def test_runtime_still_quitting_does_not_attempt_to_show_disposed_native_window(self):
		runtime = window._Runtime.__new__(window._Runtime)
		runtime._quitting = True
		runtime._window = Mock()
		runtime._window.native.InvokeRequired = False
		runtime._web_sleep = Mock()
		runtime._tray = Mock()
		runtime._wake_observer = Mock()
		runtime.show()
		runtime._web_sleep.resume.assert_not_called()
		runtime._window.show.assert_not_called()

	def test_second_instance_during_startup_waits_for_native_form_before_show(self):
		runtime = window._Runtime.__new__(window._Runtime)
		runtime._quitting = False
		runtime._stop = threading.Event()
		runtime._condition = threading.Condition()
		runtime._web_sleep = Mock()
		runtime._tray = Mock()
		runtime._wake_observer = Mock()
		runtime._window = Mock()
		runtime._window.native = None
		def ready(timeout):
			self.assertEqual(timeout, 15)
			runtime._window.native = SimpleNamespace(InvokeRequired=False, WindowState="Normal")
			return True
		runtime._window.events.shown.wait.side_effect = ready
		with patch.object(window, "_recover_window"), patch.object(window, "_focus_window", return_value=True):
			runtime.show()
		runtime._window.events.shown.wait.assert_called_once_with(15)
		runtime._window.show.assert_called_once()
		self.assertTrue(runtime._visible)

	def test_second_instance_reports_unready_window_instead_of_accessing_none_native(self):
		runtime = window._Runtime.__new__(window._Runtime)
		runtime._quitting = False
		runtime._stop = threading.Event()
		runtime._window = Mock()
		runtime._window.native = None
		runtime._window.events.shown.wait.return_value = False
		with patch.object(window._log, "warning") as logged:
			runtime.show()
		logged.assert_called_once()
		runtime._window.show.assert_not_called()

	def test_complete_invalid_json_falls_back_without_changing_hidden_or_maximized_flags(self):
		(self.home / "app-prefs.json").write_text('{"window":', encoding="utf-8")
		with patch.object(prefs._logger, "exception"):
			window._create_window(self.api, start_hidden=False)
		kwargs = self.webview.create_window.call_args.kwargs
		self.assertEqual((kwargs["x"], kwargs["y"], kwargs["width"], kwargs["height"]), (410, 140, 1100, 760))
		self.assertFalse(kwargs["hidden"])
		self.assertFalse(kwargs["maximized"])

	def test_workarea_geometry_is_idempotent_for_negative_origins_small_and_oversized_windows(self):
		for areas in ([PRIMARY, LEFT], [(0, 40, 800, 500)], [(-1920, -1080, 1920, 1040)]):
			for x in (-32000, -1800, 100, 3500):
				for size in (1, 900, 5000):
					p, minimum = window._window_geometry({"x": x, "y": 100, "width": size, "height": size, "maximized": True}, areas)
					self.assertEqual(window._window_geometry(p, areas), (p, minimum))
					self.assertIsNotNone(window._titlebar_area(p, areas))


class ResumeRecoveryTests(unittest.TestCase):
	def setUp(self):
		from enum import Enum
		class Level(Enum):
			Normal = 0
			Low = 1
		self.core = SimpleNamespace(Resume=Mock(), TrySuspendAsync=Mock(return_value="task"), MemoryUsageTargetLevel=Level.Low)
		self.controller = SimpleNamespace(IsVisible=False)
		control = SimpleNamespace(Visible=False, CoreWebView2=self.core)
		control.GetType = lambda: SimpleNamespace(GetField=lambda *args: SimpleNamespace(GetValue=lambda _: self.controller))
		self.native = SimpleNamespace(Visible=False, webview=control)
		self.win = SimpleNamespace(native=self.native)
		guard = patch.dict(sys.modules, {"System.Reflection": SimpleNamespace(BindingFlags=SimpleNamespace(Instance=4, NonPublic=32))})
		guard.start()
		self.addCleanup(guard.stop)

	def test_resume_error_still_restores_control_visibility_and_is_retryable(self):
		self.core.Resume.side_effect = [RuntimeError("synthetic resume fault"), None]
		sleep = window._WebViewSleep(self.win, invoke=lambda action: action())
		with patch.object(window._log, "exception"):
			sleep.resume()
		self.assertTrue(self.native.webview.Visible)
		self.assertTrue(self.controller.IsVisible)
		self.assertEqual(sleep.state, "failed")
		sleep.resume()
		self.assertEqual(sleep.state, "awake")

	def test_late_suspend_request_cannot_hide_a_window_after_a_show_request(self):
		pending = []
		sleep = window._WebViewSleep(self.win, invoke=pending.append)
		sleep._page_ready = True
		sleep.suspend_after_ready()
		sleep.resume()
		# A queued UI hide action arrives after the request to show, before the form becomes visible.
		pending[0]()
		self.core.TrySuspendAsync.assert_not_called()

	def test_memory_hint_error_does_not_prevent_restoring_visible_client(self):
		sleep = window._WebViewSleep(self.win, invoke=lambda action: action())
		with patch.object(sleep, "_memory", side_effect=RuntimeError("synthetic memory hint failure")), patch.object(window._log, "exception"):
			sleep.resume()
		self.assertTrue(self.native.webview.Visible)
		self.assertTrue(self.controller.IsVisible)
