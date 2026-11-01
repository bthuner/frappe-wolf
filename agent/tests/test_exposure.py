import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import collector, exposure  # noqa: E402

NOW = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)

# sl local_address rem_address st ... (real /proc/net/tcp layout, trimmed)
TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:2AF8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1
   1: 00000000:2EE0 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2 1
   2: 00000000:0CEA 00000000:0000 0A 00000000:00000000 00:00000000 00000000   999        0 3 1
   3: 0500000A:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4 1
   4: 0100007F:2328 0100007F:A000 01 00000000:00000000 00:00000000 00000000  1000        0 5 1
"""
# :: listening on 6379, ::1 on 13000
TCP6 = """  sl  local_address                         remote_address                        st
   0: 00000000000000000000000000000000:18EB 00000000000000000000000000000000:0000 0A 0
   1: 00000000000000000000000001000000:32C8 00000000000000000000000000000000:0000 0A 0
"""


class TestParsing(unittest.TestCase):
	def test_proc_net(self):
		self.assertEqual(
			exposure.parse_proc_net(TCP),
			[("127.0.0.1", 11000), ("0.0.0.0", 12000), ("0.0.0.0", 3306), ("10.0.0.5", 8000)],
		)  # the ESTABLISHED row is skipped
		self.assertEqual(exposure.parse_proc_net(TCP6, ipv6=True), [("::", 6379), ("::1", 13000)])

	def test_is_exposed(self):
		self.assertTrue(exposure.is_exposed("0.0.0.0"))
		self.assertTrue(exposure.is_exposed("::"))
		self.assertTrue(exposure.is_exposed("93.184.216.34"))
		self.assertFalse(exposure.is_exposed("127.0.0.1"))
		self.assertFalse(exposure.is_exposed("::1"))
		self.assertFalse(exposure.is_exposed("10.0.0.5"))  # bound to the LAN on purpose
		self.assertFalse(exposure.is_exposed("::ffff:127.0.0.1"))

	def test_exposed_listeners(self):
		listeners = exposure.parse_proc_net(TCP) + exposure.parse_proc_net(TCP6, ipv6=True)
		found = exposure.exposed_listeners(listeners, exposure.DEFAULT_PORTS)
		self.assertEqual(sorted(found), [3306, 6379, 12000])
		self.assertEqual(found[6379], ("Redis", "critical", ["::"]))

	def test_site_config_findings(self):
		self.assertEqual(exposure.site_config_findings({"db_name": "x", "developer_mode": 0}), [])
		found = exposure.site_config_findings({"developer_mode": 1, "allow_tests": True, "host_name": "http://erp.example.com",
		                                       "allow_cors": "*"})  # fmt: skip
		self.assertEqual([f["setting"] for f in found], ["developer_mode", "allow_tests", "host_name", "allow_cors"])
		self.assertEqual(exposure.site_config_findings({"host_name": "https://erp.example.com", "allow_cors": ["https://a"]}), [])


class TestWatcher(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.mkdtemp()
		self.proc = os.path.join(self.tmp, "proc")
		os.makedirs(self.proc)
		self.write(os.path.join(self.proc, "tcp"), TCP)
		bench = os.path.join(self.tmp, "frappe-bench")
		os.makedirs(os.path.join(bench, "sites", "a.example.com"))
		os.makedirs(os.path.join(bench, "sites", "b.example.com"))
		self.common = os.path.join(bench, "sites", "common_site_config.json")
		self.write(self.common, json.dumps({"redis_queue": "redis://127.0.0.1:11000", "webserver_port": 8000}))
		self.site_a = os.path.join(bench, "sites", "a.example.com", "site_config.json")
		self.write(self.site_a, json.dumps({"db_name": "a", "developer_mode": 1}))
		self.write(os.path.join(bench, "sites", "b.example.com", "site_config.json"), json.dumps({"db_name": "b"}))
		self.benches = [{"name": "frappe-bench", "path": bench, "sites": ["a.example.com", "b.example.com"]}]

	def write(self, path, text):
		with open(path, "w") as f:
			f.write(text)

	def test_counts_every_run_events_on_change(self):
		w = exposure.ExposureWatcher("h1", proc_root=self.proc)
		metrics, events = w.collect(self.benches, NOW)
		values = {(m["metric_name"], m["site"]): m["value"] for m in metrics}
		self.assertEqual(values[("exposed_services", None)], 2)  # 12000 and 3306; 8000 is on the LAN
		self.assertEqual(values[("site_config_risks", "a.example.com")], 1)
		self.assertEqual(values[("site_config_risks", "b.example.com")], 0)
		by = {(e["category"], e["payload"].get("port")): e for e in events}
		redis = by[("service_exposed", 12000)]
		self.assertEqual(redis["severity"], "critical")
		self.assertEqual(redis["title"], "Redis (socket.io) (port 12000) listens on all interfaces")
		self.assertEqual(by[("service_exposed", 3306)]["severity"], "warning")
		cfg = by[("insecure_site_config", None)]
		self.assertEqual((cfg["site"], cfg["bench"], cfg["severity"]), ("a.example.com", "frappe-bench", "critical"))

		metrics, events = w.collect(self.benches, NOW)  # nothing changed: counts only
		self.assertEqual(events, [])
		self.assertEqual(len(metrics), 3)

		self.write(self.site_a, json.dumps({"db_name": "a", "developer_mode": 1, "allow_tests": 1}))
		_, events = w.collect(self.benches, NOW)
		self.assertEqual([e["category"] for e in events], ["insecure_site_config"])
		self.assertIn("allow_tests", events[0]["title"])

	def test_bench_ports_from_common_site_config(self):
		self.write(self.common, json.dumps({"redis_queue": "redis://127.0.0.1:11311", "db_port": 3307}))
		ports = exposure.bench_ports(self.benches, {"5000": "custom api"})
		self.assertEqual(ports[11311], ("Redis (queue)", "critical"))
		self.assertEqual(ports[3307], ("database", "warning"))
		self.assertEqual(ports[5000], ("custom api", "warning"))

	def test_infra_runs_it_on_its_interval(self):
		ticks = [0.0]
		infra = collector.InfraChecks({"mariadb": False, "processes": False, "backups": False, "tls": False,
		                               "exposure": {"interval": 600}}, "h1", clock=lambda: ticks[0])  # fmt: skip
		infra.exposure_watcher.proc_root = self.proc
		self.assertIn("exposed_services", [m["metric_name"] for m in infra.collect(self.benches, NOW)[0]])
		ticks[0] = 30
		self.assertEqual(infra.collect(self.benches, NOW)[0], [])
		ticks[0] = 601
		self.assertTrue(infra.collect(self.benches, NOW)[0])


if __name__ == "__main__":
	unittest.main()
