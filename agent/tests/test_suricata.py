"""Suricata eve.json alerts (parsers/suricata.py), from records a real Suricata 7 wrote with ET Open
(tests/data/suricata_eve.json: sqlmap probe, and a rule on the server's 403 answer)."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import collector  # noqa: E402
from wolf_agent.parsers import PARSERS  # noqa: E402
from wolf_agent.parsers.suricata import SuricataParser, local_addresses, remote_address, timestamp  # noqa: E402

from test_discovery_config import make_bench  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data", "suricata_eve.json")
SERVER = "172.18.0.14"
CLIENT = "172.18.0.1"
NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)


def records():
	with open(DATA) as f:
		return [json.loads(line) for line in f]


def parser(**kw):
	return SuricataParser("bench-01", log_file="eve.json", local={SERVER}, site_benches={"drift.localhost": "frappe-bench"}, **kw)


class TestRemoteAddress(unittest.TestCase):
	def test_real_records(self):
		sqlmap, answer = [r for r in records() if r["event_type"] == "alert"]
		self.assertEqual(remote_address(sqlmap, {SERVER}), CLIENT)
		self.assertEqual(answer["src_ip"], SERVER)  # the alert is on the server's packet...
		self.assertEqual(remote_address(answer, {SERVER}), CLIENT)  # ...but it is about the client

	def test_rules_source_and_outbound(self):
		self.assertEqual(remote_address({"alert": {"source": {"ip": "198.51.100.9"}}, "src_ip": "203.0.113.1"}), "198.51.100.9")
		outbound = {"src_ip": "203.0.113.10", "dest_ip": "198.51.100.66", "flow": {"src_ip": "203.0.113.10", "dest_ip": "198.51.100.66"}}
		self.assertEqual(remote_address(outbound, {"203.0.113.10"}), "198.51.100.66")  # our host called out (C2)
		self.assertEqual(remote_address(outbound, set()), "203.0.113.10")  # unknown host: the opener
		lan = {"src_ip": "10.0.0.5", "dest_ip": "198.51.100.66"}
		self.assertEqual(remote_address(lan, {"10.0.0.6"}), "10.0.0.5")  # neither end is ours: the opener

	def test_local_addresses_from_proc(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		trie, inet6 = os.path.join(tmp.name, "fib_trie"), os.path.join(tmp.name, "if_inet6")
		with open(trie, "w") as f:
			f.write("Local:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 203.0.113.10\n        /32 host LOCAL\n     |-- 203.0.113.255\n        /32 link BROADCAST\n")
		with open(inet6, "w") as f:
			f.write("20010db8000000000000000000000001 02 40 00 80 eth0\n")
		self.assertEqual(local_addresses(trie, inet6), {"203.0.113.10", "2001:db8::1"})
		self.assertEqual(local_addresses("/nonexistent", "/nonexistent"), set())


class TestParser(unittest.TestCase):
	def test_real_eve_lines(self):
		p = parser()
		with open(DATA) as f:
			for line in f:
				p.feed(line)
		_, events = p.drain(NOW)
		by_sid = {e["payload"]["sid"]: e for e in events}
		self.assertEqual(sorted(by_sid), [2008538, 2101201])  # stats and http records are not alerts
		sqlmap = by_sid[2008538]
		self.assertEqual((sqlmap["type"], sqlmap["category"], sqlmap["source"]), ("security", "network_ids_alert", "suricata"))
		self.assertEqual((sqlmap["severity"], sqlmap["site"], sqlmap["bench"]), ("warning", "drift.localhost", "frappe-bench"))
		self.assertEqual(sqlmap["title"], f"Suricata: ET SCAN Sqlmap SQL Injection Scan from {CLIENT}")
		payload = sqlmap["payload"]
		self.assertEqual((payload["ip"], payload["attack_techniques"], payload["priority"]), (CLIENT, ["T1190"], 2))
		self.assertTrue(payload["http_user_agent"].startswith("sqlmap/"))
		self.assertEqual(payload["http_hostname"], "drift.localhost")
		self.assertIn("flow_id", payload)
		self.assertEqual(payload["targets"], [f"{SERVER}:8080"])
		self.assertTrue(sqlmap["timestamp"].endswith("Z"))
		self.assertEqual(p.drain(NOW), ([], []))  # drained

	def test_one_event_per_signature_and_address(self):
		p = parser()
		line = next(json.dumps(r) for r in records() if r.get("alert", {}).get("signature_id") == 2008538)
		for _ in range(50):
			p.feed(line)
		other = json.loads(line)
		other["src_ip"] = other["flow"]["src_ip"] = "198.51.100.7"
		p.feed(json.dumps(other))
		_, events = p.drain(NOW)
		self.assertEqual(sorted((e["payload"]["ip"], e["payload"]["count"]) for e in events), [(CLIENT, 50), ("198.51.100.7", 1)])
		self.assertIn(", 50 alerts", next(e["title"] for e in events if e["payload"]["count"] == 50))

	def test_priority_filter_and_bad_lines(self):
		p = parser(max_priority=1)
		for r in records():
			p.feed(json.dumps(r))
		p.feed('{"event_type":"alert", broken')
		self.assertEqual(p.drain(NOW), ([], []))  # priorities 2 and 3 skipped
		self.assertEqual(p.bad_lines, 1)

	def test_overflow_is_counted(self):
		p = parser()
		base = next(r for r in records() if r.get("alert", {}).get("signature_id") == 2008538)
		for i in range(510):
			r = dict(base, alert=dict(base["alert"], signature_id=i))
			p.feed(json.dumps(r))
		_, events = p.drain(NOW)
		self.assertEqual(len(events), 501)
		self.assertIn("10 more alerts", events[-1]["title"])

	def test_timestamps(self):
		self.assertEqual(timestamp("2026-11-02T15:58:00.361888+0000"), "2026-11-02T15:58:00Z")
		self.assertEqual(timestamp("2026-11-02T17:58:00+0200"), "2026-11-02T15:58:00Z")
		self.assertIsNone(timestamp("yesterday"))


class TestWiring(unittest.TestCase):
	def test_collector_reads_the_eve_log(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		path = make_bench(tmp.name, sites=("drift.localhost",), redis=None)
		eve = os.path.join(tmp.name, "eve.json")
		with open(DATA) as src, open(eve, "w") as dst:
			dst.write(src.read())
		cfg = {"hostname": "bench-01", "benches": [{"name": "frappe-bench", "path": path, "sites": ["drift.localhost"]}],
		       "auth_log": False, "collect_host_metrics": False, "suricata": {"eve_log": eve},
		       "mariadb": False, "processes": False, "exposure": False, "backups": False, "tls": False, "rq": {},
		       "versions": False, "site_settings": False, "pilot": {"enabled": False}, "platform": "generic"}  # fmt: skip
		c = collector.Collector(cfg, state_path=os.path.join(tmp.name, "state.json"), from_start=True)
		for source in c.sources:
			for parser_ in source.parsers:
				if isinstance(parser_, SuricataParser):
					parser_.local = {SERVER}
		_, events = c.collect(NOW)
		self.assertEqual(sorted(e["payload"]["sid"] for e in events if e["category"] == "network_ids_alert"), [2008538, 2101201])
		self.assertIn("suricata", PARSERS)
		off = collector.Collector({**cfg, "suricata": False}, state_path=os.path.join(tmp.name, "state2.json"), from_start=True)
		self.assertFalse(any(isinstance(p, SuricataParser) for s in off.sources for p in s.parsers))
		c.close()
		off.close()


if __name__ == "__main__":
	unittest.main()
