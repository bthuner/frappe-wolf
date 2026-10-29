"""Frappe Pilot benches, against fixtures built from github.com/frappe/pilot.

bench.toml mirrors pilot's tests/fixtures/bench_toml/production_systemd.toml;
log names come from pilot/managers/processes/*, the nginx line from the
`pilot_access` log_format, task files and the /api/v1/tasks shape from
pilot/internal/tasks and pilot/managers/task/models.py (TaskInfo.as_dict).
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import discovery, pilot  # noqa: E402
from wolf_agent.collector import Collector  # noqa: E402
from wolf_agent.parsers.nginx import NginxParser  # noqa: E402

NOW = datetime(2026, 10, 29, 2, 10, 0, tzinfo=timezone.utc)
SECRET = "fixture-jwt-secret"

BENCH_TOML = """\
[bench]
name = "systemd-prod"
python = "3.14"
http_port = 8020
socketio_port = 9020
socketio_backend = "node"
db_type = "mariadb"
default_branch = "version-16"

[[apps]]
name = "frappe"
repo = "https://github.com/frappe/frappe"
branch = "version-16"

[redis]
cache_port = 13020
queue_port = 11020

[[workers]]
queues = ["default", "short", "long"]
count = 3

[production]
enabled = true
process_manager = "systemd"

[admin]
port = 7020
enabled = true
jwt_secret = "fixture-jwt-secret"
domain = "admin.systemd.example.test"
tls = true

