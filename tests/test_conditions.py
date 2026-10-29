import unittest
from datetime import time, timedelta

from frappe_wolf.wolf import conditions as c


class TestCompare(unittest.TestCase):
	def test_operators(self):
		self.assertTrue(c.compare(91, ">", 90))
		self.assertFalse(c.compare(90, ">", 90))
		self.assertTrue(c.compare(90, ">=", 90))
		self.assertTrue(c.compare("3", "==", 3.0))
		self.assertFalse(c.compare(None, ">", 0))
		with self.assertRaises(ValueError):
			c.compare(1, "=>", 0)

	def test_breached_needs_samples(self):
		row = {"value": 95, "samples": 2}
		self.assertFalse(c.is_breached(row, ">", 90, min_samples=3))
		self.assertTrue(c.is_breached(row, ">", 90, min_samples=2))
		self.assertFalse(c.is_breached({"value": None, "samples": 0}, ">=", 0))


class TestActiveWindow(unittest.TestCase):
	def test_no_window(self):
		self.assertTrue(c.in_active_window(time(12), None, None))

	def test_simple_window(self):
		self.assertTrue(c.in_active_window(time(2, 0), "01:00:00", "04:00:00"))
		self.assertFalse(c.in_active_window(time(4, 0), "01:00:00", "04:00:00"))
		self.assertFalse(c.in_active_window(time(0, 59), "01:00", "04:00"))

	def test_wraps_midnight(self):
		self.assertTrue(c.in_active_window(time(23, 0), "04:00:00", "01:00:00"))
		self.assertTrue(c.in_active_window(time(0, 30), "04:00:00", "01:00:00"))
		self.assertFalse(c.in_active_window(time(2, 0), "04:00:00", "01:00:00"))

	def test_mariadb_timedelta(self):
		self.assertTrue(c.in_active_window(time(2), timedelta(hours=1), timedelta(hours=4)))

	def test_nightly_and_daytime_presets_are_complementary(self):
		for hour in range(24):
			for minute in (0, 30, 59):
				t = time(hour, minute)
				night = c.in_active_window(t, "01:00:00", "04:00:00")
				day = c.in_active_window(t, "04:00:00", "01:00:00")
				self.assertNotEqual(night, day, t)


class TestGroups(unittest.TestCase):
	def test_keys(self):
		row = {"host": "h1", "bench": "main", "site": "a.example.com", "label_key": "mount=/"}
		self.assertEqual(c.group_key("none", row), "*")
		self.assertEqual(c.group_key("site", row), "site=a.example.com")
		self.assertEqual(c.group_key("bench", row), "host=h1,bench=main")
		self.assertEqual(c.group_key("host_label", row), "host=h1,label_key=mount=/")
		self.assertEqual(c.group_key("site", {"site": None}), "site=-")
		self.assertEqual(c.describe_group("host_label", row), "h1 mount=/")
		self.assertEqual(c.describe_group("none", row), "all")
		with self.assertRaises(ValueError):
			c.group_columns("planet")

	def test_severities(self):
		self.assertEqual(c.severities_at_least("warning"), ["warning", "critical"])
		self.assertEqual(c.severities_at_least(None), ["info", "warning", "critical"])
		self.assertTrue(c.severity_at_least("critical", "warning"))
		self.assertFalse(c.severity_at_least("info", "warning"))

	def test_format(self):
		self.assertEqual(c.format_value(10.0), "10")
		self.assertEqual(c.format_value(1.23456), "1.23")
		self.assertEqual(c.format_value(None), "n/a")


if __name__ == "__main__":
	unittest.main()
