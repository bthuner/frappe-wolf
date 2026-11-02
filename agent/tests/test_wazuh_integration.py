"""agent/wazuh/custom-frappe-wolf.py: the script Wazuh's integration daemon runs per alert."""

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("wolf_wazuh_integration", os.path.join(HERE, "..", "wazuh", "custom-frappe-wolf.py"))
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)

with open(os.path.join(HERE, "data", "wazuh_alerts.json")) as f:
	ALERTS = {a["rule"]["id"]: a for a in map(json.loads, f)}


class FakeWolf:
	def __init__(self, status=200, body=None):
		self.status, self.body, self.requests = status, body, []

	def __call__(self, req, timeout):
		self.requests.append(req)
		if self.status != 200:
			raise urllib.error.HTTPError(req.full_url, self.status, "x", {}, io.BytesIO(b'{"exc_type": "AuthenticationError"}'))
		return io.BytesIO(json.dumps(self.body or {"message": {"stored": True, "event": "ev1"}}).encode())


class TestIntegration(unittest.TestCase):
	def test_posts_the_alert_with_agent_credentials(self):
		wolf = FakeWolf()
		ok, detail = integration.send(ALERTS["5404"], "wazuh-mgr:s3cret", "https://wolf.example.com/", opener=wolf)
		self.assertEqual((ok, detail), (True, "stored as ev1"))
		req = wolf.requests[0]
		self.assertEqual(req.full_url, "https://wolf.example.com/api/method/frappe_wolf.api.wazuh.alert")
		self.assertEqual((req.get_header("X-wolf-agent-id"), req.get_header("X-wolf-agent-secret")), ("wazuh-mgr", "s3cret"))
		self.assertEqual(json.loads(req.data)["rule"]["id"], "5404")  # the alert as it is
		full = "https://wolf.example.com/api/method/frappe_wolf.api.wazuh.alert"
		self.assertEqual(integration.url_for(full), full)

	def test_refusals(self):
		self.assertEqual(integration.send(ALERTS["5404"], "no-secret", "https://w", opener=FakeWolf())[0], False)
		wolf = FakeWolf()
		self.assertEqual(integration.send(ALERTS["100903"], "a:b", "https://w", opener=wolf), (True, "skipped: an alert on Wolf's own export"))
		self.assertEqual(wolf.requests, [])  # nothing posted back
		ok, detail = integration.send(ALERTS["5404"], "a:b", "https://w", opener=FakeWolf(status=401))
		self.assertEqual(ok, False)
		self.assertIn("HTTP 401", detail)
		ok, detail = integration.send(ALERTS["19007"], "a:b", "https://w", opener=FakeWolf(body={"message": {"stored": False, "reason": "group sca"}}))
		self.assertEqual((ok, detail), (True, "not stored: group sca"))

	def test_main_reads_the_alert_file(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		path = os.path.join(tmp.name, "alert.json")
		with open(path, "w") as f:
			json.dump(ALERTS["5404"], f)
		integration.LOG = os.path.join(tmp.name, "integrations.log")
		self.assertEqual(integration.main(["x"]), 1)  # usage
		self.assertEqual(integration.main(["x", os.path.join(tmp.name, "missing"), "a:b", "https://w"]), 1)
		self.assertEqual(integration.main(["x", path, "bad", "https://w"]), 1)
		with open(integration.LOG) as f:
			self.assertIn("api_key must be AGENT_ID:AGENT_SECRET", f.read())


if __name__ == "__main__":
	unittest.main()
