import io
import json
import unittest

from frappe_wolf.wolf import advisories as adv


def entry(range_, patched):
	return {"range": range_, "patched": patched}


class TestVersions(unittest.TestCase):
	def test_parse_and_encode(self):
		self.assertEqual(adv.parse_version("15.100.0"), (15, 100, 0))
		self.assertEqual(adv.parse_version(" v16.35.2 "), (16, 35, 2))
		for bad in ("16.0.0-dev", "15.x.x-develop", "", None, "1.2", "tbd"):
			self.assertIsNone(adv.parse_version(bad), bad)
		self.assertEqual(adv.encode((15, 100, 0)), 15100000)
		self.assertEqual(adv.decode(15100000.0), (15, 100, 0))
		self.assertEqual(adv.decode(16035002), (16, 35, 2))
		self.assertIsNone(adv.encode((15, 1000, 0)))

	def test_ranges_as_github_has_them(self):
		self.assertEqual(adv.parse_range("<16.21.0"), [("<", (16, 21, 0))])
		self.assertEqual(adv.parse_range("< v15.120.0"), [("<", (15, 120, 0))])
		self.assertEqual(adv.parse_range("<= v16.18.2"), [("<=", (16, 18, 2))])
		self.assertEqual(adv.parse_range(">= 15.0.0, < 15.123.0"), [(">=", (15, 0, 0)), ("<", (15, 123, 0))])
		self.assertEqual(adv.parse_range("16.11.0"), [("=", (16, 11, 0))])
		self.assertEqual(adv.parse_range("<15.91.1 "), [("<", (15, 91, 1))])
		for bad in ("tbd", "", None, "< 16.x"):
			self.assertIsNone(adv.parse_range(bad), bad)

	def test_affects(self):
		v16 = entry("<16.21.0", "16.21.0")
		self.assertTrue(adv.affects((16, 20, 9), v16))
		self.assertFalse(adv.affects((16, 21, 0), v16))
		self.assertFalse(adv.affects((15, 0, 0), v16))  # an upper bound only covers its own line
		self.assertFalse(adv.affects((14, 0, 0), v16))
		self.assertTrue(adv.affects((16, 18, 2), entry("<= v16.18.2", "> v16.18.2")))
		self.assertFalse(adv.affects((16, 18, 3), entry("<= v16.18.2", "> v16.18.2")))
		lower = entry(">= 15.0.0, < 15.123.0", "15.123.0")
		self.assertTrue(adv.affects((15, 0, 0), lower))
		self.assertFalse(adv.affects((14, 99, 0), lower))
		self.assertFalse(adv.affects((16, 11, 0), entry("16.11.0", "16.11.0")))  # patched wins over a typo
		self.assertIsNone(adv.affects((16, 0, 0), entry("tbd", "tbd")))


ADVISORIES = [
	{"id": "GHSA-new", "severity": "high", "published": "2026-10-01", "summary": "new",
	 "ranges": [entry("<16.30.0", "16.30.0"), entry("<15.120.0", "15.120.0"), entry("tbd", "tbd")]},
	{"id": "GHSA-old", "severity": "medium", "published": "2025-03-01", "summary": "old",
	 "ranges": [entry("<14.90.0", "14.90.0"), entry("<13.50.0", "13.50.0")]},
]  # fmt: skip


class TestAssess(unittest.TestCase):
	def test_statuses(self):
		self.assertEqual(adv.lines(ADVISORIES), {15, 16})  # 13 and 14 not named in the last year
		r = adv.assess("frappe", (16, 29, 0), ADVISORIES)
		self.assertEqual((r["status"], r["fixed_in"], [a["id"] for a in r["found"]]), ("affected", "16.30.0", ["GHSA-new"]))
		self.assertEqual(adv.assess("frappe", (16, 30, 0), ADVISORIES)["status"], "clear")
		self.assertEqual(adv.assess("frappe", (13, 0, 0), ADVISORIES)["status"], "affected")  # an old advisory still applies
		r = adv.assess("frappe", (14, 95, 0), ADVISORIES)
		self.assertEqual(r["status"], "unknown")  # past the old fix, but no recent advisory names 14.x
		self.assertIn("not 14.x", r["why"])
		self.assertEqual(adv.assess("frappe", (4, 2, 0), ADVISORIES)["status"], "unknown")

	def test_summary_orders_by_severity(self):
		found = [{"id": "B", "severity": "low"}, {"id": "A", "severity": "critical"}, {"id": "C", "severity": "low"}]
		text = adv.summary({"app": "erpnext", "version": "15.1.0", "found": found, "fixed_in": "15.2.0"})
		self.assertEqual(text, "erpnext 15.1.0: 3 advisories (1 critical, 2 low), fixed in 15.2.0")
		self.assertEqual(adv.summary({"app": "x", "version": "1.0.0", "found": [], "fixed_in": None}), "x 1.0.0: no advisory applies")


class FakeResponse(io.BytesIO):
	def __init__(self, body, link=None):
		super().__init__(json.dumps(body).encode())
		self.headers = {"Link": link} if link else {}


class TestFetch(unittest.TestCase):
	def test_pages_and_compaction(self):
		raw = {"ghsa_id": "GHSA-1", "cve_id": "CVE-1", "severity": "HIGH", "summary": " s ", "html_url": "u",
		       "published_at": "2026-10-01T10:00:00Z", "withdrawn_at": None, "state": "published",
		       "vulnerabilities": [{"package": {"name": "frappe"}, "vulnerable_version_range": "<16.1.0", "patched_versions": "16.1.0"}]}  # fmt: skip
		withdrawn = {**raw, "ghsa_id": "GHSA-2", "withdrawn_at": "2026-10-02T00:00:00Z"}
		pages = {
			adv.API.format(repo="frappe/frappe"): ([raw], '<https://api.github.com/next?page=2>; rel="next", <https://x>; rel="last"'),
			"https://api.github.com/next?page=2": ([withdrawn], None),
		}
		seen = []

		def opener(req, timeout):
			seen.append((req.full_url, req.get_header("User-agent")))
			body, link = pages[req.full_url]
			return FakeResponse(body, link)

		out = adv.fetch("frappe/frappe", opener=opener)
		self.assertEqual(len(seen), 2)
		self.assertEqual(seen[0][1], "frappe-wolf")
		self.assertEqual(out, [{"id": "GHSA-1", "cve": "CVE-1", "severity": "high", "summary": "s", "url": "u",
		                        "published": "2026-10-01", "ranges": [{"range": "<16.1.0", "patched": "16.1.0"}]}])  # fmt: skip

	def test_an_error_object_is_refused(self):
		with self.assertRaises(ValueError):
			adv.fetch("frappe/frappe", opener=lambda req, timeout: FakeResponse({"message": "API rate limit exceeded"}))


if __name__ == "__main__":
	unittest.main()
