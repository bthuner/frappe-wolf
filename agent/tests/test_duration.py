import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent.util import parse_duration_minutes  # noqa: E402


class TestDuration(unittest.TestCase):
	def test_units(self):
		self.assertEqual(parse_duration_minutes("15m"), 15)
		self.assertEqual(parse_duration_minutes("15"), 15)
		self.assertEqual(parse_duration_minutes("1h"), 60)
		self.assertEqual(parse_duration_minutes("2H"), 120)
		self.assertEqual(parse_duration_minutes("1d"), 1440)
		self.assertEqual(parse_duration_minutes(" 30 m "), 30)

	def test_seconds_round_up(self):
		self.assertEqual(parse_duration_minutes("90s"), 2)
		self.assertEqual(parse_duration_minutes("60s"), 1)
		self.assertEqual(parse_duration_minutes("1s"), 1)

	def test_invalid(self):
		for value in ("", None, "0m", "-5m", "15x", "1h30m", "soon"):
			with self.subTest(value=value), self.assertRaises(ValueError):
				parse_duration_minutes(value)
