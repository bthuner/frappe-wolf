import json
import threading
import unittest
import urllib.request
from email.header import decode_header
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from frappe_wolf.wolf import channels as c
from frappe_wolf.wolf.channels import gotify, matrix, mattermost, ntfy, pagerduty, slack, sms

ACK = "https://wolf.example.com/api/method/frappe_wolf.api.actions.incident_action?incident=WINC-00012&action=ack&sig=aa"
RESOLVE = ACK.replace("action=ack", "action=resolve")


def msg(**kw):
	base = {
		"kind": "fired",
		"label": "FIRING",
		"severity": "critical",
		"status": "Open",
		"title": "502 spike after deployment: site a.example.com",
		"name": "WINC-00012",
		"alert_rule": "502 spike after deployment",
		"subject": "[Wolf] FIRING CRITICAL: 502 spike after deployment: site a.example.com",
		"subject_html": "[Wolf] FIRING CRITICAL: 502 spike after deployment: site a.example.com",
		"text": "FIRING · critical · 502 spike\nScope: a.example.com / main\nhttps://wolf.example.com/app/wolf-incident/WINC-00012",
		"html": "FIRING · critical · 502 spike<br>Scope: a.example.com / main",
		"url": "https://wolf.example.com/app/wolf-incident/WINC-00012",
		"scope": "a.example.com / main / web1",
		"summary": "sum(http_502) = 42 (>= 10) over 5 min",
		"started_at": "2026-10-30 02:00:00",
		"site": "a.example.com",
		"bench": "main",
		"host": "web1",
		"lines": ["Scope: a.example.com / main / web1", "Started: 2026-10-30 02:00:00", "Details: sum(http_502) = 42"],
		"actions": {"ack": ACK, "resolve": RESOLVE},
		"attempt_id": "abc123",
	}
	base.update(kw)
	return base


