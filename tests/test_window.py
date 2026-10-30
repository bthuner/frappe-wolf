import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import window
from frappe_wolf.wolf.schema import PayloadError

NOW = datetime(2026, 10, 30, 14, 0)  # Europe/Paris, CET (UTC+1) after the fall-back


class TestParse(unittest.TestCase):
	def test_relative_default(self):
		win = window.parse(NOW, "Europe/Paris")
		self.assertEqual((win.since, win.until, win.relative), (NOW - timedelta(hours=24), NOW, "24h"))

	def test_relative_units(self):
		self.assertEqual(window.parse(NOW, "UTC", range="15m").since, NOW - timedelta(minutes=15))
		self.assertEqual(window.parse(NOW, "UTC", range=" 7d ").since, NOW - timedelta(days=7))

	def test_bad_ranges(self):
		for bad in ("0h", "24", "1w", "-1d", "abc", "99999d"):
			with self.assertRaises(PayloadError, msg=bad):
				window.parse(NOW, "UTC", range=bad)
		with self.assertRaises(PayloadError):
			window.parse(NOW, "UTC", range="401d")  # longer than any retention

	def test_explicit_times_are_converted_to_the_site_zone(self):
		win = window.parse(NOW, "Europe/Paris", since="2026-10-30T08:00:00Z", until="2026-10-30T10:30:00+00:00")
		self.assertEqual(win.since, datetime(2026, 10, 30, 9, 0))
		self.assertEqual(win.until, datetime(2026, 10, 30, 11, 30))
		self.assertIsNone(win.relative)

	def test_explicit_until_defaults_to_now(self):
		self.assertEqual(window.parse(NOW, "UTC", since="2026-10-30T10:00:00Z").until, NOW)

	def test_inconsistent_explicit_windows(self):
		with self.assertRaises(PayloadError):
			window.parse(NOW, "UTC", since="2026-10-30T12:00:00Z", until="2026-10-30T11:00:00Z")
		with self.assertRaises(PayloadError):
			window.parse(NOW, "UTC", until="2026-10-30T11:00:00Z")

	def test_describe_is_utc(self):
		win = window.parse(NOW, "Europe/Paris", range="1h")
		self.assertEqual(
			window.describe(win),
			{"since": "2026-10-30T12:00:00Z", "until": "2026-10-30T13:00:00Z", "relative": "1h", "time_zone": "Europe/Paris"},
		)

	def test_describe_across_dst(self):
		# 2026-10-25 03:00 CEST -> 02:00 CET: the window still describes real instants
		win = window.Window(datetime(2026, 10, 25, 1, 30), datetime(2026, 10, 25, 3, 30), None, "Europe/Paris")
		self.assertEqual(window.describe(win)["since"], "2026-10-24T23:30:00Z")
		self.assertEqual(window.describe(win)["until"], "2026-10-25T02:30:00Z")


class TestScope(unittest.TestCase):
	def test_filters(self):
		self.assertEqual(window.scope_filters(site=" a.example.com ", host=""), {"site": "a.example.com"})
		self.assertEqual(window.scope_filters(host="h1", bench="frappe-bench"), {"bench": "frappe-bench", "host": "h1"})

	def test_bench_needs_host(self):
		with self.assertRaises(PayloadError):
			window.scope_filters(bench="frappe-bench")


if __name__ == "__main__":
	unittest.main()
