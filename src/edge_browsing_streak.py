"""Optional, UI-measured Edge browsing. Never submits or invents client reports."""
import logging
import contextlib
import math
import os
import time
from urllib.parse import urlsplit

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import NoSuchElementException
from element_selectors import ElementSelectionUtils, ElementNotReady
import run_record

TITLE = "Edge Browsing Streak"
EARN = "https://rewards.bing.com/earn"
PAGES = ("https://www.bing.com/", "https://www.msn.com/")
POLL_SECONDS = 180
SCROLL_SECONDS = 30
logger = logging.getLogger(__name__)


class EdgeStreakUnavailable(NoSuchElementException):
	def __init__(self, reason):
		self.reason = reason
		super().__init__(reason)


class EdgeStreakIncomplete(Exception):
	pass


@contextlib.contextmanager
def command_limits(driver):
	"""Bound page/transport stalls and restore the caller's driver settings."""
	page_load = driver.timeouts.page_load
	config = getattr(getattr(driver, "command_executor", None), "_client_config", None)
	transport = config.timeout if config is not None else None
	try:
		if config is not None:
			config.timeout = 15
		driver.set_page_load_timeout(15)
		yield
	finally:
		try:
			driver.set_page_load_timeout(page_load)
		finally:
			if config is not None:
				config.timeout = transport


def enabled():
	return os.environ.get("EDGE_BROWSING_STREAK", "").strip().lower() in ("1", "true", "yes")


def maximum_minutes():
	try:
		value = float(os.environ.get("EDGE_BROWSING_STREAK_MAX_MINUTES", "35"))
	except ValueError:
		raise ValueError("EDGE_BROWSING_STREAK_MAX_MINUTES must be a finite positive number") from None
	if not math.isfinite(value) or value <= 0:
		raise ValueError("EDGE_BROWSING_STREAK_MAX_MINUTES must be greater than0")
	return value


def normalized(value):
	return " ".join((value or "").split())


def find_tile(driver):
	elements = ElementSelectionUtils(driver)
	try:
		return elements._streak_tile(TITLE)
	except ElementNotReady:
		raise
	except NoSuchElementException:
		raise EdgeStreakUnavailable("Edge Browsing Streak is not available; shown tiles: " + ", ".join(elements.streak_tile_titles())) from None


def read_minutes(driver):
	counter = ElementSelectionUtils.streak_tile_counter(find_tile(driver), label="Minutes")
	if counter is None:
		raise ElementNotReady("Edge browsing minute counter is not ready")
	return counter


def activation(driver, *, panel_opened=False):
	# A tile may contain activation without a panel. Otherwise inspect only
	# a panel identified by its title; unknown app/visual panels cannot act.
	tile = find_tile(driver)
	for scope in [tile, *(driver.find_elements(By.TAG_NAME, "section") if panel_opened else [])]:
		if scope is not tile:
			if not (scope.get_dom_attribute("id") or "").startswith("react-aria") or not scope.is_displayed():
				continue
			labels = [title.casefold() for title in ElementSelectionUtils.streak_panel_titles(scope)]
			if labels and not any(TITLE.casefold() in label for label in labels):
				raise EdgeStreakIncomplete("Edge browsing opener showed another streak panel; stopped")
		for control in scope.find_elements(By.CSS_SELECTOR, 'a, button, [role="link"], [role="button"]'):
			if (control.is_displayed() and normalized(control.text).casefold() in ("activate streak", "get started")
				and control.get_dom_attribute("aria-disabled") != "true" and control.get_dom_attribute("disabled") is None):
				return control
	return None


def complete(tasks):
	if not enabled():
		return
	# App-only evidence additions surround the upstream algorithm. Unknown
	# readings must never look like an unchanged, fully measured top-up.
	began = time.monotonic()
	observation = {"minutes_before": None, "minutes_after": None, "minutes_target": None,
		"activated": False, "maximum_minutes": None, "maximum_reached": False,
		"final_read_success": False, "outcome": "error"}
	try:
		limit = maximum_minutes()
		observation["maximum_minutes"] = limit
		with command_limits(tasks.driver):
			return _complete(tasks, limit, observation=observation)
	except EdgeStreakUnavailable:
		observation["outcome"] = "not_available"
		raise
	except EdgeStreakIncomplete:
		observation["outcome"] = "incomplete"
		raise
	except KeyboardInterrupt:
		observation["outcome"] = "stopped"
		raise
	except Exception:
		observation["outcome"] = "error"
		raise
	finally:
		elapsed = max(0, time.monotonic() - began)
		observation["maximum_reached"] = observation["maximum_minutes"] is not None and elapsed >= observation["maximum_minutes"] * 60
		run_record.emit("edge_browsing", **observation, elapsed_seconds=round(elapsed, 3), duration_ms=round(elapsed * 1000))


