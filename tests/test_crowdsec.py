import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from frappe_wolf.wolf import crowdsec

NOW = datetime(2026, 11, 2, 15, 0)
EVENT = {"name": "ev1", "event_type": "security", "category": "web_attack_probe", "severity": "warning", "title": "probes",
         "timestamp": datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc), "site": "erp.example.com", "host": "bench-01",
         "count": 12, "source": "nginx", "payload": {"ip": "203.0.113.50"}}  # fmt: skip


class TestAlerts(unittest.TestCase):
	def test_detection_alert(self):
		a = crowdsec.detection_alert(EVENT)
		self.assertEqual(a["scenario"], "frappe-wolf/web_attack_probe")
		self.assertEqual(a["source"], {"scope": "Ip", "value": "203.0.113.50", "ip": "203.0.113.50"})
		self.assertEqual((a["remediation"], a["events_count"], a["start_at"]), (False, 12, "2026-11-02T14:00:00Z"))
		self.assertNotIn("decisions", a)  # CrowdSec's profiles decide, if anything
		self.assertIn({"key": "wolf_event", "value": "ev1"}, a["events"][0]["meta"])
		self.assertTrue(crowdsec.detection_alert(EVENT, remediation=True)["remediation"])
		self.assertIsNone(crowdsec.detection_alert({**EVENT, "payload": {"ip": "not an ip"}}))

	def test_block_alert_carries_the_decision(self):
		a = crowdsec.block_alert("198.51.100.7", 24, "WRA-1 approved by ops@x", datetime(2026, 11, 2, 15, 0))
		self.assertEqual(a["decisions"], [{"type": "ban", "duration": "24h", "scope": "Ip", "value": "198.51.100.7",
		                                   "origin": "frappe-wolf", "scenario": "frappe-wolf/approved_block"}])  # fmt: skip
		self.assertEqual(a["start_at"], "2026-11-02T15:00:00Z")

	def test_wanted(self):
		self.assertTrue(crowdsec.wanted(EVENT))
		self.assertFalse(crowdsec.wanted({**EVENT, "severity": "info"}))
		self.assertTrue(crowdsec.wanted({**EVENT, "severity": "info"}, "info"))
		self.assertFalse(crowdsec.wanted({**EVENT, "event_type": "error"}))
		self.assertFalse(crowdsec.wanted({**EVENT, "payload": {}}))
		self.assertFalse(crowdsec.wanted({**EVENT, "source": "crowdsec"}))


class TestDecisions(unittest.TestCase):
	def test_durations(self):
		self.assertEqual(crowdsec.parse_duration("3h59m58.5s"), timedelta(hours=3, minutes=59, seconds=58.5))
		self.assertEqual(crowdsec.parse_duration("24h0m0s"), timedelta(hours=24))
		self.assertEqual(crowdsec.parse_duration("150ms"), timedelta(milliseconds=150))
		self.assertEqual(crowdsec.parse_duration("-1m"), timedelta(minutes=-1))
		for bad in ("", "4x", "h", "12"):
			with self.assertRaises(ValueError):
				crowdsec.parse_duration(bad)

	def test_stream_lookup_and_prune(self):
		d = crowdsec.Decisions().apply({"new": [
			{"scope": "Ip", "value": "203.0.113.50", "type": "ban", "origin": "CAPI", "scenario": "crowdsecurity/ssh-bf", "duration": "4h"},
			{"scope": "Range", "value": "198.51.100.0/24", "type": "ban", "origin": "lists", "scenario": "firehol", "duration": "24h"},
			{"scope": "Range", "value": "198.51.100.0/28", "type": "captcha", "origin": "cscli", "scenario": "manual", "duration": "1h"},
			{"scope": "Country", "value": "XX", "type": "ban", "duration": "1h"},
		], "deleted": None}, NOW)  # fmt: skip
		self.assertEqual(sorted(d.items), ["198.51.100.0/24", "198.51.100.0/28", "203.0.113.50"])  # countries skipped
		self.assertEqual(d.lookup("203.0.113.50", NOW)["origin"], "CAPI")
		self.assertEqual(d.lookup("198.51.100.3", NOW)["type"], "captcha")  # the narrowest range
		self.assertEqual(d.lookup("198.51.100.200", NOW)["value"], "198.51.100.0/24")
		self.assertEqual(d.lookup("::ffff:203.0.113.50", NOW)["value"], "203.0.113.50")
		self.assertIsNone(d.lookup("192.0.2.1", NOW))
		self.assertIsNone(d.lookup("203.0.113.50", NOW + timedelta(hours=5)))  # expired
		d.apply({"new": [], "deleted": [{"value": "203.0.113.50"}]}, NOW)
		self.assertIsNone(d.lookup("203.0.113.50", NOW))
		d.prune(NOW + timedelta(hours=2))
		self.assertEqual(sorted(d.items), ["198.51.100.0/24"])
		self.assertEqual(crowdsec.Decisions.from_json(json.loads(json.dumps(d.to_json()))).lookup("198.51.100.9", NOW)["origin"], "lists")

	def test_tag(self):
		d = crowdsec.Decisions().apply({"new": [
			{"scope": "Ip", "value": "203.0.113.50", "type": "ban", "origin": "crowdsec", "scenario": "crowdsecurity/http-probing", "duration": "4h"},
			{"scope": "Ip", "value": "203.0.113.60", "type": "ban", "origin": "frappe-wolf", "scenario": "frappe-wolf/approved_block", "duration": "4h"},
		]}, NOW)  # fmt: skip
		payload, severity = crowdsec.tag({"ip": "203.0.113.50"}, "warning", d, NOW)
		self.assertEqual((payload["crowdsec"]["scenario"], severity), ("crowdsecurity/http-probing", "critical"))
		payload, severity = crowdsec.tag({"ip": "203.0.113.60"}, "warning", d, NOW)
		self.assertEqual((payload["crowdsec"]["origin"], severity), ("frappe-wolf", "warning"))  # Wolf's own block: no raise
		same = {"ip": "192.0.2.1"}
		self.assertIs(crowdsec.tag(same, "info", d, NOW)[0], same)
		self.assertIs(crowdsec.tag(same, "info", None, NOW)[0], same)


