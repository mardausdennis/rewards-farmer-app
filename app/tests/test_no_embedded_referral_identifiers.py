import _bootstrap  # noqa: F401

from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit
import unittest


URL = re.compile(r"https?://(?:rewards\.bing\.com|(?:www\.)?bing\.com)/[^\s\"'<>\\]*", re.IGNORECASE)
CODE_KEYS = {"code", "ref", "refid", "referrer", "referral", "referralcode", "referralid", "invitationcode"}
TEXT_SUFFIXES = {".py", ".js", ".json", ".html", ".css", ".md", ".toml", ".txt", ".yml", ".yaml"}


def embedded_identifiers(roots):
	problems = []
	for root in roots:
		for path in root.rglob("*"):
			if not path.is_file() or path.suffix not in TEXT_SUFFIXES or any(part in ("__pycache__", "node_modules", "dist", "build") for part in path.parts):
				continue
			for match in URL.finditer(path.read_text(encoding="utf-8")):
				parsed = urlsplit(match.group())
				segments = parsed.path.strip("/").split("/")
				if not segments or segments[0].casefold() not in ("refer", "referandearn"):
					continue
				if any(not re.fullmatch(r"SYNTHETIC(?:_[A-Z0-9]+)*", unquote(part)) for part in segments[1:]):
					problems.append(str(path.relative_to(root)))
				for key, value in parse_qsl(parsed.query):
					if key.casefold() in CODE_KEYS and value and not re.fullmatch(r"SYNTHETIC(?:_[A-Z0-9]+)*", value):
						problems.append(str(path.relative_to(root)))
	return problems


class EmbeddedReferralTests(unittest.TestCase):
	def test_no_developer_identifier_under_app_or_src(self):
		root = Path(__file__).resolve().parents[2]
		self.assertEqual(embedded_identifiers([root / "app", root / "src"]), [])

	def test_scanner_detects_identifier_and_allows_only_obvious_synthetic_codes(self):
		with tempfile.TemporaryDirectory() as folder:
			root = Path(folder)
			path = root / "sample.json"
			for key in CODE_KEYS:
				path.write_text('"https://rewards.bing.com/refer?' + key + '=NOT_PUBLIC_EXAMPLE"', encoding="utf-8")
				self.assertEqual(embedded_identifiers([root]), ["sample.json"])
				path.write_text('"https://rewards.bing.com/refer?' + key + '=SYNTHETIC"', encoding="utf-8")
				self.assertEqual(embedded_identifiers([root]), [])
			for prefix in ("refer", "referandearn"):
				path.write_text('"https://rewards.bing.com/' + prefix + '/NOT_PUBLIC_EXAMPLE"', encoding="utf-8")
				self.assertEqual(embedded_identifiers([root]), ["sample.json"])
				path.write_text('"https://rewards.bing.com/' + prefix + '/SYNTHETIC"', encoding="utf-8")
				self.assertEqual(embedded_identifiers([root]), [])
