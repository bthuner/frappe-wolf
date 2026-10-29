import json
import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import forwarding, schema

NOW = datetime(2026, 10, 29, 3, 0, 0)
SCOPE = {"host": "bench-01", "bench": "frappe-bench", "site": "b.example.com"}


def normalized(**overrides):
	raw = {
		**SCOPE,
		"type": "cron_failure",
		"title": "Scheduled job failed: app.tasks.nightly",
		"severity": "critical",
		"category": "scheduled_job",
		"source": "scheduler",
		"payload": {"method": "app.tasks.nightly", "traceback": "Traceback ..."},
		"timestamp": "2026-10-29T01:00:13Z",
		**overrides,
	}
	return schema.normalize_event(raw, "Europe/Paris", NOW)


class TestPayloadShaping(unittest.TestCase):
	def test_to_utc_iso(self):
		self.assertEqual(forwarding.to_utc_iso(datetime(2026, 10, 29, 2, 0, 13), "Europe/Paris"), "2026-10-29T01:00:13Z")
		# Summer time: UTC+2
		self.assertEqual(forwarding.to_utc_iso(datetime(2026, 7, 1, 4, 0, 0), "Europe/Paris"), "2026-07-01T02:00:00Z")
		self.assertEqual(forwarding.to_utc_iso("2026-10-29 02:00:13.123456", "UTC"), "2026-10-29T02:00:13Z")

	def test_protocol_event(self):
		data = normalized()
		event = forwarding.protocol_event(data, "Europe/Paris")
		self.assertEqual(event["type"], "cron_failure")
		self.assertEqual(event["timestamp"], "2026-10-29T01:00:13Z")
		self.assertEqual({k: event[k] for k in SCOPE}, SCOPE)
		self.assertEqual(event["payload"]["method"], "app.tasks.nightly")
		self.assertEqual(event["fingerprint"], data["fingerprint"])
		self.assertNotIn("count", event)
		json.dumps(event)  # must be serializable as-is

	def test_round_trip_through_central(self):
		"""What the central site stores equals what the satellite would have stored itself."""
		data = normalized(fingerprint="fp-1")
		event = forwarding.with_count(forwarding.protocol_event(data, "Europe/Paris"), 3)
		# central runs in another timezone and has its own default host
		central = schema.normalize_event(event, "UTC", NOW, default_host="central-host")
		for key in ("event_type", "category", "severity", "source", "title", "fingerprint", "host", "bench", "site"):
			self.assertEqual(central[key], data[key], key)
		self.assertEqual(central["timestamp"], datetime(2026, 10, 29, 1, 0, 13))
		self.assertEqual(central["count"], 3)
		self.assertEqual(json.loads(central["payload"]), json.loads(data["payload"]))

	def test_payload_edge_cases(self):
		self.assertIsNone(forwarding.protocol_event(normalized(payload=None), "UTC")["payload"])
		data = normalized()
		data["payload"] = "not json"
		self.assertEqual(forwarding.protocol_event(data, "UTC")["payload"], {"value": "not json"})

	def test_with_count(self):
		self.assertEqual(forwarding.with_count({"type": "error"}, None)["count"], 1)
		self.assertEqual(forwarding.with_count({"type": "error"}, 0)["count"], 1)
		self.assertEqual(forwarding.with_count({"type": "error"}, 4)["count"], 4)

	def test_push_body(self):
		body = forwarding.push_body([{"type": "error"}], "bench-01", "0.1.0", sent_at="2026-10-29T02:00:30Z")
		self.assertEqual(
			body,
			{"sent_at": "2026-10-29T02:00:30Z", "agent_version": "0.1.0", "hostname": "bench-01", "metrics": [], "events": [{"type": "error"}]},
		)
		self.assertTrue(forwarding.push_body([], "h", "v")["sent_at"].endswith("Z"))

	def test_register_body(self):
		body = forwarding.register_body("tok", SCOPE, "0.1.0", "Linux", "/home/frappe/frappe-bench")
		self.assertEqual(body["kind"], "satellite")
		self.assertEqual(body["hostname"], "b.example.com")
		self.assertEqual(body["host"], "bench-01")
		self.assertEqual(body["registration_token"], "tok")
		self.assertEqual(body["benches"][0]["sites"], ["b.example.com"])
		self.assertEqual(body["benches"][0]["name"], "frappe-bench")


class TestBatching(unittest.TestCase):
	def test_chunks(self):
		self.assertEqual(forwarding.chunks([], 3), [])
		self.assertEqual(forwarding.chunks(list(range(7)), 3), [[0, 1, 2], [3, 4, 5], [6]])
		self.assertEqual(forwarding.chunks(list(range(10)), 3, limit=2), [[0, 1, 2], [3, 4, 5]])

	def test_defaults_fit_central_limits(self):
		self.assertLessEqual(forwarding.BATCH_SIZE, 1000)  # default max_events_per_push
		self.assertEqual(len(forwarding.chunks(list(range(5000)))), forwarding.MAX_BATCHES_PER_RUN)

	def test_overflow(self):
		self.assertEqual(forwarding.overflow(10, cap=20), 0)
		self.assertEqual(forwarding.overflow(25, cap=20), 5)


class TestBackoff(unittest.TestCase):
	def test_schedule(self):
		self.assertEqual([forwarding.backoff_seconds(n) for n in range(7)], [0, 60, 120, 240, 480, 900, 900])
		self.assertEqual(forwarding.backoff_seconds(50), forwarding.MAX_BACKOFF)

	def test_next_attempt(self):
		self.assertEqual(forwarding.next_attempt(NOW, 2), NOW + timedelta(minutes=2))
		self.assertEqual(forwarding.next_attempt(NOW, 1, outcome="auth"), NOW + timedelta(seconds=forwarding.MAX_BACKOFF))

	def test_classify_status(self):
		self.assertEqual(forwarding.classify_status(200), "ok")
		self.assertEqual(forwarding.classify_status(401), "auth")
		self.assertEqual(forwarding.classify_status(403), "auth")
		self.assertEqual(forwarding.classify_status(417), "rejected")  # frappe.ValidationError
		self.assertEqual(forwarding.classify_status(429), "retry")
		self.assertEqual(forwarding.classify_status(408), "retry")
		self.assertEqual(forwarding.classify_status(502), "retry")


class TestErrors(unittest.TestCase):
	def test_server_messages(self):
		body = json.dumps({"_server_messages": json.dumps([json.dumps({"message": "Invalid registration token"})])})
		self.assertEqual(forwarding.error_detail(body), "Invalid registration token")

	def test_exception_and_plain_text(self):
		self.assertEqual(
			forwarding.error_detail(json.dumps({"exception": "frappe.exceptions.AuthenticationError"})),
			"frappe.exceptions.AuthenticationError",
		)
		self.assertEqual(forwarding.error_detail("<html>502 Bad Gateway</html>\n"), "<html>502 Bad Gateway</html>")
		self.assertEqual(forwarding.error_detail(None), "")
		self.assertEqual(len(forwarding.error_detail("x" * 2000)), 500)

	def test_central_url(self):
		self.assertEqual(forwarding.normalize_central_url(" https://wolf.example.com/ "), "https://wolf.example.com")
		self.assertEqual(forwarding.normalize_central_url("http://wolf.localhost:8000"), "http://wolf.localhost:8000")
		for bad in ("", "wolf.example.com", "ftp://wolf.example.com", "https://"):
			with self.assertRaises(ValueError):
				forwarding.normalize_central_url(bad)


if __name__ == "__main__":
	unittest.main()
