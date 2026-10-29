import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent.collector import Collector  # noqa: E402
from wolf_agent.parsers import auth, nginx, scheduler, traceback  # noqa: E402
from wolf_agent.scrub import Scrubber, iban_ok, luhn_ok  # noqa: E402

NOW = datetime(2026, 10, 29, 2, 1, 0, tzinfo=timezone.utc)
S = Scrubber()


class TestRules(unittest.TestCase):
	def test_email(self):
		self.assertEqual(
			S.text("frappe.exceptions.DoesNotExistError: User jane.doe+billing@client-corp.co.uk not found"),
			"frappe.exceptions.DoesNotExistError: User [EMAIL] not found",
		)

	def test_url_encoded_email_in_route(self):
		self.assertEqual(S.text("GET /api/resource/User/jane%40example.com"), "GET /api/resource/User/[EMAIL]")

	def test_administrator_and_decorators_untouched(self):
		line = '    @frappe.whitelist(allow_guest=True)  # Administrator, Guest'
		self.assertEqual(S.text(line), line)

	def test_authorization_header_variants(self):
		self.assertEqual(S.text("Authorization: token 1a2b3c4d5e6f7a8:9f8e7d6c5b4a3f2"), "Authorization: [REDACTED]")
		self.assertEqual(S.text("authorization=Bearer abc.def.ghi123"), "authorization=[REDACTED]")
		self.assertEqual(
			S.text("headers={'Authorization': 'Basic dXNlcjpwYXNzd29yZA==', 'Accept': '*/*'}"),
			"headers={'Authorization': '[REDACTED]', 'Accept': '*/*'}",
		)
		self.assertEqual(S.text('{"Proxy-Authorization": "Basic Zm9vOmJhcg=="}'), '{"Proxy-Authorization": "[REDACTED]"}')

	def test_cookie_header(self):
		self.assertEqual(
			S.text("Cookie: sid=3f1b2c9d0e8a7f6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f; system_user=yes; full_name=Jane"),
			"Cookie: [REDACTED]",
		)
		self.assertEqual(S.text('"Set-Cookie": "sid=abc123; Path=/"'), '"Set-Cookie": "[REDACTED]"')

	def test_bare_scheme_tokens(self):
		self.assertEqual(
			S.text("requests.get(url, headers=h) with token 1a2b3c4d5e6f7a8:9f8e7d6c5b4a3f2 failed"),
			"requests.get(url, headers=h) with token [REDACTED] failed",
		)
		self.assertEqual(S.text("got Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"), "got Bearer [JWT]")
		# plain words after "token" are not credentials
		self.assertEqual(S.text("invalid token expired for user"), "invalid token expired for user")

	def test_query_and_form_values(self):
		self.assertEqual(
			S.text("POST /api/method/login?cmd=login&usr=jane&pwd=S3cr3t!&device=desktop"),
			"POST /api/method/login?cmd=login&usr=jane&pwd=[REDACTED]&device=desktop",
		)
		self.assertEqual(
			S.text("form_dict: {'new_password': 'correct horse battery', 'key': 'abc', 'logout_all_sessions': 1}"),
			"form_dict: {'new_password': '[REDACTED]', 'key': 'abc', 'logout_all_sessions': 1}",
		)
		self.assertEqual(S.text('{"api_secret": "9f8e7d6c5b4a3f2", "api_key": "1a2b3c"}'), '{"api_secret": "[REDACTED]", "api_key": "[REDACTED]"}')
		self.assertEqual(S.text("password=hunter2 access_token=ya29.a0Af"), "password=[REDACTED] access_token=[REDACTED]")

	def test_sid_cookie_value(self):
		self.assertEqual(S.text("session sid=6b8c1e2f3a4d5e6f7a8b9c0d resumed"), "session sid=[REDACTED] resumed")
		# "sid" inside a word is left alone
		self.assertEqual(S.text("consider=yes inside=1"), "consider=yes inside=1")

	def test_cards(self):
		self.assertEqual(S.text("card 4111 1111 1111 1111 declined"), "card [CARD] declined")
		self.assertEqual(S.text("pan=5500-0000-0000-0004;"), "pan=[CARD];")
		self.assertEqual(S.text("amex 378282246310005"), "amex [CARD]")
		# Luhn-invalid, epoch milliseconds, short numbers and timestamps stay
		for keep in ("4111 1111 1111 1112", "ts=1730170800123", "order 4242", "2026-10-29 02:00:13,123"):
			self.assertEqual(S.text(keep), keep)

	def test_iban(self):
		self.assertEqual(S.text("IBAN FR76 3000 6000 0112 3456 7890 189 rejected"), "IBAN [IBAN] rejected")
		self.assertEqual(S.text("to DE89370400440532013000"), "to [IBAN]")
		self.assertEqual(S.text("ref FR76 3000 6000 0112 3456 7890 188"), "ref FR76 3000 6000 0112 3456 7890 188")

	def test_checksums(self):
		self.assertTrue(luhn_ok("4242424242424242"))
		self.assertFalse(luhn_ok("4242424242424241"))
		self.assertTrue(iban_ok("GB82 WEST 1234 5698 7654 32"))
		self.assertFalse(iban_ok("GB82 WEST 1234 5698 7654 33"))

	def test_ips_kept_by_default(self):
		line = "Failed password for root from 203.0.113.7 port 22 ssh2 (2001:db8::1)"
		self.assertEqual(S.text(line), line)

	def test_ips_hashed_when_enabled(self):
		s = Scrubber(ips=True, salt="WAG-0001")
		out = s.text("from 203.0.113.7 and 2001:db8::1 at 2026-10-29 02:00:13 mac aa:bb:cc:dd:ee:ff")
		self.assertNotIn("203.0.113.7", out)
		self.assertNotIn("2001:db8::1", out)
		self.assertIn("02:00:13", out)
		self.assertIn("aa:bb:cc:dd:ee:ff", out)
		self.assertEqual(s.text("203.0.113.7"), s.text("x 203.0.113.7").split(" ")[1])  # stable
		self.assertNotEqual(s.text("203.0.113.7"), Scrubber(ips=True, salt="WAG-0002").text("203.0.113.7"))

	def test_keep_allowlist(self):
		s = Scrubber(keep=["noreply@dokos.io", "@internal.example"])
		self.assertEqual(
			s.text("from noreply@dokos.io to ops@internal.example cc jane@client.com"),
			"from noreply@dokos.io to ops@internal.example cc [EMAIL]",
		)

	def test_extra_patterns(self):
		s = Scrubber(extra_patterns=[r"CUST-\d{6}", {"pattern": r"(?i)siret\s*\d{14}", "replacement": "[SIRET]"}, "(unclosed"])
		self.assertEqual(s.text("customer CUST-123456 SIRET 73282932000074"), "customer [REDACTED] [SIRET]")

	def test_disabled(self):
		s = Scrubber.from_config({"scrub": {"enabled": False}})
		self.assertEqual(s.text("jane@example.com password=x"), "jane@example.com password=x")
		self.assertFalse(Scrubber.from_config({"scrub": False}).enabled)
		self.assertTrue(Scrubber.from_config({}).enabled)

	def test_idempotent(self):
		raw = (
			"Cookie: sid=abc; x=y\nAuthorization: token aaaa1111:bbbb2222\npwd=x&usr=jane@x.io "
			"card 4111111111111111 iban DE89370400440532013000 jwt eyJabcdefg.eyJhijklmn.opqrstuvw"
		)
		once = S.text(raw)
		self.assertEqual(S.text(once), once)
		for secret in ("abc", "aaaa1111", "jane@x.io", "4111111111111111", "DE89", "eyJabcdefg"):
			self.assertNotIn(secret, once)

	def test_event_structure(self):
		ev = {
			"type": "error", "category": "traceback", "severity": "warning", "timestamp": "2026-10-29T02:00:00Z",
			"host": "bench-01", "bench": "b", "site": "a.example.com", "source": "frappe", "fingerprint": "f" * 40,
			"title": "ValidationError: jane@x.io", "payload": {"traceback": "pwd=1", "count": 3, "users": ["a@b.io"], "nested": {"k": ("token abcd1234efgh",)}},
		}
		out = S.event(ev)
		self.assertEqual(out["title"], "ValidationError: [EMAIL]")
		self.assertEqual(out["payload"], {"traceback": "pwd=[REDACTED]", "count": 3, "users": ["[EMAIL]"], "nested": {"k": ["token [REDACTED]"]}})
		for key in ("type", "host", "site", "fingerprint", "timestamp"):
			self.assertEqual(out[key], ev[key])
		self.assertEqual(ev["title"], "ValidationError: jane@x.io")  # input not mutated


