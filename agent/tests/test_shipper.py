import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import shipper  # noqa: E402
from wolf_agent.shipper import AuthError, Shipper, Spool, register, split_batches  # noqa: E402


class FakeWolf(ThreadingHTTPServer):
	daemon_threads = True

	def __init__(self):
		super().__init__(("127.0.0.1", 0), FakeWolfHandler)
		self.requests = []
		self.status = 200
		self.push_interval = 30
		self.health = None
		threading.Thread(target=self.serve_forever, daemon=True).start()

	@property
	def url(self):
		return f"http://127.0.0.1:{self.server_address[1]}"


class FakeWolfHandler(BaseHTTPRequestHandler):
	def log_message(self, *args):
		pass

	def do_POST(self):
		body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
		self.server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
		status = self.server.status
		if self.path.endswith("agent.register"):
			resp = {"message": {"agent_id": "WAG-0007", "agent_secret": "x" * 40, "push_interval": 20}}
		elif self.path.endswith("ingest.push"):
			resp = {
				"message": {
					"metrics": len(body["metrics"]),
					"events": len(body["events"]),
					"push_interval": self.server.push_interval,
					"health": self.server.health,
				}
			}
		elif self.path.endswith("ingest.status"):
			resp = {"message": {"agent": "WAG-0001", "health": self.server.health}}
		else:
			resp = {"message": {"ok": True}}
		data = json.dumps(resp if status == 200 else {"exc_type": "Error"}).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)


class Clock:
	def __init__(self):
		self.t = 1000.0

	def __call__(self):
		return self.t


class TestSplit(unittest.TestCase):
	def test_split_limits(self):
		chunks = list(split_batches(list(range(12001)), list(range(1500))))
		self.assertEqual([(len(m), len(e)) for m, e in chunks], [(5000, 1000), (5000, 500), (2001, 0)])
		self.assertEqual(list(split_batches([], [])), [])
		self.assertEqual([(len(m), len(e)) for m, e in split_batches([], [1])], [(0, 1)])


class TestSpool(unittest.TestCase):
	def test_cap_drops_oldest(self):
		with tempfile.TemporaryDirectory() as tmp:
			spool = Spool(tmp, cap_bytes=250)
			for i in range(10):
				spool.put({"n": i, "pad": "x" * 50})
			kept = [body["n"] for _, body in spool.items()]
			self.assertLess(len(kept), 10)
			self.assertEqual(kept, sorted(kept))
			self.assertEqual(kept[-1], 9)

	def test_unreadable_file_dropped(self):
		with tempfile.TemporaryDirectory() as tmp:
			spool = Spool(tmp)
			with open(os.path.join(tmp, "0001.json"), "w") as f:
				f.write("{broken")
			self.assertEqual(list(spool.items()), [])
			self.assertEqual(len(spool), 0)