class FakeLAPI:
	"""Answers like the LAPI: login with a name/version User-Agent, alerts with a token."""

	def __init__(self):
		self.calls, self.tokens = [], ["t1", "t2"]
		self.expired = set()

	def __call__(self, req, timeout):
		path, ua = req.full_url.split("8080", 1)[1], req.get_header("User-agent")
		self.calls.append((req.get_method(), path, req.get_header("Authorization"), req.get_header("X-api-key")))
		if path == "/v1/watchers/login":
			if "/" not in ua:
				return self.error(401, {"message": "incorrect Username or Password"})
			return io.BytesIO(json.dumps({"code": 200, "token": self.tokens.pop(0)}).encode())
		if path == "/v1/alerts":
			if req.get_header("Authorization") in self.expired:
				return self.error(401, {"message": "token expired"})
			return io.BytesIO(json.dumps([str(i) for i, _ in enumerate(json.loads(req.data), 1)]).encode())
		if path.startswith("/v1/decisions/stream"):
			if req.get_header("X-api-key") != "key":
				return self.error(403, {"message": "access forbidden"})
			return io.BytesIO(json.dumps({"new": [], "deleted": None}).encode())
		return self.error(404, {})

	def error(self, code, body):
		raise urllib.error.HTTPError("http://crowdsec:8080", code, "error", {}, io.BytesIO(json.dumps(body).encode()))


class TestClient(unittest.TestCase):
	def client(self, **kw):
		lapi = FakeLAPI()
		args = {"bouncer_key": "key", "machine_id": "wolf", "password": "pw", "version": "0.1.0", **kw}
		return crowdsec.Client("http://crowdsec:8080/", opener=lapi, **args), lapi

	def test_user_agent_is_name_slash_version(self):
		c, lapi = self.client()
		self.assertEqual(c.push_alerts([crowdsec.detection_alert(EVENT), None]), ["1"])
		self.assertEqual([m for m, *_ in lapi.calls], ["POST", "POST"])
		self.assertEqual(lapi.calls[1][2], "Bearer t1")

	def test_expired_token_logs_in_again(self):
		c, lapi = self.client()
		c.push_alerts([crowdsec.detection_alert(EVENT)])
		lapi.expired.add("Bearer t1")
		self.assertEqual(c.push_alerts([crowdsec.detection_alert(EVENT)]), ["1"])
		self.assertEqual([p for _, p, *_ in lapi.calls].count("/v1/watchers/login"), 2)

	def test_stream_and_errors(self):
		c, lapi = self.client()
		self.assertEqual(c.decisions_stream(startup=True), {"new": [], "deleted": None})
		self.assertEqual(lapi.calls[-1][1], "/v1/decisions/stream?startup=true")
		with self.assertRaisesRegex(crowdsec.CrowdSecError, "HTTP 403"):
			self.client(bouncer_key="wrong")[0].decisions_stream()
		with self.assertRaisesRegex(crowdsec.CrowdSecError, "no machine login"):
			self.client(machine_id=None)[0].push_alerts([crowdsec.detection_alert(EVENT)])
		self.assertEqual(self.client(machine_id=None)[0].push_alerts([None]), [])  # nothing to send: no login


if __name__ == "__main__":
	unittest.main()
