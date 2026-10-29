"""Backups, TLS expiry, MariaDB, process states and their wiring (InfraChecks)."""

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import backups, collector, database, processes, tls_expiry  # noqa: E402
from wolf_agent.config import section  # noqa: E402

from test_discovery_config import make_bench  # noqa: E402

NOW = datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)


def touch(path, mtime):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as f:
		f.write("x")
	os.utime(path, (mtime, mtime))


class TestBackups(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = make_bench(self.tmp.name, sites=("a.example.com", "b.example.com", "c.example.com"))
		self.bench = {"name": "main", "path": self.path, "sites": ["a.example.com", "b.example.com", "c.example.com"]}
		self.wall = 1_800_000_000.0

	def tearDown(self):
		self.tmp.cleanup()

	def test_newest_database_dump(self):
		d = backups.backup_dir(self.path, "a.example.com")
		touch(os.path.join(d, "20261027_020000-a_example_com-database.sql.gz"), self.wall - 50 * 3600)
		touch(os.path.join(d, "20261028_020000-a_example_com-database.sql.gz"), self.wall - 26.5 * 3600)
		# newer, but not a database dump
		touch(os.path.join(d, "20261029_010000-a_example_com-files.tar"), self.wall - 3600)
		touch(os.path.join(d, "20261029_010000-a_example_com-site_config_backup.json"), self.wall - 3600)
		# b: encrypted dump; c: no backup at all
		touch(os.path.join(backups.backup_dir(self.path, "b.example.com"), "x-b-database-enc.sql.gz"), self.wall - 1800)
		got = {m["site"]: m for m in backups.backup_metrics([self.bench], "h1", NOW, wall=lambda: self.wall)}
		self.assertEqual(sorted(got), ["a.example.com", "b.example.com"])
		self.assertEqual(got["a.example.com"]["value"], 26.5)
		self.assertEqual(got["a.example.com"]["metric_name"], "backup_age_hours")
		self.assertEqual((got["a.example.com"]["bench"], got["a.example.com"]["host"]), ("main", "h1"))
		self.assertEqual(got["b.example.com"]["value"], 0.5)

	def test_override_directory(self):
		other = os.path.join(self.tmp.name, "offsite")
		touch(os.path.join(other, "20261029-c-database.sql"), self.wall - 7200)
		got = backups.backup_metrics([self.bench], "h1", NOW, wall=lambda: self.wall, overrides={"c.example.com": other})
		self.assertEqual([(m["site"], m["value"]) for m in got], [("c.example.com", 2.0)])


def openssl_cert(tmp, days):
	cert, key = os.path.join(tmp, f"c{days}.pem"), os.path.join(tmp, f"k{days}.pem")
	subprocess.run(
		["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
		 "-keyout", key, "-out", cert, "-days", str(days), "-subj", "/CN=shop.wolf-test.dev"],
		check=True, capture_output=True,
	)  # fmt: skip
	end = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", cert], check=True, capture_output=True, text=True)
	expected = datetime.strptime(end.stdout.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y GMT").replace(tzinfo=timezone.utc)
	return cert, key, expected


class FakeTLSServer:
	"""Accepts TLS handshakes on 127.0.0.1 with the given certificate and records the SNI name."""

	def __init__(self, cert, key):
		self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
		self.ctx.load_cert_chain(cert, key)
		self.ctx.sni_callback = lambda sock, name, ctx: self.sni.append(name)
		self.sni = []
		self.sock = socket.socket()
		self.sock.bind(("127.0.0.1", 0))
		self.sock.listen(5)
		self.port = self.sock.getsockname()[1]
		self.thread = threading.Thread(target=self.serve, daemon=True)
		self.thread.start()

	def serve(self):
		while True:
			try:
				conn, _ = self.sock.accept()
			except OSError:
				return
			try:
				with self.ctx.wrap_socket(conn, server_side=True) as tls:
					tls.recv(1)
			except (OSError, ssl.SSLError):
				pass

	def close(self):
		self.sock.close()


@unittest.skipIf(shutil.which("openssl") is None, "openssl CLI not available to generate a test certificate")
class TestTLS(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.tmp = tempfile.TemporaryDirectory()
		cls.cert, cls.key, cls.expiry = openssl_cert(cls.tmp.name, 10)
		cls.server = FakeTLSServer(cls.cert, cls.key)

	@classmethod
	def tearDownClass(cls):
		cls.server.close()
		cls.tmp.cleanup()

	def test_der_not_after_matches_openssl(self):
		for days in (10, 10000):  # UTCTime, then GeneralizedTime (after 2049)
			cert, _, expected = openssl_cert(self.tmp.name, days)
			with open(cert) as f:
				der = ssl.PEM_cert_to_DER_cert(f.read())
			self.assertEqual(tls_expiry.not_after(der), expected)

	def test_der_rejects_garbage(self):
		for blob in (b"", b"\x30\x05abc", b"\x04\x00"):
			with self.assertRaises(tls_expiry.DERError):
				tls_expiry.not_after(blob)

	def test_handshake_with_sni(self):
		got = tls_expiry.fetch_not_after("shop.wolf-test.dev", port=self.server.port, timeout=3, connect_host="127.0.0.1")
		self.assertEqual(got, self.expiry)
		self.assertIn("shop.wolf-test.dev", self.server.sni)

	def test_tls_metrics(self):
		now = self.expiry - timedelta(days=2, hours=12)
		domains = {"shop.wolf-test.dev": ("main", "shop.wolf-test.dev"), "closed.wolf-test.dev": ("main", "x")}

		def fetch(domain, **kw):
			if domain.startswith("closed"):
				raise ConnectionRefusedError("refused")
			return tls_expiry.fetch_not_after(domain, port=self.server.port, timeout=3, connect_host="127.0.0.1")

		got = tls_expiry.tls_metrics(domains, "h1", now=now, fetch=fetch)
		self.assertEqual(len(got), 1)  # the unreachable domain is skipped
		self.assertEqual(got[0]["metric_name"], "tls_days_left")
		self.assertEqual(got[0]["value"], 2.5)
		self.assertEqual((got[0]["site"], got[0]["bench"], got[0]["labels"]), ("shop.wolf-test.dev", "main", {"domain": "shop.wolf-test.dev"}))


class TestTLSDomains(unittest.TestCase):
	def test_public_names(self):
		for name in ("erp.dokos.io", "shop.client.fr"):
			self.assertTrue(tls_expiry.is_public_name(name), name)
		for name in ("localhost", "site1.localhost", "wolf.local", "x.test", "10.0.0.1", "::1", "bench", "a.example.com", "example.org", ""):
			self.assertFalse(tls_expiry.is_public_name(name), name)

	def test_site_domains(self):
		benches = [{"name": "main", "sites": ["erp.dokos.io", "site1.localhost"], "site_domains": {"shop.client.fr": "erp.dokos.io", "dev.local": "erp.dokos.io"}}]
		got = tls_expiry.site_domains(benches, extra=["status.dokos.io", {"domain": "api.client.fr", "site": "erp.dokos.io"}], exclude=["shop.client.fr"])
		self.assertEqual(got, {
			"api.client.fr": (None, "erp.dokos.io"),
			"erp.dokos.io": ("main", "erp.dokos.io"),
			"status.dokos.io": (None, None),
		})


class FakeRun:
	"""Stands in for subprocess.run: answers by matching the command line."""

	def __init__(self, answers):
		self.answers = answers
		self.calls = []
		self.option_files = []

	def __call__(self, args, **kw):
		self.calls.append(args)
		for arg in args:
			if arg.startswith("--defaults-extra-file="):
				path = arg.split("=", 1)[1]
				with open(path) as f:
					self.option_files.append((path, oct(os.stat(path).st_mode & 0o777), f.read()))
		for match, answer in self.answers:
			if match(args):
				if isinstance(answer, Exception):
					raise answer
				code, out, err = answer
				return SimpleNamespace(returncode=code, stdout=out, stderr=err)
		return SimpleNamespace(returncode=1, stdout="", stderr="unexpected command")


def is_replica_query(args):
	return "-E" in args


STATUS_OUT = "Slow_queries\t40\nThreads_connected\t30\nmax_connections\t151\n"
REPLICA_OUT = (
	"*************************** 1. row ***************************\n"
	"               Slave_IO_State: Waiting for master to send event\n"
	"             Slave_IO_Running: Yes\n"
	"            Slave_SQL_Running: Yes\n"
	"        Seconds_Behind_Master: 420\n"
)


class TestMariaDB(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = make_bench(self.tmp.name, sites=("a.example.com",))
		with open(os.path.join(self.path, "sites", "a.example.com", "site_config.json"), "w") as f:
			json.dump({"db_name": "_abc", "db_password": 'p"w\\d'}, f)
		with open(os.path.join(self.path, "sites", "common_site_config.json"), "w") as f:
			json.dump({"db_host": "10.0.0.5", "db_port": 3307}, f)
		self.bench = {"name": "main", "path": self.path, "sites": ["a.example.com"]}

	def tearDown(self):
		self.tmp.cleanup()

	def test_bench_target(self):
		self.assertEqual(
			database.bench_target(self.bench),
			{"host": "10.0.0.5", "port": 3307, "user": "_abc", "password": 'p"w\\d'},
		)
		with open(os.path.join(self.path, "sites", "common_site_config.json"), "w") as f:
			json.dump({"db_type": "postgres"}, f)
		self.assertIsNone(database.bench_target(self.bench))

	def probe(self, answers):
		run = FakeRun(answers)
		probes = database.build_probes([self.bench], "h1", section({}, "mariadb"), which=lambda n: "/usr/bin/" + n, run=run)
		self.assertEqual(len(probes), 1)
		return probes[0], run

	def test_status_and_replication(self):
		probe, run = self.probe([(is_replica_query, (0, REPLICA_OUT, "")), (lambda a: True, (0, STATUS_OUT, ""))])
		first = {m["metric_name"]: m for m in probe.collect(NOW)}
		self.assertEqual(first["db_threads_connected"]["value"], 30)
		self.assertEqual(first["db_connections_used_percent"]["value"], 19.87)
		self.assertEqual(first["db_replication_lag_seconds"]["value"], 420)
		self.assertEqual(first["db_replication_running"]["value"], 1)
		self.assertEqual(first["db_threads_connected"]["labels"], {"db": "10.0.0.5:3307"})
		self.assertEqual(first["db_threads_connected"]["bench"], "main")
		self.assertNotIn("db_slow_queries", first)  # needs a baseline
		run.answers[1] = (lambda a: True, (0, STATUS_OUT.replace("\t40", "\t52"), ""))
		second = {m["metric_name"]: m for m in probe.collect(NOW)}
		self.assertEqual(second["db_slow_queries"]["value"], 12)

		# the password never appears on the command line, only in a private option file
		self.assertEqual(run.calls[0][0], "/usr/bin/mariadb")
		self.assertTrue(run.calls[0][1].startswith("--defaults-extra-file="))
		self.assertFalse(any("p\"w" in a for call in run.calls for a in call))
		path, mode, content = run.option_files[0]
		self.assertEqual(mode, "0o600")
		self.assertIn('password="p\\"w\\\\d"', content)
		self.assertIn('user="_abc"', content)
		self.assertFalse(os.path.exists(path))
		self.assertIn(["-h", "10.0.0.5"], [call[i:i + 2] for call in run.calls for i in range(len(call))])

	def test_replication_denied_is_skipped_for_good(self):
		denied = (1, "", "ERROR 1227 (42000): Access denied; you need (at least one of) the REPLICATION CLIENT privilege(s)")
		probe, run = self.probe([(is_replica_query, denied), (lambda a: True, (0, STATUS_OUT, ""))])
		names = {m["metric_name"] for m in probe.collect(NOW)}
		self.assertEqual(names, {"db_threads_connected", "db_connections_used_percent"})
		probe.collect(NOW)
		self.assertEqual(sum(1 for c in run.calls if is_replica_query(c)), 1)

	def test_not_a_replica_and_stopped_replica(self):
		probe, run = self.probe([(is_replica_query, (0, "", "")), (lambda a: True, (0, STATUS_OUT, ""))])
		self.assertNotIn("db_replication_running", {m["metric_name"] for m in probe.collect(NOW)})
		stopped = REPLICA_OUT.replace("SQL_Running: Yes", "SQL_Running: No").replace(": 420", ": NULL")
		run.answers[0] = (is_replica_query, (0, stopped, ""))
		got = {m["metric_name"]: m["value"] for m in probe.collect(NOW)}
		self.assertEqual(got["db_replication_running"], 0)
		self.assertNotIn("db_replication_lag_seconds", got)

	def test_connection_failure_raises(self):
		probe, _ = self.probe([(lambda a: True, (1, "", "ERROR 2002 (HY000): Can't connect"))])
		with self.assertRaises(RuntimeError):
			probe.collect(NOW)

	def test_no_cli_or_explicit_credentials(self):
		self.assertEqual(database.build_probes([self.bench], "h1", section({}, "mariadb"), which=lambda n: None), [])
		cfg = section({"mariadb": {"user": "monitor", "password": "x", "host": "db1"}}, "mariadb")
		probes = database.build_probes([self.bench], "h1", cfg, which=lambda n: "/usr/bin/" + n if n == "mysql" else None)
		self.assertEqual(len(probes), 1)
		self.assertEqual((probes[0].cli, probes[0].target["user"], probes[0].bench), ("/usr/bin/mysql", "monitor", None))


SUPERVISOR_OUT = """\
frappe-bench-redis:frappe-bench-redis-cache            RUNNING   pid 101, uptime 2 days, 3:04:05
frappe-bench-web:frappe-bench-frappe-web               RUNNING   pid 102, uptime 2 days, 3:04:05
frappe-bench-workers:frappe-bench-frappe-short-worker-0 FATAL     Exited too quickly (process log may have details)
frappe-bench-workers:frappe-bench-frappe-long-worker-0 STOPPED   Oct 29 01:58 AM
frappe-bench-2-web:frappe-bench-2-frappe-web           FATAL     Exited too quickly
other-app                                              EXITED    Oct 29 01:00 AM
"""
SYSTEMD_OUT = """\
pilot-erp-web.service           loaded active   running      Pilot erp web
pilot-erp-worker_short_0.service loaded failed   failed       Pilot erp worker
● pilot-erp-schedule.service    loaded activating auto-restart Pilot erp schedule
pilot-erp-backup.service        loaded inactive dead         Pilot erp oneshot
nginx.service                   loaded failed   failed       nginx
"""


class TestProcesses(unittest.TestCase):
	def watcher(self, outputs, manager="auto"):
		def run(args, **kw):
			out = outputs.get(os.path.basename(args[0]))
			if isinstance(out, Exception):
				raise out
			return SimpleNamespace(returncode=3, stdout=out or "", stderr="")

		return processes.ProcessWatcher("h1", manager, which=lambda n: f"/usr/bin/{n}" if n in outputs else None, run=run)

	def test_supervisor(self):
		w = self.watcher({"supervisorctl": SUPERVISOR_OUT})
		benches = [{"name": "frappe-bench"}, {"name": "frappe-bench-2"}, {"name": "press-bench", "platform": "press"}]
		metrics, events = w.collect(benches, NOW)
		down = {m["bench"]: m["value"] for m in metrics}
		# STOPPED counts as down but is not an event; frappe-bench-2-* belongs to the longer name
		self.assertEqual(down, {"frappe-bench": 2, "frappe-bench-2": 1})
		self.assertEqual(metrics[0]["metric_name"], "bench_processes_down")
		self.assertEqual(
			sorted((e["bench"], e["payload"]["process"], e["payload"]["state"]) for e in events),
			[("frappe-bench", "frappe-bench-workers:frappe-bench-frappe-short-worker-0", "FATAL"),
			 ("frappe-bench-2", "frappe-bench-2-web:frappe-bench-2-frappe-web", "FATAL")],
		)  # fmt: skip
		self.assertEqual({(e["type"], e["category"], e["severity"]) for e in events}, {("worker_failure", "process_down", "critical")})
		# still down on the next cycle: no new event, the metric keeps counting
		metrics, events = w.collect(benches, NOW)
		self.assertEqual(events, [])
		self.assertEqual(sum(m["value"] for m in metrics), 3)

	def test_systemd_fallback(self):
		# supervisorctl present but cannot reach supervisord: no status lines
		w = self.watcher({"supervisorctl": "unix:///var/run/supervisor.sock no such file\n", "systemctl": SYSTEMD_OUT})
		metrics, events = w.collect([{"name": "pilot-erp"}], NOW)
		self.assertEqual([(m["bench"], m["value"], m["labels"]) for m in metrics], [("pilot-erp", 2, {"manager": "systemd"})])
		self.assertEqual(
			sorted(e["payload"]["state"] for e in events), ["failed"]
		)

	def test_nothing_found(self):
		w = self.watcher({"systemctl": SYSTEMD_OUT})
		self.assertEqual(w.collect([{"name": "frappe-bench"}], NOW), ([], []))
		w = self.watcher({"supervisorctl": OSError("boom")}, manager="supervisor")
		self.assertEqual(w.collect([{"name": "frappe-bench"}], NOW), ([], []))


class TestInfraChecks(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.ticks = [0.0]
		self.path = make_bench(self.tmp.name, sites=("a.example.com",), redis=None)
		touch(os.path.join(backups.backup_dir(self.path, "a.example.com"), "x-database.sql.gz"), time.time() - 3600)
		self.benches = [{"name": "main", "path": self.path, "sites": ["a.example.com"], "site_domains": {"erp.dokos.io": "a.example.com"}}]

	def tearDown(self):
		self.tmp.cleanup()

	def infra(self, sync, **cfg):
		base = {"mariadb": False, "processes": False, "backups": {"interval": 600}, "tls": {"interval": 3600}}
		base.update(cfg)
		return collector.InfraChecks(base, "h1", clock=lambda: self.ticks[0], sync=sync)

	def names(self, infra):
		return [m["metric_name"] for m in infra.collect(self.benches, NOW)[0]]

	def test_intervals_and_background_tls(self):
		calls = []
		gate = threading.Event()

		def fake_tls(domains, host, **kw):
			calls.append(sorted(domains))
			gate.wait(5)
			return [{"metric_name": "tls_days_left", "value": 30}]

		infra = self.infra(sync=False)
		with mock.patch.object(tls_expiry, "tls_metrics", fake_tls):
			self.assertEqual(self.names(infra), ["backup_age_hours"])  # TLS started in the background
			self.ticks[0] = 30
			self.assertEqual(self.names(infra), [])  # backups not due, TLS still running
			gate.set()
			infra.tls_thread.join(5)
			self.ticks[0] = 60
			self.assertEqual(self.names(infra), ["tls_days_left"])  # delivered with the next cycle
			self.ticks[0] = 700
			self.assertEqual(self.names(infra), ["backup_age_hours"])
		self.assertEqual(calls, [["erp.dokos.io"]])  # a.example.com is a documentation name

	def test_sync_and_disabled(self):
		with mock.patch.object(tls_expiry, "tls_metrics", lambda *a, **k: [{"metric_name": "tls_days_left"}]):
			self.assertEqual(sorted(self.names(self.infra(sync=True))), ["backup_age_hours", "tls_days_left"])
			self.assertEqual(self.names(self.infra(sync=True, tls=False, backups={"enabled": False})), [])

	def test_press_benches_skip_backups(self):
		self.benches[0]["platform"] = "press"
		self.assertEqual(self.names(self.infra(sync=True, tls=False)), [])
		self.assertEqual(self.names(self.infra(sync=True, tls=False, backups={"press": True})), ["backup_age_hours"])

	def test_one_failing_check_does_not_stop_the_others(self):
		with mock.patch.object(collector, "backup_metrics", side_effect=RuntimeError("boom")):
			with mock.patch.object(tls_expiry, "tls_metrics", lambda *a, **k: [{"metric_name": "tls_days_left"}]):
				with self.assertLogs("wolf_agent.collector", "ERROR"):
					self.assertEqual(self.names(self.infra(sync=True)), ["tls_days_left"])


if __name__ == "__main__":
	unittest.main()