class FakeServer:
	"""Local stand-in for Slack, ntfy, PagerDuty…: records what it receives."""

	def __init__(self, status=200):
		self.requests = []
		outer = self

		class Handler(BaseHTTPRequestHandler):
			def _handle(self):
				length = int(self.headers.get("Content-Length") or 0)
				outer.requests.append(
					{"method": self.command, "path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length)}
				)
				self.send_response(status)
				self.end_headers()
				self.wfile.write(b"{}")

			do_POST = do_PUT = do_GET = _handle

			def log_message(self, *args):
				pass

		self.server = HTTPServer(("127.0.0.1", 0), Handler)
		self.url = f"http://127.0.0.1:{self.server.server_port}"
		threading.Thread(target=self.server.serve_forever, daemon=True).start()

	def close(self):
		self.server.shutdown()
		self.server.server_close()


def send(req):
	"""Send a built request the way notify.http_request does (stdlib only)."""
	body = json.dumps(req["json"]).encode() if "json" in req else req.get("data")
	headers = dict(req.get("headers") or {})
	if "json" in req:
		headers["Content-Type"] = "application/json"
	r = urllib.request.Request(req["url"], data=body, method=req["method"], headers=headers)
	with urllib.request.urlopen(r, timeout=5) as response:
		return response.status


class TestHelpers(unittest.TestCase):
	def test_truncate_word_boundary(self):
		self.assertEqual(c.truncate("short", 10), "short")
		out = c.truncate("the quick brown fox jumps over", 16)
		self.assertLessEqual(len(out), 16)
		self.assertEqual(out, "the quick brown…")
		self.assertEqual(c.truncate("abcdefghij", 5, "..."), "ab...")
		self.assertEqual(c.truncate("abc", 0), "")

	def test_redact_secrets_and_url_paths(self):
		err = (
			"HTTPSConnectionPool(host='hooks.slack.com', port=443): Max retries exceeded with url: "
			"/services/T0/B0/XYZSECRET (Caused by https://hooks.slack.com/services/T0/B0/XYZSECRET) token tk_abc123"
		)
		out = c.redact(err, secrets=["tk_abc123", "https://hooks.slack.com/services/T0/B0/XYZSECRET"])
		self.assertNotIn("XYZSECRET", out)
		self.assertNotIn("tk_abc123", out)
		self.assertIn("hooks.slack.com", out)

	def test_redact_url_encoded_secret_and_query(self):
		out = c.redact("GET https://sms.example.com/send?key=p%40ss%2Fword&to=1 failed: p@ss/word", secrets=["p@ss/word"])
		self.assertNotIn("p@ss", out)
		self.assertNotIn("p%40ss", out)
		self.assertIn("https://sms.example.com/***", out)

	def test_redact_echoed_path_of_secret_url(self):
		out = c.redact("HTTP 400: no such hook /services/T0/B0/XYZSECRET", secrets=["https://hooks.slack.com/services/T0/B0/XYZSECRET"])
		self.assertEqual(out, "HTTP 400: no such hook ***")

	def test_redact_ignores_tiny_values(self):
		self.assertEqual(c.redact("error 500", secrets=["", None, "5"]), "error 500")

	def test_identity(self):
		a = c.identity("PagerDuty", {"pagerduty_routing_key": "R0UTINGKEY123", "pagerduty_url": None})
		b = c.identity("PagerDuty", {"pagerduty_routing_key": "R0UTINGKEY123", "pagerduty_url": ""})
		other = c.identity("PagerDuty", {"pagerduty_routing_key": "OTHERKEY456"})
		self.assertEqual(a, b)
		self.assertNotEqual(a, other)
		self.assertTrue(a.startswith("h:"))
		self.assertNotIn("R0UTINGKEY123", a)
		self.assertNotEqual(a, c.identity("Slack", {"chat_webhook_url": "R0UTINGKEY123"}))

	def test_target_label_has_no_secret(self):
		self.assertEqual(c.target_label("Slack", {"chat_webhook_url": "https://hooks.slack.com/services/T/B/SECRET"}), "hooks.slack.com")
		self.assertEqual(
			c.target_label("Matrix", {"matrix_homeserver": "https://m.example.org", "matrix_room_id": "!r:example.org",
			                          "matrix_access_token": "syt_secret"}),
			"!r:example.org on m.example.org",
		)
		self.assertEqual(c.target_label("ntfy", {"ntfy_topic": "wolf-ops", "ntfy_token": "tk_x"}), "ntfy.sh/wolf-ops")
		self.assertEqual(c.target_label("SMS", {"sms_url": "https://gw.example.com/x?k={secret}", "sms_numbers": "+33 6 12 34 56 78"}),
		                 "gw.example.com → …5678")
		self.assertEqual(c.target_label("PagerDuty", {"pagerduty_routing_key": "k"}), "events.pagerduty.com")

	def test_missing_and_invalid_urls(self):
		self.assertEqual(c.missing("Matrix", {"matrix_homeserver": "https://m", "matrix_room_id": " "}), ["Room ID", "Access Token"])
		self.assertEqual(c.missing("ntfy", {"ntfy_topic": "t"}), [])
		self.assertEqual(c.invalid_urls({"gotify_server": "gotify.local", "sms_url": "https://x/{to}"}), ["gotify_server"])
		self.assertEqual(c.invalid_urls({"chat_webhook_url": "*****"}), [], "a saved password reads as asterisks")

	def test_worst_severity(self):
		self.assertEqual(c.worst_severity(["info", "warning", "info"]), "warning")
		self.assertEqual(c.worst_severity([]), "info")

	def test_builder_registry(self):
		for t in c.NATIVE:
			self.assertTrue(callable(c.builder(t)), t)
		self.assertIsNone(c.builder("Email"))


class TestSelect(unittest.TestCase):
	EMAIL = {"channel_type": "Email", "enabled": 1}
	PD = {"channel_type": "PagerDuty", "enabled": 1, "key": "k1"}
	PD_OFF = {"channel_type": "PagerDuty", "enabled": 0}
	ESC_PD = {"channel_type": "PagerDuty", "enabled": 1, "key": "k2"}
	ESC_PD_SAME = {"channel_type": "PagerDuty", "enabled": 1, "key": "k1"}
	ESC_SMS = {"channel_type": "SMS", "enabled": 1}

	def sel(self, kind, **kw):
		chans = kw.pop("channels", [self.EMAIL, self.PD, self.PD_OFF])
		esc = kw.pop("escalation", [self.ESC_PD, self.ESC_SMS])
		return c.select(kind, chans, esc, key=lambda r: r.get("key") or id(r), **kw)

	def test_fired_goes_to_enabled_channels(self):
		self.assertEqual(self.sel("fired"), [self.EMAIL, self.PD])

	def test_escalated_goes_to_escalation_channels(self):
		self.assertEqual(self.sel("escalated"), [self.ESC_PD, self.ESC_SMS])

	def test_acknowledged_only_stateful(self):
		self.assertEqual(self.sel("acknowledged"), [self.PD])
		self.assertEqual(self.sel("acknowledged", escalated=True), [self.PD, self.ESC_PD])

	def test_resolved(self):
		self.assertEqual(self.sel("resolved"), [self.EMAIL, self.PD])
		self.assertEqual(self.sel("resolved", only_stateful=True), [self.PD])
		self.assertEqual(self.sel("resolved", only_stateful=True, escalated=True), [self.PD, self.ESC_PD])

	def test_same_destination_once(self):
		out = self.sel("resolved", escalation=[self.ESC_PD_SAME], escalated=True)
		self.assertEqual(out, [self.EMAIL, self.PD])

	def test_no_stateful(self):
		self.assertEqual(self.sel("acknowledged", channels=[self.EMAIL]), [])


class TestSlack(unittest.TestCase):
	CFG = {"chat_webhook_url": "https://hooks.slack.com/services/T/B/X"}

	def test_payload(self):
		(req,) = slack.build_payload(msg(title="a <b> & c"), self.CFG)
		self.assertEqual((req["method"], req["url"]), ("POST", self.CFG["chat_webhook_url"]))
		body = req["json"]
		self.assertEqual(body["text"], msg()["subject"])
		att = body["attachments"][0]
		self.assertEqual(att["color"], c.COLORS["critical"])
		section, actions = att["blocks"]
		self.assertEqual(section["type"], "section")
		self.assertEqual(section["text"]["type"], "mrkdwn")
		self.assertIn("<https://wolf.example.com/app/wolf-incident/WINC-00012|a &lt;b&gt; &amp; c>", section["text"]["text"])
		self.assertEqual(actions["type"], "actions")
		buttons = actions["elements"]
		self.assertEqual([b["action_id"] for b in buttons], ["wolf_open", "wolf_ack", "wolf_resolve"])
		self.assertTrue(all(b["type"] == "button" and b["text"]["type"] == "plain_text" for b in buttons))
		self.assertEqual(buttons[1]["url"], ACK)
		self.assertEqual(buttons[1]["style"], "primary")
		self.assertNotIn("style", buttons[2])

	def test_resolved_is_green_without_actions(self):
		(req,) = slack.build_payload(msg(kind="resolved", label="RESOLVED", actions={}), self.CFG)
		att = req["json"]["attachments"][0]
		self.assertEqual(att["color"], c.KIND_COLORS["resolved"])
		self.assertEqual([b["action_id"] for b in att["blocks"][1]["elements"]], ["wolf_open"])

	def test_limits(self):
		(req,) = slack.build_payload(msg(lines=["x" * 5000], title="t" * 200), self.CFG)
		blocks = req["json"]["attachments"][0]["blocks"]
		self.assertLessEqual(len(blocks[0]["text"]["text"]), slack.SECTION_LIMIT)
		self.assertTrue(all(len(b["text"]["text"]) <= 75 for b in blocks[1]["elements"]))

	def test_warning_color(self):
		(req,) = slack.build_payload(msg(severity="warning"), self.CFG)
		self.assertEqual(req["json"]["attachments"][0]["color"], c.COLORS["warning"])


class TestMattermost(unittest.TestCase):
	def test_payload(self):
		(req,) = mattermost.build_payload(msg(), {"chat_webhook_url": "https://mm.example.com/hooks/abc"})
		att = req["json"]["attachments"][0]
		self.assertEqual(att["fallback"], msg()["subject"])
		self.assertEqual(att["color"], c.COLORS["critical"])
		self.assertEqual(att["title"], msg()["title"])
		self.assertEqual(att["title_link"], msg()["url"])
		self.assertIn(f"[Acknowledge]({ACK})", att["text"])
		self.assertIn(f"[Resolve]({RESOLVE})", att["text"])
		self.assertEqual({f["title"] for f in att["fields"]}, {"Severity", "Scope"})
		self.assertTrue(all(isinstance(f["short"], bool) for f in att["fields"]))
		self.assertIn("WINC-00012", att["footer"])

	def test_no_links_when_resolved(self):
		(req,) = mattermost.build_payload(msg(kind="resolved", actions={}), {"chat_webhook_url": "https://mm/x"})
		self.assertNotIn("Acknowledge", req["json"]["attachments"][0]["text"])


class TestMatrix(unittest.TestCase):
	CFG = {"matrix_homeserver": "https://matrix.example.org/", "matrix_room_id": "!abc:example.org",
	       "matrix_access_token": "syt_token"}

	def test_payload(self):
		(req,) = matrix.build_payload(msg(), self.CFG)
		self.assertEqual(req["method"], "PUT")
		self.assertEqual(
			req["url"],
			"https://matrix.example.org/_matrix/client/v3/rooms/%21abc%3Aexample.org/send/m.room.message/wolf-abc123",
		)
		self.assertEqual(req["headers"]["Authorization"], "Bearer syt_token")
		body = req["json"]
		self.assertEqual(body["msgtype"], "m.text")
		self.assertEqual(body["format"], "org.matrix.custom.html")
		self.assertEqual(body["body"], msg()["text"])
		self.assertIn("<strong>", body["formatted_body"])
		self.assertIn(msg()["html"], body["formatted_body"])

	def test_txn_id_differs_per_attempt(self):
		a = matrix.build_payload(msg(attempt_id="one"), self.CFG)[0]["url"]
		b = matrix.build_payload(msg(attempt_id="two"), self.CFG)[0]["url"]
		self.assertNotEqual(a, b)


class TestNtfy(unittest.TestCase):
	def test_payload(self):
		(req,) = ntfy.build_payload(msg(), {"ntfy_server": "https://ntfy.example.com/", "ntfy_topic": "wolf-ops",
		                                    "ntfy_token": "tk_secret"})
		self.assertEqual((req["method"], req["url"]), ("POST", "https://ntfy.example.com/wolf-ops"))
		h = req["headers"]
		self.assertEqual(h["Title"], msg()["subject"])
		self.assertEqual(h["Priority"], "5")
		self.assertEqual(h["Tags"], "rotating_light,critical")
		self.assertEqual(h["Click"], msg()["url"])
		self.assertEqual(h["Authorization"], "Bearer tk_secret")
		actions = h["Actions"].split("; ")
		self.assertEqual(len(actions), 3)
		self.assertEqual(actions[0], f"view, Open, {msg()['url']}")
		self.assertEqual(actions[1], f"view, Acknowledge, {ACK}, clear=true")
		self.assertEqual(req["data"], msg()["text"].encode())

	def test_default_server_no_token(self):
		(req,) = ntfy.build_payload(msg(actions={}), {"ntfy_topic": "t"})
		self.assertEqual(req["url"], "https://ntfy.sh/t")
		self.assertNotIn("Authorization", req["headers"])
		self.assertEqual(req["headers"]["Actions"], f"view, Open, {msg()['url']}")

	def test_priorities(self):
		self.assertEqual(ntfy.priority("warning", "fired"), 4)
		self.assertEqual(ntfy.priority("info", "fired"), 3)
		self.assertEqual(ntfy.priority("warning", "escalated"), 5)
		self.assertEqual(ntfy.priority("critical", "escalated"), 5)
		self.assertEqual(ntfy.priority("critical", "resolved"), 2)

	def test_non_ascii_title_is_rfc2047(self):
		(req,) = ntfy.build_payload(msg(subject="[Wolf] FIRING: café → down"), {"ntfy_topic": "t"})
		title = req["headers"]["Title"]
		self.assertTrue(title.isascii())
		(raw, charset), = decode_header(title)
		self.assertEqual(raw.decode(charset), "[Wolf] FIRING: café → down")
		self.assertEqual(ntfy.header_value("a\nb"), "a b")

	def test_body_limit(self):
		(req,) = ntfy.build_payload(msg(text="x " * 5000), {"ntfy_topic": "t"})
		self.assertLessEqual(len(req["data"].decode()), ntfy.BODY_LIMIT)


class TestGotify(unittest.TestCase):
	def test_payload(self):
		(req,) = gotify.build_payload(msg(), {"gotify_server": "https://push.example.com", "gotify_token": "AppTok"})
		self.assertEqual((req["method"], req["url"]), ("POST", "https://push.example.com/message"))
		self.assertEqual(req["headers"], {"X-Gotify-Key": "AppTok"})
		body = req["json"]
		self.assertEqual(body["title"], msg()["subject"])
		self.assertEqual(body["message"], msg()["text"])
		self.assertEqual(body["priority"], 8)
		self.assertEqual(body["extras"]["client::notification"]["click"]["url"], msg()["url"])
		self.assertEqual(body["extras"]["client::display"]["contentType"], "text/plain")

	def test_priorities(self):
		self.assertEqual(gotify.priority("warning", "fired"), 5)
		self.assertEqual(gotify.priority("info", "fired"), 3)
		self.assertEqual(gotify.priority("critical", "escalated"), 10)
		self.assertEqual(gotify.priority("critical", "resolved"), 2)
		self.assertEqual(gotify.priority("critical", "test"), 4)


class TestSmsText(unittest.TestCase):
	def test_short_message_keeps_url(self):
		text = sms.sms_text(msg(title="Disk full", scope="web1"))
		self.assertEqual(text, f"Wolf FIRING critical: Disk full (web1) {msg()['url']}")
		self.assertLessEqual(sms.gsm_length(text), 160)

	def test_long_title_is_cut_before_url(self):
		text = sms.sms_text(msg(title="word " * 80))
		self.assertTrue(text.endswith(" " + msg()["url"]))
		self.assertIn("...", text)
		self.assertLessEqual(sms.gsm_length(text), 160)

	def test_two_segments(self):
		text = sms.sms_text(msg(title="word " * 80), 320)
		self.assertLessEqual(sms.gsm_length(text), 306)
		self.assertGreater(len(text), 160)

	def test_long_url_dropped(self):
		text = sms.sms_text(msg(url="https://wolf.example.com/" + "x" * 100))
		self.assertNotIn("https://", text)
		self.assertLessEqual(len(text), 160)

	def test_fold_to_gsm(self):
		self.assertEqual(sms.fit("a · b … c → d"), "a - b ... c -> d")
		self.assertIsNotNone(sms.gsm_length(sms.fit("a · b … c → d")))

	def test_extension_characters_count_twice(self):
		text = sms.fit("€" * 100)
		self.assertLessEqual(sms.gsm_length(text), 160)
		self.assertLessEqual(len(text), 81)

	def test_unicode_uses_ucs2_limits(self):
		self.assertLessEqual(len(sms.fit("🔥 " + "x" * 200)), 70)
		self.assertLessEqual(len(sms.fit("🔥 " + "x" * 400, 320)), 134)

	def test_invalid_length_falls_back(self):
		self.assertLessEqual(len(sms.fit("x" * 400, "abc")), 160)
		self.assertLessEqual(len(sms.fit("x" * 400, 999)), 160)

	def test_gsm_length(self):
		self.assertEqual(sms.gsm_length("abc"), 3)
		self.assertEqual(sms.gsm_length("a€"), 3)
		self.assertIsNone(sms.gsm_length("✓"))


class TestSmsPayload(unittest.TestCase):
	def test_get_template(self):
		reqs = sms.build_payload(
			msg(title="Disk full", scope="web1"),
			{"sms_numbers": "+33612345678, +33700000000", "sms_method": "GET",
			 "sms_url": "https://gw.example.com/send?to={to}&msg={text}&key={secret}", "sms_secret": "k&y"},
		)
		self.assertEqual(len(reqs), 2)
		q = parse_qs(urlsplit(reqs[0]["url"]).query)
		self.assertEqual(q["to"], ["+33612345678"])
		self.assertEqual(q["msg"], [sms.sms_text(msg(title="Disk full", scope="web1"))])
		self.assertEqual(q["key"], ["k&y"])
		self.assertNotIn("data", reqs[0])
		self.assertNotIn("33612345678", reqs[0]["action"])

	def test_post_form_default_body(self):
		(req,) = sms.build_payload(msg(), {"sms_numbers": "+1555", "sms_url": "https://gw/x",
		                                   "sms_auth_header": "Authorization: Bearer {secret}", "sms_secret": "S3"})
		self.assertEqual(req["method"], "POST")
		self.assertEqual(req["headers"]["Authorization"], "Bearer S3")
		self.assertEqual(req["headers"]["Content-Type"], "application/x-www-form-urlencoded")
		self.assertEqual(parse_qs(req["data"].decode())["to"], ["+1555"])

	def test_post_json_template_escapes(self):
		(req,) = sms.build_payload(
			msg(title='say "hi"'),
			{"sms_numbers": "+1555", "sms_url": "https://gw/x", "sms_content_type": "json",
			 "sms_body": '{"to": "{to}", "message": "{text}", "auth": "{secret}"}', "sms_secret": "S3"},
		)
		body = json.loads(req["data"])
		self.assertEqual(body["to"], "+1555")
		self.assertIn('say "hi"', body["message"])
		self.assertEqual(body["auth"], "S3")
		self.assertEqual(req["headers"]["Content-Type"], "application/json")

	def test_json_default_body(self):
		(req,) = sms.build_payload(msg(), {"sms_numbers": "+1", "sms_url": "https://gw/x", "sms_content_type": "json"})
		self.assertEqual(set(json.loads(req["data"])), {"to", "text"})

	def test_secret_placeholder_in_alert_text_is_not_expanded(self):
		(req,) = sms.build_payload(
			msg(title="{secret} {to}"),
			{"sms_numbers": "+1", "sms_url": "https://gw/x?m={text}", "sms_method": "GET", "sms_secret": "TOPSECRET"},
		)
		self.assertNotIn("TOPSECRET", unquote(req["url"]))
		self.assertIn("{secret}", unquote(req["url"]))

	def test_form_body_template(self):
		(req,) = sms.build_payload(
			msg(title="a&b"), {"sms_numbers": "+1", "sms_url": "https://gw/x", "sms_body": "dst={to}&txt={text}"}
		)
		self.assertIn("a&b", parse_qs(req["data"].decode())["txt"][0])


class TestPagerDuty(unittest.TestCase):
	CFG = {"pagerduty_routing_key": "R0UTE"}

	def test_trigger(self):
		(req,) = pagerduty.build_payload(msg(), self.CFG)
		self.assertEqual((req["method"], req["url"], req["action"]), ("POST", pagerduty.DEFAULT_URL, "trigger"))
		body = req["json"]
		self.assertEqual(body["routing_key"], "R0UTE")
		self.assertEqual(body["event_action"], "trigger")
		self.assertEqual(body["dedup_key"], "WINC-00012")
		p = body["payload"]
		self.assertEqual(p["severity"], "critical")
		self.assertEqual(p["source"], "web1")
		self.assertIn(msg()["title"], p["summary"])
		self.assertEqual(p["component"], "a.example.com")
		self.assertEqual(p["group"], "main")
		self.assertEqual(p["custom_details"]["url"], msg()["url"])
		self.assertEqual(body["links"][0]["href"], msg()["url"])
		self.assertIn(p["severity"], ("critical", "error", "warning", "info"))

	def test_trigger_kinds(self):
		for kind in ("fired", "reopened", "escalated"):
			(req,) = pagerduty.build_payload(msg(kind=kind), self.CFG)
			self.assertEqual(req["json"]["event_action"], "trigger", kind)

	def test_acknowledge_and_resolve(self):
		for kind, action in (("acknowledged", "acknowledge"), ("resolved", "resolve")):
			(req,) = pagerduty.build_payload(msg(kind=kind), self.CFG)
			self.assertEqual(req["json"], {"routing_key": "R0UTE", "event_action": action, "dedup_key": "WINC-00012"})

	def test_test_triggers_then_resolves(self):
		reqs = pagerduty.build_payload(msg(kind="test"), self.CFG)
		self.assertEqual([r["json"]["event_action"] for r in reqs], ["trigger", "resolve"])
		self.assertEqual({r["json"]["dedup_key"] for r in reqs}, {"WINC-00012"})

	def test_custom_url_and_digest(self):
		(req,) = pagerduty.build_payload(msg(), {**self.CFG, "pagerduty_url": "https://oncall.example.com/integrations/v1/x/"})
		self.assertEqual(req["url"], "https://oncall.example.com/integrations/v1/x/")
		self.assertEqual(pagerduty.build_payload(msg(kind="digest"), self.CFG), [])

	def test_summary_limit_and_source_fallback(self):
		(req,) = pagerduty.build_payload(msg(title="x" * 2000, host=None, site=None, bench=None), self.CFG)
		p = req["json"]["payload"]
		self.assertLessEqual(len(p["summary"]), 1024)
		self.assertEqual(p["source"], "frappe-wolf")
		self.assertNotIn("component", p)


class TestAgainstLocalServer(unittest.TestCase):
	"""Every builder's request goes through a real HTTP stack (local fake, no internet)."""

	def setUp(self):
		self.fake = FakeServer()
		self.addCleanup(self.fake.close)

	def test_each_channel_round_trip(self):
		u = self.fake.url
		cases = [
			(slack, {"chat_webhook_url": f"{u}/services/T/B/X"}),
			(mattermost, {"chat_webhook_url": f"{u}/hooks/abc"}),
			(matrix, {"matrix_homeserver": u, "matrix_room_id": "!r:example.org", "matrix_access_token": "syt"}),
			(ntfy, {"ntfy_server": u, "ntfy_topic": "wolf", "ntfy_token": "tk"}),
			(gotify, {"gotify_server": u, "gotify_token": "AppTok"}),
			(sms, {"sms_numbers": "+1555", "sms_url": f"{u}/sms?to={{to}}", "sms_auth_header": "X-Key: {secret}",
			       "sms_secret": "S"}),
			(pagerduty, {"pagerduty_url": f"{u}/v2/enqueue", "pagerduty_routing_key": "R"}),
		]
		m = msg(subject="[Wolf] FIRING CRITICAL: café down")
		for module, cfg in cases:
			for req in module.build_payload(m, cfg):
				self.assertEqual(send(req), 200, module.__name__)
		got = self.fake.requests
		self.assertEqual(len(got), len(cases))
		self.assertEqual(got[2]["method"], "PUT")
		self.assertTrue(got[2]["path"].startswith("/_matrix/client/v3/rooms/%21r%3Aexample.org/send/m.room.message/"))
		self.assertEqual(got[3]["headers"]["Priority"], "5")
		decoded = decode_header(got[3]["headers"]["Title"])[0]
		self.assertEqual(decoded[0].decode(decoded[1]), "[Wolf] FIRING CRITICAL: café down")
		self.assertEqual(json.loads(got[4]["body"])["priority"], 8)
		self.assertEqual(got[5]["headers"]["X-Key"], "S")
		self.assertEqual(json.loads(got[6]["body"])["dedup_key"], "WINC-00012")
