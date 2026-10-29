"""Press server support, against a fake Press layout built from frappe/agent's sources.

Layout and schema mirror frappe/agent (see docs/press.md): agent config.json from
`agent setup config` (agent/cli.py), bench dirs from Server.bench_init
(agent/server.py), bench config keys from press Bench.update_bench_config
(press/press/doctype/bench/bench.py), job tables from peewee JobModel/StepModel
(agent/job.py), nginx `main` log format from agent/templates/nginx/nginx.conf.jinja2.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import discovery, press  # noqa: E402
from wolf_agent.collector import Collector  # noqa: E402
from wolf_agent.parsers.nginx import NginxParser  # noqa: E402

NOW = datetime(2026, 10, 29, 2, 10, 0, tzinfo=timezone.utc)


def local(dt):
	"""What peewee stores: naive datetime.now() text in server local time."""
	return dt.astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def write_json(path, data):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as f:
		json.dump(data, f)


class FakePress:
	def __init__(self, root):
		self.root = root
		self.agent_dir = os.path.join(root, "home", "frappe", "agent")
		self.benches_dir = os.path.join(root, "home", "frappe", "benches")
		os.makedirs(self.benches_dir)
		write_json(
			os.path.join(self.agent_dir, "config.json"),
			{
				"benches_directory": self.benches_dir,
				"name": "f1-mumbai.frappe.cloud",
				"nginx_directory": os.path.join(self.agent_dir, "nginx"),
				"redis_port": 25025,
				"user": "frappe",
				"workers": 2,
				"web_port": 25052,
			},
		)
		self.access_log = os.path.join(root, "var", "log", "nginx", "access.log")
		os.makedirs(os.path.dirname(self.access_log))
		open(self.access_log, "w").close()
		self.db = os.path.join(self.agent_dir, "jobs.sqlite3")
		conn = sqlite3.connect(self.db)
		conn.execute("pragma journal_mode=wal")
		conn.executescript(
			"""
			create table jobmodel (id integer primary key autoincrement, name varchar(255) not null,
				status varchar(255) not null, agent_job_id varchar(255), data text, enqueue datetime not null,
				start datetime, end datetime, duration time);
			create table stepmodel (id integer primary key autoincrement, name varchar(255) not null,
				job_id integer not null references jobmodel (id), status varchar(255) not null, data text,
				start datetime not null, end datetime, duration time);
			"""
		)
		conn.commit()
		self.conn = conn  # kept open, like the running Press agent (keeps -wal/-shm around)

	def add_bench(self, name, sites=(), rq_port=11001, domains=None):
		path = os.path.join(self.benches_dir, name)
		for d in ("logs", "sites", "config"):
			os.makedirs(os.path.join(path, d), exist_ok=True)
		write_json(
			os.path.join(path, "config.json"),
			{"docker_image": f"registry.frappe.cloud/{name}:1", "web_port": 18001, "socketio_port": 19001,
			 "rq_port": rq_port, "rq_cache_port": 13001, "single_container": True},
		)
		write_json(os.path.join(path, "sites", "common_site_config.json"), {"redis_queue": "redis://localhost:11000", "monitor": True})
		for site in sites:
			cfg = {"db_name": "x"}
			if domains and site in domains:
				cfg["domains"] = domains[site]
			write_json(os.path.join(path, "sites", site, "site_config.json"), cfg)
		return path

	def move_site(self, site, source, target):
		os.rename(os.path.join(self.benches_dir, source, "sites", site), os.path.join(self.benches_dir, target, "sites", site))

	def add_job(self, name, status, data, start=None, end=None, agent_job_id=None, enqueue=None):
		cur = self.conn.execute(
			"insert into jobmodel (name, status, agent_job_id, data, enqueue, start, end) values (?,?,?,?,?,?,?)",
			(name, status, agent_job_id, json.dumps(data), local(enqueue or start or NOW),
			 local(start) if start else None, local(end) if end else None),
		)
		self.conn.commit()
		return cur.lastrowid

	def set_job(self, jid, **fields):
		for key, value in fields.items():
			if isinstance(value, datetime):
				value = local(value)
			elif isinstance(value, (dict, list)):
				value = json.dumps(value)
			self.conn.execute(f"update jobmodel set {key} = ? where id = ?", (value, jid))
		self.conn.commit()

	def add_step(self, jid, name, status, data, start=NOW):
		self.conn.execute(
			"insert into stepmodel (name, job_id, status, data, start) values (?,?,?,?,?)",
			(name, jid, status, json.dumps(data), local(start)),
		)
		self.conn.commit()

	def close(self):
		self.conn.close()


class PressCase(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.press = FakePress(self.tmp.name)
		self.spool = os.path.join(self.tmp.name, "spool")

	def tearDown(self):
		self.press.close()
		self.tmp.cleanup()


class TestPressDiscovery(PressCase):
	def test_detect_and_describe(self):
		self.assertTrue(press.detect(self.press.agent_dir))
		self.assertFalse(press.detect(os.path.join(self.tmp.name, "nope")))
		path = self.press.add_bench(
			"bench-0001-000002-f1-mumbai", sites=("a.frappe.cloud", "b.frappe.cloud"), rq_port=11007,
			domains={"a.frappe.cloud": ["erp.example.com", "a.frappe.cloud"]},
		)
		os.makedirs(os.path.join(path, "sites", "archived"))  # Server.move_site leftovers: not a site
		os.makedirs(os.path.join(self.press.benches_dir, "junk"))  # no config.json: not a bench
		benches = press.discover_press_benches(self.press.agent_dir, nginx_access_log=self.press.access_log)
		self.assertEqual(len(benches), 1)
		b = benches[0]
		self.assertEqual(b["name"], "bench-0001-000002-f1-mumbai")
		self.assertEqual(b["sites"], ["a.frappe.cloud", "b.frappe.cloud"])
		self.assertEqual(b["logs_dir"], os.path.join(path, "logs"))
		self.assertEqual(b["redis_queue"], "redis://127.0.0.1:11007")  # rq_port, not the in-container 11000
		self.assertEqual(b["nginx_access_log"], self.press.access_log)
		self.assertEqual(b["site_domains"], {"erp.example.com": "a.frappe.cloud"})
		self.assertEqual(b["platform"], "press")

	def test_discover_benches_auto_detects_press(self):
		self.press.add_bench("bench-a", sites=("a.frappe.cloud",))
		found = discovery.discover_benches(
			platform=None, search_globs=(), nginx_default=None, press_agent_dir=self.press.agent_dir
		)
		self.assertEqual([b["name"] for b in found], ["bench-a"])
		generic = discovery.discover_benches(
			platform="generic", search_globs=(), nginx_default=None, press_agent_dir=self.press.agent_dir
		)
		self.assertEqual(generic, [])
		self.assertEqual(discovery.resolve_platform(None, os.path.join(self.tmp.name, "nope")), "generic")

	def test_explicit_press_bench_path(self):
		path = self.press.add_bench("bench-b", sites=("b.frappe.cloud",))
		found = discovery.discover_benches([path], nginx_default=None)
		self.assertEqual(found[0]["sites"], ["b.frappe.cloud"])
		self.assertEqual(found[0]["platform"], "press")

	def test_nginx_main_format_and_custom_domain(self):
		parser = NginxParser(
			"f1", site_benches={"a.frappe.cloud": "bench-a", "b.frappe.cloud": "bench-a"},
			site_aliases={"erp.example.com": "a.frappe.cloud"}, log_file=self.press.access_log,
		)
		# log_format main: ... "$http_referer" "$http_user_agent" "$http_x_forwarded_for" "$host" $request_time
		parser.feed('10.0.0.5 - - [29/Oct/2026:02:00:13 +0000] "GET /app HTTP/2.0" 502 166 "-" "Mozilla/5.0" "203.0.113.9" "erp.example.com" 0.002')
		parser.feed('10.0.0.5 - - [29/Oct/2026:02:00:14 +0000] "GET /api/method/ping HTTP/2.0" 200 20 "-" "curl" "-" "b.frappe.cloud" 0.010')
		metrics, events = parser.drain(NOW)
		by = {(m["site"], m["metric_name"]): m["value"] for m in metrics}
		self.assertEqual(by[("a.frappe.cloud", "http_502")], 1)
		self.assertEqual(by[("b.frappe.cloud", "http_requests")], 1)
		self.assertEqual(events[0]["site"], "a.frappe.cloud")
		self.assertEqual(events[0]["bench"], "bench-a")


class TestPressJobs(PressCase):
	def watcher(self):
		benches = lambda: {n for n in os.listdir(self.press.benches_dir)}  # noqa: E731
		return press.PressJobWatcher(
			"f1", agent_dir=self.press.agent_dir, state_path=os.path.join(self.spool, "press-jobs.json"), known_benches=benches
		)

	def test_first_run_skips_history_then_dedupes(self):
		self.press.add_bench("bench-old", sites=("a.frappe.cloud",))
		self.press.add_bench("bench-new")
		old = NOW - timedelta(days=2)
		self.press.add_job("Update Site Migrate", "Success", None, start=old, end=old)
		self.press.add_job("Backup Site", "Success", None, start=old, end=old)
		running = self.press.add_job(
			"Update Site Migrate", "Running",
			{"function": "update_site_migrate_job", "args": ["a.frappe.cloud", "bench-old", "bench-new", True, False, False], "kwargs": {}},
			start=NOW - timedelta(minutes=3), agent_job_id="b2c3d4e5",
		)
		events = self.watcher().poll(NOW)
		self.assertEqual(len(events), 1)
		ev = events[0]
		self.assertEqual((ev["type"], ev["category"], ev["site"], ev["bench"]), ("deployment", "press", "a.frappe.cloud", "bench-new"))
		self.assertEqual(ev["payload"]["agent_job_id"], "b2c3d4e5")
		self.assertEqual(ev["payload"]["local_job_id"], running)
		self.assertEqual(ev["timestamp"], "2026-10-29T02:07:00Z")

		# restart: nothing new; then the job fails → one failure event, once
		self.assertEqual(self.watcher().poll(NOW), [])
		self.press.set_job(running, status="Failure", end=NOW, data={"traceback": "Traceback ...\nOperationalError: (1054)"})
		self.press.add_step(running, "Migrate Site", "Failure", {"command": "docker exec bench-new bench --site a.frappe.cloud migrate"})
		events = self.watcher().poll(NOW)
		self.assertEqual([(e["type"], e["category"]) for e in events], [("error", "press_job_failed")])
		self.assertEqual(events[0]["payload"]["failed_steps"], ["Migrate Site"])
		self.assertIn("OperationalError", events[0]["payload"]["traceback"])
		self.assertEqual(self.watcher().poll(NOW), [])

	def test_short_job_finished_between_polls_scoped_from_steps(self):
		bench = self.press.add_bench("bench-0001-000003-f1", sites=("c.frappe.cloud",))
		w = self.watcher()
		self.assertEqual(w.poll(NOW), [])  # baseline
		# Site.migrate_job carries no bench/site in its args, and its data is
		# overwritten with the result when it ends: the steps name them.
		jid = self.press.add_job("Migrate Site", "Success", None, start=NOW - timedelta(seconds=40), end=NOW)
		self.press.add_step(jid, "Migrate Site", "Success", {
			"command": "docker exec -w /home/frappe/frappe-bench  bench-0001-000003-f1 bench --site c.frappe.cloud migrate",
			"directory": bench, "status": "Success",
		})
		events = w.poll(NOW)
		self.assertEqual(len(events), 1)
		self.assertEqual((events[0]["bench"], events[0]["site"]), ("bench-0001-000003-f1", "c.frappe.cloud"))
		self.assertEqual(w.poll(NOW), [])

	def test_unscoped_running_job_waits_then_ships_host_level(self):
		w = self.watcher()
		w.poll(NOW)
		self.press.add_job("Bench Restart", "Running", {"function": "restart_job", "args": [], "kwargs": {}}, start=NOW - timedelta(seconds=30))
		self.assertEqual(w.poll(NOW), [])  # bench not known yet
		events = w.poll(NOW + timedelta(minutes=3))
		self.assertEqual(len(events), 1)
		self.assertIsNone(events[0]["bench"])
		self.assertEqual(events[0]["host"], "f1")

	def test_stale_running_job_does_not_pin_the_floor(self):
		w = self.watcher()
		w.poll(NOW)
		self.press.add_job("Update Bench In Place", "Running", {"function": "update_inplace", "args": [["x.frappe.cloud"], "img", []], "kwargs": {}},
			enqueue=NOW - timedelta(days=3))
		later = self.press.add_job("New Bench", "Success", None, start=NOW, end=NOW)
		events = w.poll(NOW)
		self.assertEqual([e["payload"]["local_job_id"] for e in events], [later])
		self.assertEqual(w.state["floor"], later)

	def test_unreadable_db_returns_none(self):
		w = press.PressJobWatcher("f1", agent_dir=os.path.join(self.tmp.name, "missing"))
		self.assertIsNone(w.poll(NOW))


class TestPressFilesystemFallback(PressCase):
	def test_new_bench_and_site_move(self):
		state = os.path.join(self.spool, "press-benches.json")
		self.press.add_bench("bench-1", sites=("a.frappe.cloud",))
		w = press.PressBenchWatcher("f1", state_path=state)
		discover = lambda: press.discover_press_benches(self.press.agent_dir, nginx_access_log=None)  # noqa: E731
		self.assertEqual(w.poll(discover(), NOW), [])  # first snapshot
		self.press.add_bench("bench-2")
		self.press.move_site("a.frappe.cloud", "bench-1", "bench-2")
		events = press.PressBenchWatcher("f1", state_path=state).poll(discover(), NOW)
		self.assertEqual([e["title"] for e in events], ["Press: new bench bench-2", "Press: a.frappe.cloud moved to bench-2"])
		self.assertTrue(all(e["type"] == "deployment" for e in events))
		self.assertEqual(events[1]["payload"]["from_bench"], "bench-1")


class TestPressCollector(PressCase):
	def test_rediscovers_new_bench_and_ships_deployments(self):
		self.press.add_bench("bench-1", sites=("a.frappe.cloud",))
		ticks = [0.0]
		cfg = {
			"hostname": "f1", "collect_host_metrics": False, "auth_log": False, "benches": [], "platform": "press",
			"press": {"agent_dir": self.press.agent_dir, "nginx_access_log": self.press.access_log, "rediscover_interval": 60},
		}
		state_path = os.path.join(self.spool, "tail-state.json")
		c = Collector(cfg, state_path=state_path, clock=lambda: ticks[0])
		self.assertEqual([b["name"] for b in c.benches], ["bench-1"])
		c.collect(NOW)  # baseline for jobs

		# a deploy: new bench, site moved, its logs written before we notice
		path = self.press.add_bench("bench-2")
		self.press.move_site("a.frappe.cloud", "bench-1", "bench-2")
		with open(os.path.join(path, "logs", "web.error.log"), "w") as f:
			f.write(
				"2026-10-29 02:05:00,000 ERROR frappe Request failed\nTraceback (most recent call last):\n"
				'  File "apps/frappe/frappe/app.py", line 114, in application\n'
				"pymysql.err.ProgrammingError: (1146, \"Table 'tabWolf' doesn't exist\")\n"
				"2026-10-29 02:05:01,000 INFO frappe next\n"
			)
		self.press.add_job("Update Site Migrate", "Running",
			{"function": "update_site_migrate_job", "args": ["a.frappe.cloud", "bench-1", "bench-2", True, False, False], "kwargs": {}},
			start=NOW)
		ticks[0] = 61.0
		metrics, events = c.collect(NOW)
		self.assertEqual(sorted(b["name"] for b in c.benches), ["bench-1", "bench-2"])
		# bench-2's logs were read from the start: the traceback written before rediscovery is there
		self.assertIn(("error", "bench-2"), [(e["type"], e["bench"]) for e in events])
		deploys = [e for e in events if e["type"] == "deployment"]
		self.assertEqual([(e["bench"], e["site"]) for e in deploys], [("bench-2", "a.frappe.cloud")])
		site_benches = {m["site"]: m["bench"] for m in metrics if m["metric_name"] == "http_requests"}
		self.assertEqual(site_benches.get("a.frappe.cloud"), "bench-2")
		c.close()


if __name__ == "__main__":
	unittest.main()