def _complete(tasks, limit, *, observation):
	clock, sleep = time.monotonic, time.sleep
	began = clock()
	deadline = began + limit * 60
	before = after = None
	activated = False
	def budget():
		if clock() >= deadline:
			raise EdgeStreakIncomplete("Edge browsing reached its maximum minutes before completion")
	def measure():
		budget()
		tasks.driver.get(EARN)
		tasks.tab_utils.ensure_focus()
		url = urlsplit(tasks.driver.current_url)
		if url.hostname != "rewards.bing.com" or url.path.rstrip("/") != "/earn":
			raise EdgeStreakIncomplete("Edge browsing could not return to the earn page; stopped")
		# Rendered absence is definitive; an empty row may still hydrate.
		try:
			find_tile(tasks.driver)
		except __import__("element_selectors").ElementNotReady:
			tasks.wait_for_element(lambda: find_tile(tasks.driver), timeout=min(10, max(0, deadline-clock())))
		return tasks.wait_for_element(lambda: read_minutes(tasks.driver), timeout=min(10, max(0, deadline-clock())))
	# This constructor previously enabled Rewards-only emulation headers. The
	# new task must use the ordinary browser identity on both offer and pages.
	tasks.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {}})
	try:
		before, target = measure()
		after = before
		observation.update(minutes_before=before, minutes_after=after, minutes_target=target)
		logger.info("Edge browsing minutes before: %s/%s", before, target)
		if before >= target:
			observation.update(final_read_success=True, outcome="complete")
			return
		control = activation(tasks.driver)
		if control is None:
			tasks.move_to_and_click(lambda: find_tile(tasks.driver))
			def panel_ready():
				return next((section for section in tasks.driver.find_elements(By.TAG_NAME, "section")
					if (section.get_dom_attribute("id") or "").startswith("react-aria") and section.is_displayed()), None)
			tasks.wait_for_element(panel_ready, timeout=min(10, max(0, deadline-clock())))
			control = activation(tasks.driver, panel_opened=True)
		if control is not None:
			# No pointer retry after an ambiguous activation response.
			tasks.move_to_and_click(control, retries=1)
			activated = True
			observation["activated"] = True
		tasks.progress = f"Edge browsing minutes {before}/{target}"
		page = 0
		while after < target:
			budget()
			observation["final_read_success"] = False
			tasks.driver.get(PAGES[page % len(PAGES)])
			tasks.tab_utils.ensure_focus()
			next_read = min(deadline, clock() + POLL_SECONDS)
			while clock() < next_read:
				url = urlsplit(tasks.driver.current_url)
				if url.scheme != "https" or url.hostname not in ("bing.com", "www.bing.com", "msn.com", "www.msn.com"):
					raise EdgeStreakIncomplete("Edge browsing page changed to an unapproved host; stopped")
				ActionChains(tasks.driver).scroll_by_amount(0, 180 if page % 2 == 0 else -180).perform()
				sleep(max(0, min(SCROLL_SECONDS, next_read-clock())))
			previous = after
			if clock() >= deadline:
				# One last reading, not extra browsing, confirms the actual result.
				tasks.driver.get(EARN)
				url = urlsplit(tasks.driver.current_url)
				if url.hostname != "rewards.bing.com" or url.path.rstrip("/") != "/earn":
					raise EdgeStreakIncomplete("Edge browsing final counter page changed; stopped")
				after, current_target = read_minutes(tasks.driver)
			else:
				after, current_target = measure()
			observation.update(minutes_after=after, final_read_success=True)
			if current_target != target or after < previous:
				observation["final_read_success"] = False
				raise EdgeStreakIncomplete("Edge browsing counter changed its daily target; stopped")
			tasks.progress = f"Edge browsing minutes {before}/{target} -> {after}/{target}"
			page += 1
			if clock() >= deadline and after < target:
				raise EdgeStreakIncomplete(f"Edge browsing maximum {limit:g} minutes reached; counter {before}/{target} -> {after}/{target}")
		observation["outcome"] = "complete"
	finally:
		if before is not None:
			logger.info("Edge browsing minutes after: %s (before %s)", after, before)