FRAPPE_WEB_LOG = """2026-10-29 02:00:13,123 ERROR frappe Error handling request /api/method/frappe.client.get?doctype=User&name=jane%40client.com
Traceback (most recent call last):
  File "apps/frappe/frappe/app.py", line 114, in application
    response = frappe.api.handle(request)
  File "apps/frappe/frappe/auth.py", line 260, in validate_api_key_secret
    frappe.throw(f"Invalid credentials for {api_key}")
frappe.exceptions.AuthenticationError: Invalid API key/secret in Authorization: token 6c3f9d2e1b0a4f8:9e7d5c3b1a2f4e6 from jane@client.com
""".splitlines()


class TestParsersEndToEnd(unittest.TestCase):
	def test_traceback_event_is_scrubbed_and_fingerprint_unchanged(self):
		p = traceback.TracebackParser("bench-01", bench="b", site="a.example.com")
		for line in FRAPPE_WEB_LOG + ["2026-10-29 02:00:14,000 INFO next"]:
			p.feed(line)
		_, raw = p.drain(NOW)
		self.assertEqual(len(raw), 1)
		ev = S.event(raw[0])
		dump = repr(ev)
		for secret in ("6c3f9d2e1b0a4f8", "9e7d5c3b1a2f4e6", "jane@client.com"):
			self.assertNotIn(secret, dump)
		self.assertIn("Authorization: [REDACTED]", ev["payload"]["traceback"])
		self.assertIn("[EMAIL]", ev["title"])
		self.assertEqual(ev["fingerprint"], raw[0]["fingerprint"])
		self.assertIn('File "apps/frappe/frappe/auth.py", line 260', ev["payload"]["traceback"])

	def test_scheduler_sample_lines(self):
		p = scheduler.SchedulerParser("bench-01", bench="b")
		p.feed(
			"2026-10-29 02:00:13,123 ERROR scheduler Exception in Enqueue Events for Site a.example.com "
			"while sending to billing@client.com with card 4242 4242 4242 4242"
		)
		p.feed("2026-10-29 02:00:13,124 INFO scheduler Scheduled Job Failed: app.tasks.nightly for site a.example.com")
		_, raw = p.drain(NOW)
		lines = [line for ev in S.events(raw) for line in ev["payload"]["sample_lines"]]
		self.assertTrue(lines)
		self.assertFalse(any("billing@client.com" in line or "4242 4242" in line for line in lines))

	def test_ssh_ips_survive_by_default(self):
		p = auth.AuthParser("bench-01")
		p.feed("Oct 29 02:00:13 bench-01 sshd[1]: Failed password for root from 198.51.100.4 port 4242 ssh2")
		_, raw = p.drain(NOW)
		ev = S.events(raw)[0]
		self.assertEqual(ev["payload"]["ip"], "198.51.100.4")
		self.assertIn("198.51.100.4", ev["title"])

	def test_nginx_route_email(self):
		p = nginx.NginxParser("bench-01", default_site="a.example.com")
		p.feed(
			'10.0.0.5 - - [29/Oct/2026:03:00:13 +0100] "GET /api/resource/User/jane@client.com HTTP/1.1" '
			'502 157 "-" "curl/8.0"'
		)
		_, raw = p.drain(NOW)
		ev = S.events(raw)[0]
		self.assertNotIn("jane@client.com", repr(ev))
		self.assertEqual(ev["fingerprint"], raw[0]["fingerprint"])


class TestCollectorScrubs(unittest.TestCase):
	def _collect(self, scrub):
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "error.log")
			with open(path, "w") as f:
				f.write("\n".join(FRAPPE_WEB_LOG) + "\n2026-10-29 02:00:14,000 INFO next\n")
			cfg = {
				"hostname": "bench-01", "benches": [], "auth_log": False, "collect_host_metrics": False,
				"platform": "generic", "extra_logs": [{"path": path, "parser": "traceback"}],
			}
			if scrub is not None:
				cfg["scrub"] = scrub
			c = Collector(cfg, from_start=True)
			_, events = c.collect(NOW)
			c.close()
			return events

	def test_default_on(self):
		events = self._collect(None)
		self.assertEqual(len(events), 1)
		self.assertNotIn("jane@client.com", repr(events))

	def test_opt_out(self):
		self.assertIn("jane@client.com", repr(self._collect({"enabled": False})))


if __name__ == "__main__":
	unittest.main()
