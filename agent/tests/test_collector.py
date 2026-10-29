import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import cli, metrics  # noqa: E402
from wolf_agent.collector import Collector  # noqa: E402

from test_discovery_config import make_bench  # noqa: E402
from test_shipper import FakeWolf  # noqa: E402

PROC_STAT_1 = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 100 0 100 800 0 0 0 0 0 0\n"
PROC_STAT_2 = "cpu  150 0 150 900 0 0 0 0 0 0\ncpu0 150 0 150 900 0 0 0 0 0 0\n"
MEMINFO = "MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 250 kB\nSwapTotal: 200 kB\nSwapFree: 150 kB\n"
MOUNTS = (
	"/dev/sda1 / ext4 rw 0 0\n"
	"proc /proc proc rw 0 0\n"
	"tmpfs /run tmpfs rw 0 0\n"
	"/dev/sda1 /home ext4 rw 0 0\n"
	"overlay /var/lib/docker/overlay2/x/merged overlay rw 0 0\n"
	"/dev/sdb1 /devdata xfs rw 0 0\n"
)


def write(path, text, mode="w"):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, mode) as f:
		f.write(text)


class TestHostMetrics(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.proc = self.tmp.name
		write(os.path.join(self.proc, "stat"), PROC_STAT_1)
		write(os.path.join(self.proc, "meminfo"), MEMINFO)
		write(os.path.join(self.proc, "loadavg"), "1.50 1.00 0.50 2/300 4242\n")
		write(os.path.join(self.proc, "mounts"), MOUNTS)

	def tearDown(self):
		self.tmp.cleanup()

	def test_pure_helpers(self):
		self.assertEqual(metrics.read_cpu_times(self.proc), (800, 1000))
		self.assertEqual(metrics.cpu_usage((800, 1000), (900, 1200)), 50.0)
		self.assertEqual(metrics.cpu_usage((1, 1), (1, 1)), 0.0)
		mem = metrics.memory_percentages(metrics.read_meminfo(self.proc))
		self.assertEqual(mem, {"memory_used_percent": 75.0, "swap_used_percent": 25.0})
		self.assertEqual(metrics.memory_percentages({"MemTotal": 100, "MemFree": 10, "Buffers": 10, "Cached": 30}), {"memory_used_percent": 50.0})
		self.assertEqual(metrics.real_mounts(self.proc), ["/", "/devdata"])

	def test_bind_mounted_files_are_skipped(self):
		bind_file = os.path.join(self.tmp.name, "resolv.conf")
		bind_dir = os.path.join(self.tmp.name, "data")
		write(bind_file, "nameserver 127.0.0.11\n")
		os.makedirs(bind_dir)
		write(
			os.path.join(self.proc, "mounts"),
			f"overlay / overlay rw 0 0\n/dev/nvme0n1p3 {bind_file} ext4 rw 0 0\n/dev/nvme0n1p3 {bind_dir} ext4 rw 0 0\n",
		)
		self.assertEqual(metrics.real_mounts(self.proc), [bind_dir])

	def test_collect(self):
		hm = metrics.HostMetrics("h1", proc=self.proc)
		with mock.patch.object(metrics, "disk_used_percent", return_value=42.0), mock.patch.object(metrics.time, "sleep"):
			first = {m["metric_name"]: m for m in hm.collect()}
			write(os.path.join(self.proc, "stat"), PROC_STAT_2)
			second = hm.collect()
		self.assertEqual(first["cpu_usage"]["value"], 0.0)  # identical samples
		self.assertEqual(first["load_1m"]["value"], 1.5)
		self.assertIn("load_per_core", first)
		cpu = next(m for m in second if m["metric_name"] == "cpu_usage")
		self.assertEqual(cpu["value"], 50.0)
		disks = [m for m in second if m["metric_name"] == "disk_used_percent"]
		self.assertEqual([d["labels"]["mount"] for d in disks], ["/", "/devdata"])
		self.assertEqual(cpu["host"], "h1")

	def test_real_statvfs(self):
		value = metrics.disk_used_percent("/")
		self.assertTrue(0 <= value <= 100)


TRACEBACK = (
	"Traceback (most recent call last):\n"
	'  File "apps/frappe/frappe/app.py", line 1, in application\n'
	"    boom()\n"
	"ZeroDivisionError: division by zero\n"
	"next\n"
)


class TestCollectorEndToEnd(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.bench = make_bench(self.tmp.name, sites=("a.example.com",), redis=None)
		self.nginx = os.path.join(self.tmp.name, "nginx", "access.log")
		self.auth = os.path.join(self.tmp.name, "auth.log")
		for path in (self.nginx, self.auth):
			write(path, "")
		for name in ("frappe.web.log", "scheduler.log", "worker.error.log"):
			write(os.path.join(self.bench, "logs", name), "")
		self.cfg = {
			"hostname": "bench-01",
			"benches": [
				{
					"name": "main",
					"path": self.bench,
					"sites": ["a.example.com"],
					"logs_dir": os.path.join(self.bench, "logs"),
					"nginx_access_log": self.nginx,
					"redis_queue": None,
				}
			],
			"extra_logs": [],
			"auth_log": self.auth,
			"collect_host_metrics": False,
		}

	def tearDown(self):
		self.tmp.cleanup()

	def test_cycle(self):
		state = os.path.join(self.tmp.name, "spool", "tail-state.json")
		collector = Collector(self.cfg, state_path=state)
		self.assertEqual(collector.collect()[1], [])  # first cycle only records EOF offsets

		logs = os.path.join(self.bench, "logs")
		write(self.nginx, '1.2.3.4 - - [29/Oct/2026:03:00:13 +0100] "GET /app HTTP/1.1" 502 0 "-" "x"\n', "a")
		write(os.path.join(logs, "frappe.web.log"), TRACEBACK, "a")
		write(os.path.join(logs, "scheduler.log"), "2026-10-29 02:00:13,1 INFO scheduler Scheduled Job Failed: erpnext.x.nightly for a.example.com\n", "a")
		write(os.path.join(logs, "worker.error.log"), "Work-horse terminated unexpectedly; waitpid returned 9\n", "a")
		write(self.auth, "Oct 29 02:00:01 bench-01 sshd[1]: Invalid user oracle from 198.51.100.7 port 1\n", "a")
		write(os.path.join(self.bench, "sites", "a.example.com", "logs", "frappe.web.log"), "", "a")

		ms, events = collector.collect()
		collector.close()
		types = sorted((e["type"], e["category"]) for e in events)
		self.assertEqual(
			types,
			[
				("cron_failure", "scheduled_job"),
				("error", "http_502"),
				("error", "traceback"),
				("security", "ssh_failed_login"),
				("worker_failure", "worker_crash"),
			],
		)
		http_502 = next(m for m in ms if m["metric_name"] == "http_502")
		self.assertEqual((http_502["value"], http_502["site"], http_502["bench"]), (1, "a.example.com", "main"))
		self.assertTrue(all(e["host"] == "bench-01" for e in events))
		self.assertTrue(os.path.exists(state))

	def test_shared_nginx_log_across_benches(self):
		other = make_bench(os.path.join(self.tmp.name, "b2"), name="other-bench", sites=("b.example.com",), redis=None)
		self.cfg["benches"].append(
			{"name": "other", "path": other, "sites": ["b.example.com"], "nginx_access_log": self.nginx}
		)
		collector = Collector(self.cfg, from_start=True)
		nginx_sources = [s for s in collector.sources if s.tailer.path == self.nginx]
		self.assertEqual(len(nginx_sources), 1)
		write(self.nginx, 'b.example.com 1.2.3.4 - - [29/Oct/2026:03:00:13 +0100] "GET / HTTP/1.1" 500 0 "-" "x"\n', "a")
		_, events = collector.collect()
		collector.close()
		e = next(e for e in events if e["source"] == "nginx")
		self.assertEqual((e["site"], e["bench"]), ("b.example.com", "other"))


class TestCli(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.server = FakeWolf()
		self.bench = make_bench(self.tmp.name, sites=("a.example.com",), redis=None)
		self.config = os.path.join(self.tmp.name, "etc", "config.json")

	def tearDown(self):
		self.server.shutdown()
		self.server.server_close()
		self.tmp.cleanup()

	def run_cli(self, *argv):
		out = io.StringIO()
		with redirect_stdout(out):
			code = cli.main(["--config", self.config, *argv])
		return code, out.getvalue()

	def test_register_then_check_and_mark_deployment(self):
		code, out = self.run_cli("register", "--url", self.server.url, "--token", "tok", "--bench", self.bench, "--hostname", "bench-01")
		self.assertEqual(code, 0)
		self.assertIn("WAG-0007", out)
		with open(self.config) as f:
			cfg = json.load(f)
		self.assertEqual(cfg["agent_id"], "WAG-0007")
		self.assertEqual(cfg["push_interval"], 20)
		self.assertEqual(cfg["benches"][0]["sites"], ["a.example.com"])
		self.assertEqual(oct(os.stat(self.config).st_mode & 0o777), "0o600")

		# check: no network, prints a payload
		cfg["auth_log"] = False
		cfg["collect_host_metrics"] = False
		with open(self.config, "w") as f:
			json.dump(cfg, f)
		before = len(self.server.requests)
		code, out = self.run_cli("check")
		self.assertEqual(code, 0)
		self.assertEqual(json.loads(out)["hostname"], "bench-01")
		self.assertEqual(len(self.server.requests), before)

		code, _ = self.run_cli("mark-deployment", "--bench", "frappe-bench", "--note", "update")
		self.assertEqual(code, 0)
		self.assertTrue(self.server.requests[-1]["path"].endswith("ingest.deployment"))

	def test_register_failure_exits(self):
		self.server.status = 403
		with self.assertRaises(SystemExit) as ctx:
			self.run_cli("register", "--url", self.server.url, "--token", "bad", "--bench", self.bench)
		self.assertIn("registration failed", str(ctx.exception.code))

	def test_run_stops_on_auth_error(self):
		cfg = {
			"url": self.server.url, "agent_id": "WAG-1", "agent_secret": "s", "hostname": "h",
			"spool_dir": os.path.join(self.tmp.name, "spool"), "benches": [], "auth_log": False,
			"collect_host_metrics": False,
		}
		os.makedirs(os.path.dirname(self.config), exist_ok=True)
		with open(self.config, "w") as f:
			json.dump(cfg, f)
		self.server.status = 401
		with mock.patch.object(cli.signal, "signal"):
			code, _ = self.run_cli("run")
		self.assertEqual(code, cli.EXIT_AUTH)

	def test_discover(self):
		code, out = self.run_cli("discover", "--bench", self.bench)
		self.assertEqual(json.loads(out)[0]["name"], "frappe-bench")


if __name__ == "__main__":
	unittest.main()
