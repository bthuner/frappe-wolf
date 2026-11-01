import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import responder  # noqa: E402
from wolf_agent.shipper import Shipper  # noqa: E402

BLOCK = {"id": "WRA-00001", "action": "block_ip", "ip": "45.33.32.156", "seconds": 86400}


class FakeRun:
	def __init__(self, code=0, out="blocked"):
		self.calls, self.code, self.out = [], code, out

	def __call__(self, args, **kw):
		self.calls.append(args)
		return subprocess.CompletedProcess(args, self.code, self.out, "")


class TestResponder(unittest.TestCase):
	def test_disabled_by_default_reports_why(self):
		run = FakeRun()
		r = responder.Responder({}, run=run)
		(result,) = r.handle([BLOCK])
		self.assertFalse(result["ok"])
		self.assertIn("response.enabled", result["detail"])
		self.assertEqual(run.calls, [])

	def test_runs_the_command_with_arguments_not_a_shell(self):
		run = FakeRun()
		r = responder.Responder({"enabled": True}, run=run)
		self.assertEqual(r.handle([BLOCK]), [{"id": "WRA-00001", "ok": True, "detail": "blocked"}])
		self.assertEqual(run.calls, [["sudo", "-n", "/usr/local/sbin/wolf-block", "45.33.32.156", "86400"]])
		self.assertEqual(r.handle([BLOCK]), [])  # once per id

	def test_refuses_what_the_site_should_never_send(self):
		run = FakeRun()
		r = responder.Responder({"enabled": True}, run=run)
		bad = [
			{**BLOCK, "id": "a", "ip": "10.0.0.1"},
			{**BLOCK, "id": "b", "ip": "127.0.0.1; rm -rf /"},
			{**BLOCK, "id": "c", "seconds": 10 ** 7},
			{**BLOCK, "id": "d", "action": "shutdown"},
		]
		results = r.handle(bad)
		self.assertTrue(all(not x["ok"] and "refused" in x["detail"] or "unsupported" in x["detail"] for x in results))
		self.assertEqual(run.calls, [])

	def test_failure_is_reported(self):
		r = responder.Responder({"enabled": True}, run=FakeRun(code=1, out="sudo: a password is required"))
		(result,) = r.handle([BLOCK])
		self.assertEqual(result, {"id": "WRA-00001", "ok": False, "detail": "exit 1: sudo: a password is required"})

	def test_missing_binary(self):
		def boom(args, **kw):
			raise FileNotFoundError("no sudo")

		(result,) = responder.Responder({"enabled": True}, run=boom).handle([BLOCK])
		self.assertFalse(result["ok"])
		self.assertIn("no sudo", result["detail"])


class TestShipperCarriesActions(unittest.TestCase):
	def test_actions_in_and_results_out(self):
		import tempfile

		cfg = {"url": "https://wolf.example.com", "agent_id": "a", "agent_secret": "s", "hostname": "h",
		       "spool_dir": tempfile.mkdtemp()}  # fmt: skip
		shipper = Shipper(cfg)
		sent = []

		def post_json(url, body, headers, verify, **kw):
			sent.append(body)
			return {"actions": [BLOCK]}

		from unittest import mock

		patcher = mock.patch("wolf_agent.shipper.post_json", post_json)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.assertTrue(shipper.send([], []))
		self.assertEqual(shipper.actions, [BLOCK])
		self.assertNotIn("action_results", sent[0])
		shipper.results = [{"id": "WRA-00001", "ok": True, "detail": "blocked"}]
		self.assertTrue(shipper.send([], []))
		self.assertEqual(sent[1]["action_results"], [{"id": "WRA-00001", "ok": True, "detail": "blocked"}])
		self.assertEqual(shipper.results, [])


if __name__ == "__main__":
	unittest.main()