[monitor]
log_path = "/var/log/systemd-prod-stats.log"
"""


def write(path, text):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as f:
		f.write(text)


def iso(dt):
	return dt.isoformat() if dt else None


def make_pilot_bench(root, toml=BENCH_TOML):
	path = os.path.join(root, "home", "frappe", "pilot", "benches", "systemd-prod")
	write(os.path.join(path, "bench.toml"), toml)
	write(os.path.join(path, "sites", "a.example.test", "site_config.json"), json.dumps(
		{"db_name": "a", "domains": ["www.a.example.test", {"domain": "shop.customer.test", "route": {"public_scheme": "https"}}]}
	))
	write(os.path.join(path, "sites", "common_site_config.json"), "{}")
	for name in ("web.log", "schedule.log", "worker_default_short_long_0.log", "nginx-access.log"):
		write(os.path.join(path, "logs", name), "")
	return path


def add_task(bench_path, task_id, command, status, args=None, started=None, finished=None, failure=None):
	d = os.path.join(bench_path, "tasks", task_id)
	meta = {"task_id": task_id, "command": command, "args": args or {}, "queued_at": iso(started or NOW),
		"started_at": iso(started), "finished_at": iso(finished), "exit_code": 1 if status == "failed" else None}
	if failure:
		meta["failure"] = failure
	write(os.path.join(d, "meta.json"), json.dumps(meta))
	write(os.path.join(d, "status"), status)


def verify_jwt(token, secret):
	head, body, sig = token.split(".")
	expected = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
	pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
	if not hmac.compare_digest(base64.urlsafe_b64decode(pad(sig)), expected):
		return None
	return json.loads(base64.urlsafe_b64decode(pad(body)))


@unittest.skipIf(discovery._toml is None, "no TOML parser available")
class TestPilotDiscovery(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()

	def tearDown(self):
		self.tmp.cleanup()

	def test_real_bench_toml(self):
		path = make_pilot_bench(self.tmp.name)
		b = discovery.describe_bench(path, nginx_default=None)
		self.assertEqual(b["platform"], "pilot")
		self.assertEqual(b["name"], "systemd-prod")
		self.assertEqual(b["sites"], ["a.example.test"])
		self.assertEqual(b["logs_dir"], os.path.join(path, "logs"))
		self.assertEqual(b["nginx_access_log"], os.path.join(path, "logs", "nginx-access.log"))
		self.assertEqual(b["redis_queue"], "redis://127.0.0.1:11020")
		self.assertEqual(b["pilot_api_url"], "http://127.0.0.1:7021")
		self.assertTrue(b["production"])
		self.assertEqual(b["site_domains"], {"www.a.example.test": "a.example.test", "shop.customer.test": "a.example.test"})
		self.assertIn("web.log.error.log", b["log_files"])  # systemd StandardError=append:<log>.error.log
		self.assertIn("web.error.log", b["log_files"])  # supervisor stderr_logfile=<name>.error.log
		self.assertEqual(b["log_files"]["worker_default_short_long_0.log"], ["worker"])
		self.assertNotIn("jwt_secret", json.dumps(b))  # secrets never land in the agent config

	def test_glob_finds_pilot_benches(self):
		make_pilot_bench(self.tmp.name)
		globs = (os.path.join(self.tmp.name, "home", "*", "pilot", "benches", "*"),)
		self.assertEqual([b["name"] for b in discovery.discover_benches(search_globs=globs, nginx_default=None)], ["systemd-prod"])
		self.assertIn("/home/*/pilot/benches/*", discovery.DEFAULT_SEARCH_GLOBS)

	def test_non_pilot_toml_keeps_defensive_parse(self):
		path = make_pilot_bench(self.tmp.name, toml='[bench]\nname = "x"\nlogs_dir = "var/logs"\n')
		b = discovery.describe_bench(path, nginx_default=None)
		self.assertNotIn("platform", b)
		self.assertEqual(b["logs_dir"], os.path.join(path, "var", "logs"))

	def test_pilot_access_log_format(self):
		parser = NginxParser("h", site_benches={"a.example.test": "systemd-prod"}, site_aliases={"shop.customer.test": "a.example.test"})
		parser.feed('203.0.113.9 [29/Oct/2026:02:00:13 +0000] "POST /api/method/run" 502 "shop.customer.test" 0.004')
		parser.feed('203.0.113.9 [29/Oct/2026:02:00:14 +0000] "GET /app" 200 "a.example.test" 0.050')
		metrics, events = parser.drain(NOW)
		by = {(m["site"], m["metric_name"]): m["value"] for m in metrics}
		self.assertEqual((by[("a.example.test", "http_requests")], by[("a.example.test", "http_502")]), (2, 1))
		self.assertEqual((events[0]["site"], events[0]["bench"], events[0]["payload"]["route"]), ("a.example.test", "systemd-prod", "/api/method/run"))


class TestPilotTasks(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = make_pilot_bench(self.tmp.name)
		self.bench = {"name": "systemd-prod", "path": self.path, "pilot_api_url": None}
		self.state = os.path.join(self.tmp.name, "spool", "pilot-tasks.json")

	def tearDown(self):
		self.tmp.cleanup()

	def watcher(self, **kw):
		return pilot.PilotTaskWatcher("h", self.bench, state_path=self.state, **kw)

	def test_files_history_then_running_then_failed(self):
		old = NOW - timedelta(days=1)
		add_task(self.path, "20261028-021000-aaaaaa", "update", "success", {"operation_id": "op1"}, old, old)
		add_task(self.path, "20261029-020800-bbbbbb", "migrate", "running", {"operation_id": "op2", "site": "a.example.test"}, NOW - timedelta(minutes=2))
		add_task(self.path, "20261029-020900-cccccc", "backup-site", "running", {"site": "a.example.test"}, NOW)
		events = self.watcher().poll(NOW)
		self.assertEqual(len(events), 1)
		e = events[0]
		self.assertEqual((e["type"], e["category"], e["bench"], e["site"]), ("deployment", "pilot", "systemd-prod", "a.example.test"))
		self.assertEqual(e["timestamp"], "2026-10-29T02:08:00Z")
		self.assertEqual(e["payload"]["task"], "20261029-020800-bbbbbb")

		self.assertEqual(self.watcher().poll(NOW), [])  # restart: deduplicated
		add_task(self.path, "20261029-020800-bbbbbb", "migrate", "failed", {"site": "a.example.test"}, NOW - timedelta(minutes=2), NOW,
			failure={"code": "migration_failed", "message": "Patch erpnext.patches.v16.x failed"})
		add_task(self.path, "20261029-021000-dddddd", "switch-branch", "success", {"name": "erpnext", "branch": "version-16", "sites": ["a.example.test"]}, NOW, NOW)
		events = self.watcher().poll(NOW + timedelta(minutes=1))
		self.assertEqual([(e["type"], e["category"]) for e in events], [("error", "pilot_task_failed"), ("deployment", "pilot")])
		self.assertEqual(events[0]["payload"]["failure_code"], "migration_failed")
		self.assertEqual(self.watcher().poll(NOW + timedelta(minutes=2)), [])

	def test_admin_api_with_minted_token(self):
		seen = {}
		tasks = [{"task_id": "20261029-020800-bbbbbb", "command": "update", "args": {"operation_id": "op"}, "status": "running",
			"queued_at": iso(NOW), "started_at": iso(NOW - timedelta(minutes=1)), "finished_at": None, "failure": None}]

		class Handler(BaseHTTPRequestHandler):
			def do_GET(self):
				auth = self.headers.get("Authorization", "")
				claims = verify_jwt(auth[7:], SECRET) if auth.startswith("Bearer ") else None
				seen["path"], seen["claims"] = self.path, claims
				ok = claims and claims.get("scope") == "bench" and claims["exp"] > claims["iat"]
				body = json.dumps(tasks if ok else {"error": "unauthorized"}).encode()
				self.send_response(200 if ok else 401)
				self.send_header("Content-Type", "application/json")
				self.end_headers()
				self.wfile.write(body)

			def log_message(self, *a):
				pass

		server = HTTPServer(("127.0.0.1", 0), Handler)
		threading.Thread(target=server.serve_forever, daemon=True).start()
		try:
			url = f"http://127.0.0.1:{server.server_port}"
			w = self.watcher(api_url=url, toml_reader=discovery.read_bench_toml if discovery._toml else None, jwt_secret=None if discovery._toml else SECRET)
			events = w.poll(NOW)
			self.assertEqual(seen["path"], "/api/v1/tasks?limit=100")
			self.assertEqual((seen["claims"]["sub"], seen["claims"]["scope"]), ("admin", "bench"))
			self.assertEqual([e["payload"]["pilot_command"] for e in events], ["update"])
			bad = pilot.PilotTaskWatcher("h", dict(self.bench, path=os.path.join(self.tmp.name, "nowhere")), api_url=url, jwt_secret="wrong")
			self.assertIsNone(bad.poll(NOW))  # API refuses, no task files: unavailable
		finally:
			server.shutdown()
			server.server_close()

	def test_static_token_preferred(self):
		w = self.watcher(api_url="http://127.0.0.1:9", token="tok", jwt_secret=SECRET)
		self.assertEqual(w._bearer(), "tok")


@unittest.skipIf(discovery._toml is None, "no TOML parser available")
class TestPilotCollector(unittest.TestCase):
	def test_collect_ships_pilot_deployments_and_tails_process_logs(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = make_pilot_bench(tmp)
			bench = discovery.describe_bench(path, nginx_default=None)
			bench["pilot_api_url"] = None  # no Admin API in the test: task files are read
			cfg = {"hostname": "h", "collect_host_metrics": False, "auth_log": False, "platform": "generic", "benches": [bench]}
			c = Collector(cfg, state_path=os.path.join(tmp, "spool", "tail-state.json"))
			self.assertTrue(any(s.tailer.path.endswith("worker_default_short_long_0.log") for s in c.sources))
			self.assertTrue(any(s.tailer.path.endswith("nginx-access.log") for s in c.sources))
			c.collect(NOW)
			add_task(path, "20261029-021000-eeeeee", "update", "running", {"operation_id": "op"}, NOW)
			_, events = c.collect(NOW)
			self.assertEqual([(e["type"], e["bench"]) for e in events], [("deployment", "systemd-prod")])
			c.close()


if __name__ == "__main__":
	unittest.main()
