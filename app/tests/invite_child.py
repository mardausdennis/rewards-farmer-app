"""Test-only process: real launcher/signin, injected driver, disposable state."""

import _bootstrap  # noqa: F401

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import Mock, patch


def main(args):
	from rfapp import paths
	home = paths.data_home()
	if not (home / "synthetic-foundation.json").is_file(): raise RuntimeError("Synthetic fixture marker required")
	assert not any("RewardsFarmer" == part for part in home.parts)
	browser = ModuleType("browser")
	browser.build_options = lambda account: account
	browser.build_service = lambda: None
	events = []
	class Driver:
		window_handles = ["synthetic"]
		def __init__(self, options, service): self.account = options
		def set_page_load_timeout(self, value): pass
		def get(self, url):
			events.append("dashboard" if url == "https://rewards.bing.com/" else "invite")
			Path(self.account.user_data_dir).joinpath("Local State").write_text(json.dumps({"profile": {"info_cache": {"Default": {"gaia_name": "Synthetic New Person"}}}}), encoding="utf-8")
			(home / "driver-events.json").write_text(json.dumps(events), encoding="utf-8")
		@property
		def current_url(self): return "https://rewards.bing.com/dashboard"
		def find_elements(self, by, selector):
			return [type("Greeting", (), {"text": "Welcome, Synthetic New Person!", "is_displayed": lambda self: True})()]
		def quit(self):
			events.append("quit")
			(home / "driver-events.json").write_text(json.dumps(events), encoding="utf-8")
	from selenium import webdriver
	spec = importlib.util.spec_from_file_location("f119_real_entry", paths.repo_root() / "app/launcher.py")
	entry = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(entry)
	with patch.dict(sys.modules, {"browser": browser}), patch.object(webdriver, "Edge", Driver):
		return entry.main(args)


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))
