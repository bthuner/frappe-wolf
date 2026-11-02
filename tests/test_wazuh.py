"""Wazuh alert → hids_alert for the integration route (wolf/wazuh.py), on the real alerts the agent
tests use (agent/tests/data/wazuh_alerts.json, written by a Wazuh 4.9.2 manager)."""

import json
import os
import unittest

from frappe_wolf.wolf import schema, wazuh

DATA = os.path.join(os.path.dirname(__file__), "..", "agent", "tests", "data", "wazuh_alerts.json")


def alerts():
	with open(DATA) as f:
		return {a["rule"]["id"]: a for a in map(json.loads, f)}


class TestWazuhMapping(unittest.TestCase):
	def test_real_alerts(self):
		by_id = alerts()
		sudo = wazuh.event_for(by_id["5404"], schema.fingerprint)
		self.assertEqual((sudo["category"], sudo["severity"], sudo["host"], sudo["source"]), ("hids_alert", "warning", "web01", "wazuh"))
		self.assertEqual(sudo["title"], "Wazuh: Three failed attempts to run sudo on web01")
		self.assertEqual((sudo["payload"]["attack_techniques"], sudo["payload"]["via"]), (["T1548.003"], "wazuh integration"))
		ssh = wazuh.event_for(by_id["5710"], schema.fingerprint)
		self.assertEqual((ssh["payload"]["ip"], ssh["payload"]["user"], ssh["severity"]), ("45.33.32.156", "admin", "info"))
		self.assertTrue(ssh["timestamp"].endswith("Z"))
		# the same rule and subject give the same fingerprint: repeats merge on the site
		self.assertEqual(ssh["fingerprint"], wazuh.event_for(by_id["5710"], schema.fingerprint)["fingerprint"])

	def test_skipped(self):
		by_id = alerts()
		self.assertIn("frappe_wolf", wazuh.skip_reason(by_id["100903"]))  # Wolf's own export: no loop
		self.assertIn("sca", wazuh.skip_reason(by_id["19007"]))
		self.assertIsNone(wazuh.event_for(by_id["19007"], schema.fingerprint))
		self.assertEqual(wazuh.skip_reason({"x": 1}), "not a Wazuh alert")
		self.assertEqual(wazuh.skip_reason({"rule": {"level": "high"}}), "rule level is not a number")

	def test_severity_and_host(self):
		self.assertEqual([wazuh.severity(n) for n in (3, 7, 12)], ["info", "warning", "critical"])
		self.assertEqual(wazuh.host_of({"agent": {"id": "004", "name": "erp-02"}}), "erp-02")


if __name__ == "__main__":
	unittest.main()
