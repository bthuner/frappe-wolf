"""Wazuh manager alerts (parsers/wazuh.py), from alerts a real Wazuh 4.9.2 manager wrote
(tests/data/wazuh_alerts.json: a CIS check, sshd lines received by syslog from host web01, a
sudo failure, and an alert on Wolf's own SIEM export)."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import collector  # noqa: E402
from wolf_agent.parsers.wazuh import WazuhParser, host_of, severity, timestamp  # noqa: E402

from test_discovery_config import make_bench  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data", "wazuh_alerts.json")
NOW = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)


def lines():
	with open(DATA) as f:
		return f.read().splitlines()


def run(**kw):
	p = WazuhParser("wazuh-manager", log_file="alerts.json", **kw)
	for line in lines():
		p.feed(line)
	return {e["payload"]["rule_id"]: e for e in p.drain(NOW)[1]}


class TestWazuhParser(unittest.TestCase):
	def test_default_floor_and_exclusions(self):
		events = run()
		self.assertEqual(sorted(events), ["5404"])  # level 10; sshd 5/3 under the floor, CIS (sca) and Wolf's own excluded
		sudo = events["5404"]
		self.assertEqual((sudo["type"], sudo["category"], sudo["source"], sudo["severity"]), ("security", "hids_alert", "wazuh", "warning"))
		self.assertEqual(sudo["host"], "web01")  # where it happened, not the manager that received the syslog line
		self.assertEqual(sudo["title"], "Wazuh: Three failed attempts to run sudo on web01")
		self.assertEqual((sudo["payload"]["attack_techniques"], sudo["payload"]["user"], sudo["payload"]["level"]), (["T1548.003"], "deploy", 10))
		self.assertEqual(sudo["payload"]["wazuh_agent"], {"id": "000", "name": "835f67e77c4b"})
		self.assertIn("incorrect password attempts", sudo["payload"]["sample_lines"][0])

	def test_lower_floor_groups_by_rule_and_ip(self):
		events = run(min_level=3, exclude_groups=())
		self.assertNotIn("100903", events)  # the loop guard holds whatever the settings
		self.assertIn("19007", events)  # sca kept when not excluded
		ssh = events["5710"]
		self.assertEqual((ssh["payload"]["ip"], ssh["payload"]["count"], ssh["severity"]), ("45.33.32.156", 2, "info"))
		self.assertTrue(ssh["title"].endswith("from 45.33.32.156 on web01, 2 alerts"))
		self.assertEqual(ssh["payload"]["attack_techniques"], ["T1110.001", "T1021.004"])
		self.assertTrue(ssh["timestamp"].endswith("Z"))

	def test_helpers(self):
		self.assertEqual([severity(n) for n in (3, 7, 11, 12, 15)], ["info", "warning", "warning", "critical", "critical"])
		self.assertEqual(host_of({"agent": {"id": "003", "name": "erp-01"}, "predecoder": {"hostname": "x"}}), "erp-01")
		self.assertEqual(host_of({"agent": {"id": "000", "name": "mgr"}}), "mgr")
		self.assertEqual(timestamp("2026-11-02T22:30:01.123+0000"), "2026-11-02T22:30:01Z")

	def test_bad_and_foreign_lines(self):
		p = WazuhParser("m")
		p.feed('{"rule": broken')
		p.feed('{"timestamp": "x"}')
		p.feed(json.dumps({"rule": {"level": "high"}}))
		self.assertEqual((p.drain(NOW), p.bad_lines), (([], []), 1))

	def test_collector_reads_the_alerts_log(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		path = make_bench(tmp.name, sites=("a.example.com",), redis=None)
		alerts = os.path.join(tmp.name, "alerts.json")
		with open(alerts, "w") as f:
			f.write("\n".join(lines()) + "\n")
		cfg = {"hostname": "wazuh-manager", "benches": [{"name": "b", "path": path, "sites": ["a.example.com"]}],
		       "auth_log": False, "collect_host_metrics": False, "wazuh": {"alerts_log": alerts}, "suricata": False,
		       "mariadb": False, "processes": False, "exposure": False, "backups": False, "tls": False, "rq": {},
		       "versions": False, "site_settings": False, "pilot": {"enabled": False}, "platform": "generic"}  # fmt: skip
		c = collector.Collector(cfg, state_path=os.path.join(tmp.name, "state.json"), from_start=True)
		_, events = c.collect(NOW)
		self.assertEqual([e["payload"]["rule_id"] for e in events if e["category"] == "hids_alert"], ["5404"])
		c.close()
		off = collector.Collector({**cfg, "wazuh": False}, state_path=os.path.join(tmp.name, "s2.json"), from_start=True)
		self.assertFalse(any(isinstance(p, WazuhParser) for s in off.sources for p in s.parsers))
		off.close()


if __name__ == "__main__":
	unittest.main()
