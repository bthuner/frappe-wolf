import base64
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import cli, fallback  # noqa: E402
from wolf_agent.fallback import FallbackError  # noqa: E402
from wolf_agent.watchdog import RETRY_SECONDS, Watchdog, assess  # noqa: E402


class Clock:
	def __init__(self):
		self.t = 1_800_000_000.0

	def __call__(self):
		return self.t

	def advance(self, minutes):
		self.t += minutes * 60


class Sender:
	"""Records messages; `fail` makes the next sends raise like a dead channel."""

	def __init__(self):
		self.sent = []
		self.fail = False

	def __call__(self, subject, text):
		if self.fail:
			raise FallbackError("channel down")
		self.sent.append((subject, text))
		return ["fake"]

	@property
	def subjects(self):
		return [s for s, _ in self.sent]


def health(rules_age=30, **extra):
	return {"ok": True, "rules": {"age_seconds": rules_age}, "scheduler": {"enabled": True, "age_seconds": 20}, **extra}


CFG = {"url": "https://wolf.example.com/", "hostname": "bench-01", "watchdog": {"stale_minutes": 5}}


class TestAssess(unittest.TestCase):
	def test_assess(self):
		self.assertEqual(assess(health()), [])
		self.assertEqual(assess(None), [])
		self.assertEqual(assess({}), [])
		self.assertEqual(assess(health(rules_age=360)), ["alert rules not evaluated for 6 min"])
		self.assertEqual(assess(health(rules_age=360), stale_minutes=10), [])
		self.assertIn("in the future", assess(health(rules_age=-3600))[0])
		h = {"rules": {"age_seconds": None}, "scheduler": {"age_seconds": 900, "enabled": False, "reason": "pause_scheduler"}}
		self.assertEqual(assess(h), ["scheduled jobs not evaluated for 15 min; scheduler disabled (pause_scheduler)"])
		self.assertIn("job queue unreachable: refused", assess(health(rules_age=400, queues={"error": "refused"}))[0])