class TestShipper(unittest.TestCase):
	def setUp(self):
		self.server = FakeWolf()
		self.tmp = tempfile.TemporaryDirectory()
		self.clock = Clock()
		cfg = {
			"url": self.server.url + "/",
			"agent_id": "WAG-0001",
			"agent_secret": "secret",
			"hostname": "bench-01",
			"push_interval": 30,
			"spool_dir": self.tmp.name,
		}
		self.shipper = Shipper(cfg, clock=self.clock)

	def tearDown(self):
		self.server.shutdown()
		self.server.server_close()
		self.tmp.cleanup()

	def test_push_format_and_headers(self):
		self.server.push_interval = 15
		self.assertTrue(self.shipper.send([{"metric_name": "cpu_usage", "value": 1}], [{"type": "error"}]))
		req = self.server.requests[0]
		self.assertEqual(req["path"], "/api/method/frappe_wolf.api.ingest.push")
		self.assertEqual(req["headers"]["X-Wolf-Agent-Id"], "WAG-0001")
		self.assertEqual(req["headers"]["X-Wolf-Agent-Secret"], "secret")
		self.assertEqual(req["headers"]["Content-Type"], "application/json")
		body = req["body"]
		self.assertEqual(set(body), {"sent_at", "agent_version", "hostname", "metrics", "events"})
		self.assertTrue(body["sent_at"].endswith("Z"))
		self.assertEqual(body["hostname"], "bench-01")
		self.assertEqual(self.shipper.push_interval, 15)

	def test_large_batch_is_split(self):
		self.shipper.send([{"v": i} for i in range(6000)], [{"e": i} for i in range(10)])
		sizes = [(len(r["body"]["metrics"]), len(r["body"]["events"])) for r in self.server.requests]
		self.assertEqual(sizes, [(5000, 10), (1000, 0)])

	def test_failure_spools_then_replays_in_order(self):
		self.server.status = 502
		self.assertFalse(self.shipper.send([{"n": 1}], []))
		self.assertEqual(len(self.shipper.spool), 1)

		# still in backoff: nothing is attempted, new data goes to the spool
		attempts = len(self.server.requests)
		self.assertFalse(self.shipper.send([{"n": 2}], []))
		self.assertEqual(len(self.server.requests), attempts)
		self.assertEqual(len(self.shipper.spool), 2)

		self.server.status = 200
		self.clock.t += 3600
		self.assertTrue(self.shipper.send([{"n": 3}], []))
		self.assertEqual(len(self.shipper.spool), 0)
		replayed = [r["body"]["metrics"][0]["n"] for r in self.server.requests if r["body"]["metrics"]]
		self.assertEqual(replayed[-3:], [1, 2, 3])

	def test_backoff_grows_and_caps(self):
		self.server.status = 503
		delays = []
		for _ in range(10):
			self.clock.t = self.shipper.retry_at + 1
			start = self.clock.t
			self.shipper.send([{"n": 1}], [])
			delays.append(self.shipper.retry_at - start)
		self.assertEqual(delays[:4], [5, 10, 20, 40])
		self.assertEqual(delays[-1], shipper.MAX_BACKOFF)

	def test_auth_error_raises(self):
		for status in (401, 403):
			self.server.status = status
			with self.assertRaises(AuthError):
				self.shipper.send([{"n": 1}], [])

	def test_heartbeat_sent_but_not_spooled(self):
		self.assertTrue(self.shipper.send([], []))
		self.assertEqual(self.server.requests[-1]["body"]["metrics"], [])
		self.server.status = 502
		self.shipper.send([], [])
		self.assertEqual(len(self.shipper.spool), 0)

	def test_rejected_batch_is_dropped(self):
		self.server.status = 417
		self.assertTrue(self.shipper.send([{"n": 1}], []))
		self.assertEqual(len(self.shipper.spool), 0)

	def test_network_down_spools(self):
		self.shipper.url = "http://127.0.0.1:9"  # discard port: connection refused
		self.assertFalse(self.shipper.send([{"n": 1}], []))
		self.assertEqual(len(self.shipper.spool), 1)

	def test_health_from_push_and_status(self):
		self.assertTrue(self.shipper.send([], []))
		self.assertIsNone(self.shipper.health)  # older servers send no health
		self.server.health = {"ok": True, "rules": {"age_seconds": 12}}
		self.shipper.send([], [])
		self.assertEqual(self.shipper.health["rules"]["age_seconds"], 12)
		self.server.status = 502
		self.shipper.send([], [])
		self.assertIsNone(self.shipper.health)  # a failed push says nothing about health

		self.server.status = 200
		self.assertEqual(self.shipper.status()["health"]["ok"], True)
		req = self.server.requests[-1]
		self.assertEqual(req["path"], "/api/method/frappe_wolf.api.ingest.status")
		self.assertEqual(req["headers"]["X-Wolf-Agent-Secret"], "secret")

	def test_mark_deployment(self):
		self.shipper.mark_deployment("main", note="bench update")
		req = self.server.requests[-1]
		self.assertEqual(req["path"], "/api/method/frappe_wolf.api.ingest.deployment")
		self.assertEqual(req["body"]["bench"], "main")
		self.assertEqual(req["body"]["source"], "manual")
		self.assertEqual(req["headers"]["X-Wolf-Agent-Id"], "WAG-0001")

	def test_register(self):
		resp = register(self.server.url, "tok", "bench-01", [{"name": "main", "path": "/b", "sites": ["a"], "logs_dir": "/b/logs"}], "Debian 12")
		self.assertEqual(resp["agent_id"], "WAG-0007")
		req = self.server.requests[-1]
		self.assertEqual(req["path"], "/api/method/frappe_wolf.api.agent.register")
		self.assertNotIn("X-Wolf-Agent-Id", req["headers"])
		self.assertEqual(req["body"]["registration_token"], "tok")
		self.assertEqual(req["body"]["benches"], [{"name": "main", "path": "/b", "sites": ["a"]}])
		self.assertEqual(req["body"]["os"], "Debian 12")


if __name__ == "__main__":
	unittest.main()
