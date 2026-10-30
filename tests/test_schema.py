import json
import unittest
from datetime import datetime

from frappe_wolf.wolf import schema

NOW = datetime(2026, 10, 29, 3, 0, 0)


class TestTimestamps(unittest.TestCase):
	def test_utc_to_system_timezone(self):
		self.assertEqual(
			schema.parse_timestamp("2026-10-29T02:00:00Z", "Europe/Paris"), datetime(2026, 10, 29, 3, 0, 0)
		)
		# Summer time: UTC+2
		self.assertEqual(
			schema.parse_timestamp("2026-07-01T02:00:00Z", "Europe/Paris"), datetime(2026, 7, 1, 4, 0, 0)
		)

	def test_offset_and_naive(self):
		self.assertEqual(schema.parse_timestamp("2026-10-29T04:00:00+02:00", "UTC"), datetime(2026, 10, 29, 2))
		self.assertEqual(schema.parse_timestamp("2026-10-29T02:00:00", "UTC"), datetime(2026, 10, 29, 2))

	def test_default_and_invalid(self):
		self.assertEqual(schema.parse_timestamp(None, "UTC", default=NOW), NOW)
		with self.assertRaises(schema.PayloadError):
			schema.parse_timestamp("yesterday", "UTC")
		with self.assertRaises(schema.PayloadError):
			schema.parse_timestamp(None, "UTC")


class TestMetric(unittest.TestCase):
	def test_normalize(self):
		m = schema.normalize_metric(
			{"metric_name": "disk_used_percent", "value": "91.5", "timestamp": "2026-10-29T02:00:00Z", "labels": {"mount": "/"}},
			"UTC",
			NOW,
			default_host="bench-01",
		)
		self.assertEqual(m["value"], 91.5)
		self.assertEqual(m["host"], "bench-01")
		self.assertEqual(m["label_key"], "mount=/")
		self.assertEqual(json.loads(m["labels"]), {"mount": "/"})

	def test_rejects_bad_values(self):
		bad_values = (
			{"value": 1},
			{"metric_name": "x", "value": "abc"},
			{"metric_name": "x", "value": float("nan")},
			{"metric_name": "x", "value": 1e13},  # beyond the decimal(21,9) column
			"x",
		)
		for bad in bad_values:
			with self.assertRaises(schema.PayloadError):
				schema.normalize_metric(bad, "UTC", NOW)


class TestEvent(unittest.TestCase):
	def test_normalize(self):
		e = schema.normalize_event(
			{
				"type": "error",
				"category": "http_502",
				"severity": "critical",
				"timestamp": "2026-10-29T02:00:13Z",
				"bench": "main",
				"site": "a.example.com",
				"source": "nginx",
				"title": "502 on GET /api/method/foo",
				"payload": {"count": 14, "route": "/api/method/foo"},
			},
			"UTC",
			NOW,
			default_host="bench-01",
		)
		self.assertEqual(e["count"], 14)
		self.assertEqual(e["host"], "bench-01")
		self.assertEqual(e["timestamp"], e["last_seen"])
		self.assertEqual(len(e["fingerprint"]), 40)
		self.assertEqual(json.loads(e["payload"])["route"], "/api/method/foo")

	def test_defaults_and_rejects(self):
		e = schema.normalize_event({"type": "deployment"}, "UTC", NOW, default_host="h")
		self.assertEqual(e["severity"], "warning")
		self.assertEqual(e["timestamp"], NOW)
		self.assertEqual(e["title"], "deployment on h")
		with self.assertRaises(schema.PayloadError):
			schema.normalize_event({"type": "meteor"}, "UTC", NOW)
		with self.assertRaises(schema.PayloadError):
			schema.normalize_event({"type": "error", "severity": "apocalyptic"}, "UTC", NOW)

	def test_large_payload_truncated(self):
		e = schema.normalize_event({"type": "error", "payload": {"tb": "x" * 100_000}}, "UTC", NOW)
		self.assertLess(len(e["payload"]), 40_000)
		self.assertTrue(json.loads(e["payload"])["truncated"])


if __name__ == "__main__":
	unittest.main()


class TestClockSkew(unittest.TestCase):
	def test_clamp_future(self):
		from datetime import timedelta

		self.assertEqual(schema.clamp_future(NOW + timedelta(seconds=299), NOW, 300), (NOW + timedelta(seconds=299), False))
		self.assertEqual(schema.clamp_future(NOW + timedelta(hours=11), NOW, 300), (NOW, True))
		self.assertEqual(schema.clamp_future(NOW - timedelta(days=1), NOW, 300), (NOW - timedelta(days=1), False))
		# 0 disables clamping
		self.assertEqual(schema.clamp_future(NOW + timedelta(hours=11), NOW, 0), (NOW + timedelta(hours=11), False))

	def test_skew(self):
		# NOW is 03:00 in Europe/Paris on 2026-10-29 (UTC+1) -> 02:00Z
		self.assertEqual(schema.clock_skew_seconds("2026-10-29T02:02:00Z", "Europe/Paris", NOW), 120)
		self.assertEqual(schema.clock_skew_seconds("2026-10-29T01:59:30Z", "Europe/Paris", NOW), -30)
		self.assertIsNone(schema.clock_skew_seconds(None, "UTC", NOW))
		self.assertIsNone(schema.clock_skew_seconds("garbage", "UTC", NOW))