class TestWatchdog(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.state = os.path.join(self.tmp.name, "spool", "watchdog-state.json")
		self.clock = Clock()
		self.sender = Sender()

	def tearDown(self):
		self.tmp.cleanup()

	def make(self, cfg=CFG):
		return Watchdog(cfg, state_path=self.state, clock=self.clock, sender=self.sender)

	def cycle(self, wd, minutes, push_ok, h=None, step=0.5):
		"""Run push cycles every `step` minutes for `minutes`."""
		for _ in range(int(minutes / step)):
			self.clock.advance(step)
			wd.observe(push_ok, h if push_ok else None)

	def test_site_down_one_alert_then_recovery(self):
		wd = self.make()
		self.cycle(wd, 10, True, health())
		self.assertEqual(self.sender.sent, [])

		self.cycle(wd, 4.5, False)  # below the 5 min threshold
		self.assertEqual(self.sender.sent, [])
		self.cycle(wd, 1, False)
		self.assertEqual(len(self.sender.sent), 1)
		subject, text = self.sender.sent[0]
		self.assertIn("bench-01", subject)
		self.assertIn("is not working", subject)
		self.assertIn("pushes to https://wolf.example.com failing", text)

		self.cycle(wd, 60, False)  # long outage: still a single alert
		self.assertEqual(len(self.sender.sent), 1)
		self.assertEqual(wd.state["status"], "down")

		self.cycle(wd, 0.5, True, health())
		self.assertEqual(len(self.sender.sent), 2)
		subject, text = self.sender.sent[1]
		self.assertIn("recovered", subject)
		self.assertIn("outage of 1 h 0", text)
		self.cycle(wd, 30, True, health())
		self.assertEqual(len(self.sender.sent), 2)

	def test_stale_rules_alert_and_recovery(self):
		wd = self.make()
		self.cycle(wd, 1, True, health(rules_age=200))
		self.assertEqual(self.sender.sent, [])
		self.cycle(wd, 1, True, health(rules_age=320, scheduler={"enabled": False, "reason": "pause_scheduler"}))
		self.assertEqual(len(self.sender.sent), 1)
		self.assertIn("alert rules not evaluated for 5 min", self.sender.sent[0][1])
		self.assertIn("pause_scheduler", self.sender.sent[0][1])
		# site goes fully down during the same outage: no second alert
		self.cycle(wd, 10, False)
		self.cycle(wd, 5, True, health(rules_age=900))
		self.assertEqual(len(self.sender.sent), 1)
		self.cycle(wd, 0.5, True, health(rules_age=20))
		self.assertEqual(self.sender.subjects[-1].endswith("recovered"), True)
		self.assertEqual(len(self.sender.sent), 2)

	def test_state_survives_restart(self):
		wd = self.make()
		self.cycle(wd, 1, True, health())
		self.cycle(wd, 6, False)
		self.assertEqual(len(self.sender.sent), 1)

		# the agent restarts mid-outage: no duplicate alert, and the new process sends the recovery
		wd = self.make()
		self.assertEqual(wd.state["status"], "down")
		self.cycle(wd, 20, False)
		self.assertEqual(len(self.sender.sent), 1)
		self.cycle(wd, 0.5, True, health())
		self.assertEqual(len(self.sender.sent), 2)
		self.assertIn("recovered", self.sender.subjects[-1])

		wd = self.make()
		self.assertEqual(wd.state["status"], "ok")
		self.cycle(wd, 5, True, health())
		self.assertEqual(len(self.sender.sent), 2)

	def test_restart_does_not_alert_on_first_failure(self):
		wd = self.make()
		self.cycle(wd, 1, True, health())
		self.clock.advance(60 * 24)  # agent stopped for a day
		wd = self.make()
		self.cycle(wd, 2, False)
		self.assertEqual(self.sender.sent, [])
		self.cycle(wd, 4, False)
		self.assertEqual(len(self.sender.sent), 1)

	def test_failed_delivery_is_retried_once_per_minute(self):
		wd = self.make()
		self.sender.fail = True
		self.cycle(wd, 6, False)
		self.assertEqual(wd.state["pending"], "alert")
		self.assertIn("channel down", wd.state["last_error"])
		next_attempt = wd.state["next_attempt"]
		self.assertEqual(next_attempt - self.clock.t, RETRY_SECONDS)

		self.sender.fail = False
		self.cycle(wd, 0.5, False)
		self.assertEqual(self.sender.sent, [])  # still waiting for the retry time
		self.cycle(wd, 0.5, False)
		self.assertEqual(len(self.sender.sent), 1)
		self.assertIsNone(wd.state["pending"])
		self.cycle(wd, 5, False)
		self.assertEqual(len(self.sender.sent), 1)

	def test_no_recovery_for_undelivered_alert(self):
		wd = self.make()
		self.sender.fail = True
		self.cycle(wd, 6, False)
		self.sender.fail = False
		self.clock.advance(0.1)
		wd.observe(True, health())  # recovers before the retry is due
		self.assertEqual(self.sender.sent, [])
		self.assertIsNone(wd.state["pending"])
		self.assertEqual(wd.state["status"], "ok")

	def test_sender_crash_and_corrupt_state_never_raise(self):
		os.makedirs(os.path.dirname(self.state))
		with open(self.state, "w") as f:
			f.write("{not json")
		wd = Watchdog(CFG, state_path=self.state, clock=self.clock, sender=lambda *a: 1 / 0)
		self.assertEqual(wd.state["status"], "ok")
		for _ in range(20):
			self.clock.advance(0.5)
			wd.observe(False)
		self.assertEqual(wd.state["status"], "down")
		self.assertEqual(wd.state["pending"], "alert")
		self.assertIn("division by zero", wd.state["last_error"])
		with open(self.state) as f:
			self.assertEqual(json.load(f)["status"], "down")

	def test_disabled_and_no_channel(self):
		wd = self.make({**CFG, "watchdog": {"enabled": False}})
		self.cycle(wd, 10, False)
		self.assertEqual(self.sender.sent, [])
		self.assertFalse(os.path.exists(self.state))

		# no channel configured: the alert is logged, the outage still counts as alerted
		wd = Watchdog(CFG, state_path=self.state, clock=self.clock)
		with self.assertLogs("wolf_agent.watchdog", level="ERROR") as logs:
			for _ in range(14):
				self.clock.advance(0.5)
				wd.observe(False)
		self.assertTrue(any("no watchdog fallback channel" in line for line in logs.output))
		self.assertTrue(wd.state["alerted"])

	def test_summary(self):
		wd = self.make()
		self.cycle(wd, 6, False)
		text = "\n".join(wd.summary())
		self.assertIn("state: down since", text)
		self.assertIn("fallback: none", text)
		self.assertIn("problem: pushes to", text)


class Hook(ThreadingHTTPServer):
	daemon_threads = True

	def __init__(self, status=200):
		super().__init__(("127.0.0.1", 0), HookHandler)
		self.bodies = []
		self.status = status
		threading.Thread(target=self.serve_forever, daemon=True).start()

	@property
	def url(self):
		return f"http://127.0.0.1:{self.server_address[1]}/hooks/abc"


class HookHandler(BaseHTTPRequestHandler):
	def log_message(self, *args):
		pass

	def do_POST(self):
		self.server.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
		self.send_response(self.server.status)
		self.send_header("Content-Length", "2")
		self.end_headers()
		self.wfile.write(b"ok")


class FakeSMTP:
	"""Just enough ESMTP for smtplib: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

	def __init__(self):
		self.sock = socket.socket()
		self.sock.bind(("127.0.0.1", 0))
		self.sock.listen(5)
		self.port = self.sock.getsockname()[1]
		self.messages = []
		self.auth = []
		threading.Thread(target=self._serve, daemon=True).start()

	def close(self):
		self.sock.close()

	def _serve(self):
		while True:
			try:
				conn, _ = self.sock.accept()
			except OSError:
				return
			threading.Thread(target=self._session, args=(conn,), daemon=True).start()

	def _session(self, conn):
		f = conn.makefile("rwb")

		def reply(line):
			f.write(line.encode() + b"\r\n")
			f.flush()

		reply("220 fake ESMTP")
		envelope = {"rcpt": []}
		with conn, f:
			for raw in f:
				cmd = raw.decode().strip()
				verb = cmd.split(" ", 1)[0].upper()
				if verb in ("EHLO", "HELO"):
					reply("250-fake")
					reply("250 AUTH PLAIN")
				elif verb == "AUTH":
					self.auth.append(base64.b64decode(cmd.split()[2]).split(b"\0")[1:])
					reply("235 ok")
				elif verb == "MAIL":
					envelope["from"] = cmd
					reply("250 ok")
				elif verb == "RCPT":
					envelope["rcpt"].append(cmd)
					reply("250 ok")
				elif verb == "DATA":
					reply("354 go")
					data = b""
					for line in f:
						if line == b".\r\n":
							break
						data += line
					self.messages.append({**envelope, "msg": message_from_bytes(data)})
					envelope = {"rcpt": []}
					reply("250 queued")
				elif verb == "QUIT":
					reply("221 bye")
					return
				else:
					reply("502 not implemented")


class TestFallback(unittest.TestCase):
	def test_webhook(self):
		hook = Hook()
		try:
			fallback.send_webhook(hook.url, "hello")
			self.assertEqual(hook.bodies, [{"text": "hello"}])
			hook.status = 500
			with self.assertRaises(FallbackError):
				fallback.send_webhook(hook.url, "hello")
		finally:
			hook.shutdown()
			hook.server_close()
		with self.assertRaises(FallbackError):
			fallback.send_webhook("http://127.0.0.1:9/x", "hello", timeout=2)

	def test_smtp_with_auth(self):
		server = FakeSMTP()
		try:
			cfg = {"host": "127.0.0.1", "port": server.port, "from": "wolf@example.com",
				"to": "ops@example.com, oncall@example.com", "user": "wolf", "password": "pw"}  # fmt: skip
			fallback.send_smtp(cfg, "Wolf down", "body text")
			(m,) = server.messages
			self.assertEqual(m["msg"]["Subject"], "Wolf down")
			self.assertEqual(m["msg"]["To"], "ops@example.com, oncall@example.com")
			self.assertIn("body text", m["msg"].get_payload())
			self.assertEqual(len(m["rcpt"]), 2)
			self.assertEqual(server.auth, [[b"wolf", b"pw"]])
		finally:
			server.close()

	def test_smtp_starttls_unsupported_fails_cleanly(self):
		server = FakeSMTP()
		try:
			with self.assertRaises(FallbackError):
				fallback.send_smtp({"host": "127.0.0.1", "port": server.port, "to": ["a@b.c"], "starttls": True}, "s", "t")
		finally:
			server.close()
		with self.assertRaises(FallbackError):
			fallback.send_smtp({"host": "127.0.0.1"}, "s", "t")  # no recipient

	def test_send_all_channels(self):
		hook, server = Hook(), FakeSMTP()
		try:
			wd = {"webhook_url": hook.url, "smtp": {"host": "127.0.0.1", "port": server.port, "to": ["a@b.c"]}}
			self.assertEqual(fallback.send(wd, "Subj", "text"), ["webhook", "smtp"])
			self.assertEqual(hook.bodies, [{"text": "*Subj*\ntext"}])
			self.assertEqual(len(server.messages), 1)

			hook.status = 500  # one channel down: still delivered by the other
			self.assertEqual(fallback.send(wd, "Subj", "text"), ["smtp"])
			wd["smtp"]["port"] = 9
			with self.assertRaises(FallbackError):
				fallback.send(wd, "Subj", "text")
		finally:
			hook.shutdown()
			hook.server_close()
			server.close()
		self.assertEqual(fallback.send({}, "s", "t"), [])
		self.assertEqual(fallback.channels(None), [])


class TestCli(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.hook = Hook()
		self.config = os.path.join(self.tmp.name, "config.json")

	def tearDown(self):
		self.hook.shutdown()
		self.hook.server_close()
		self.tmp.cleanup()

	def write(self, watchdog):
		cfg = {"url": "http://127.0.0.1:9", "agent_id": "WAG-1", "agent_secret": "s", "hostname": "h",
			"spool_dir": os.path.join(self.tmp.name, "spool"), "benches": [], "auth_log": False,
			"collect_host_metrics": False, "watchdog": watchdog}  # fmt: skip
		with open(self.config, "w") as f:
			json.dump(cfg, f)

	def run_cli(self, *argv):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = cli.main(["--config", self.config, *argv])
		return code, out.getvalue(), err.getvalue()

	def test_test_fallback(self):
		self.write({"webhook_url": self.hook.url})
		code, out, _ = self.run_cli("test-fallback")
		self.assertEqual(code, 0)
		self.assertIn("via webhook", out)
		self.assertIn("test message", self.hook.bodies[0]["text"])

		self.write({})
		with self.assertRaises(SystemExit) as ctx:
			self.run_cli("test-fallback")
		self.assertIn("no fallback channel", str(ctx.exception.code))

	def test_check_reports_watchdog_on_stderr(self):
		self.write({"webhook_url": self.hook.url})
		code, out, err = self.run_cli("check")
		self.assertEqual(code, 0)
		self.assertEqual(json.loads(out)["hostname"], "h")
		self.assertIn("watchdog: enabled, stale after 5 min, fallback: webhook", err)
		self.assertIn("state: ok", err)
		code, out, err = self.run_cli("check", "--status")
		self.assertIn("live status: unavailable", err)


if __name__ == "__main__":
	unittest.main()
