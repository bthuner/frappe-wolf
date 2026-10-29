import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent.parsers import auth, nginx, scheduler, traceback, worker  # noqa: E402
from wolf_agent.util import parse_log_timestamp  # noqa: E402

NOW = datetime(2026, 10, 29, 2, 1, 0, tzinfo=timezone.utc)


class TestTimestamps(unittest.TestCase):
	def test_iso_with_offset(self):
		self.assertEqual(parse_log_timestamp("2026-10-29T03:00:13.123+01:00 host sshd[1]: x"), "2026-10-29T02:00:13Z")

	def test_zulu(self):
		self.assertEqual(parse_log_timestamp("2026-10-29T02:00:13Z foo"), "2026-10-29T02:00:13Z")

	def test_python_logging_naive_is_local(self):
		ts = parse_log_timestamp("2026-10-29 02:00:13,123 ERROR scheduler boom")
		expected = datetime(2026, 10, 29, 2, 0, 13).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
		self.assertEqual(ts, expected)

	def test_syslog_year_rollover(self):
		ts = parse_log_timestamp("Dec 31 23:59:59 host sshd[1]: x", now=datetime(2027, 1, 1, 0, 0, 5))
		self.assertTrue(ts.startswith("2026-12-31") or ts.startswith("2027-01-01"))

	def test_garbage(self):
		self.assertIsNone(parse_log_timestamp("hello world"))


NGINX_COMBINED = (
	'10.0.0.5 - - [29/Oct/2026:03:00:13 +0100] "GET /api/method/frappe.auth.get_logged_user?_=1 HTTP/1.1" '
	'502 157 "https://a.example.com/app" "Mozilla/5.0 (X11; Linux x86_64)"'
)
NGINX_FRAPPE_MAIN = (
	'10.0.0.5 - - [29/Oct/2026:03:00:14 +0100] "POST /api/method/run_doc_method HTTP/2.0" '
	'500 42 "-" "curl/8.0" "203.0.113.9"'
)
NGINX_VHOST = (
	'b.example.com:443 10.0.0.6 - admin [29/Oct/2026:03:00:15 +0100] "GET /app/sales-invoice HTTP/1.1" '
	'200 5120 "-" "Mozilla/5.0"'
)
NGINX_HOST_TRAILING = (
	'10.0.0.7 - - [29/Oct/2026:03:00:16 +0100] "GET /assets/x.js HTTP/1.1" 504 0 "-" "Mozilla/5.0" "-" "b.example.com"'
)
NGINX_COMMON = '10.0.0.8 - - [29/Oct/2026:03:00:17 +0100] "GET / HTTP/1.0" 503 12'


class TestNginx(unittest.TestCase):
	def test_parse_combined(self):
		p = nginx.parse_line(NGINX_COMBINED)
		self.assertEqual(p["status"], 502)
		self.assertEqual(p["method"], "GET")
		self.assertEqual(p["route"], "/api/method/frappe.auth.get_logged_user")
		self.assertEqual(p["timestamp"], "2026-10-29T02:00:13Z")
		self.assertIsNone(p["vhost"])

	def test_parse_variants(self):
		self.assertEqual(nginx.parse_line(NGINX_FRAPPE_MAIN)["extra"], ["203.0.113.9"])
		v = nginx.parse_line(NGINX_VHOST)
		self.assertEqual(v["vhost"], "b.example.com")
		self.assertEqual(v["remote_addr"], "10.0.0.6")
		self.assertEqual(nginx.parse_line(NGINX_COMMON)["status"], 503)
		self.assertIsNone(nginx.parse_line("not an access log line"))
		self.assertIsNone(nginx.parse_line(""))

	def test_aggregation_single_site_bench(self):
		parser = nginx.NginxParser("h1", site_benches={"a.example.com": "main"}, default_site="a.example.com", default_bench="main")
		for _ in range(14):
			parser.feed(NGINX_COMBINED)
		parser.feed(NGINX_FRAPPE_MAIN)
		parser.feed(NGINX_COMMON)
		parser.feed('10.0.0.5 - - [29/Oct/2026:03:00:14 +0100] "GET / HTTP/1.1" 200 1 "-" "x"')
		metrics, events = parser.drain(NOW)
		by_name = {m["metric_name"]: m for m in metrics}
		self.assertEqual(by_name["http_requests"]["value"], 17)
		self.assertEqual(by_name["http_5xx"]["value"], 16)
		self.assertEqual(by_name["http_502"]["value"], 14)
		self.assertEqual(by_name["http_502"]["site"], "a.example.com")
		self.assertEqual(by_name["http_502"]["bench"], "main")
		self.assertEqual(by_name["http_502"]["timestamp"], "2026-10-29T02:01:00Z")

		self.assertEqual(len(events), 3)
		e502 = next(e for e in events if e["category"] == "http_502")
		self.assertEqual(e502["type"], "error")
		self.assertEqual(e502["severity"], "critical")
		self.assertEqual(e502["payload"]["count"], 14)
		self.assertEqual(len(e502["payload"]["sample_lines"]), 3)
		self.assertEqual(e502["title"], "502 on GET /api/method/frappe.auth.get_logged_user")
		self.assertEqual(e502["timestamp"], "2026-10-29T02:00:13Z")
		self.assertEqual(e502["source"], "nginx")
		e500 = next(e for e in events if e["payload"]["status"] == 500)
		self.assertEqual(e500["severity"], "warning")
		self.assertEqual(e500["category"], "http_5xx")
		self.assertEqual(next(e for e in events if e["payload"]["status"] == 503)["severity"], "critical")

		# drained: next interval starts from zero but keeps reporting known sites
		metrics, events = parser.drain(NOW)
		self.assertEqual(events, [])
		self.assertEqual({m["value"] for m in metrics}, {0})

	def test_site_resolution_multi_site(self):
		benches = {"a.example.com": "main", "b.example.com": "other"}
		parser = nginx.NginxParser("h1", site_benches=benches)
		parser.feed(NGINX_VHOST)
		parser.feed(NGINX_HOST_TRAILING)
		parser.feed(NGINX_COMBINED)  # no host info → unknown site
		metrics, events = parser.drain(NOW)
		req = {m["site"]: m["value"] for m in metrics if m["metric_name"] == "http_requests"}
		self.assertEqual(req["b.example.com"], 2)
		self.assertEqual(req[None], 1)
		self.assertEqual(req["a.example.com"], 0)
		e504 = next(e for e in events if e["payload"]["status"] == 504)
		self.assertEqual(e504["site"], "b.example.com")
		self.assertEqual(e504["bench"], "other")

	def test_site_from_log_file_name(self):
		parser = nginx.NginxParser(
			"h1", site_benches={"a.example.com": "main", "b.example.com": "main"},
			log_file="/var/log/nginx/b.example.com.access.log",
		)
		parser.feed(NGINX_COMBINED)
		_, events = parser.drain(NOW)
		self.assertEqual(events[0]["site"], "b.example.com")


TRACEBACK_WEB = """2026-10-29 02:00:13,001 ERROR frappe Request failed
Traceback (most recent call last):
  File "apps/frappe/frappe/app.py", line 114, in application
    response = frappe.api.handle(request)
  File "apps/erpnext/erpnext/stock/utils.py", line 42, in get_stock_balance
    raise frappe.ValidationError("Warehouse missing")
frappe.exceptions.ValidationError: Warehouse missing
2026-10-29 02:00:14,001 INFO frappe next request ok"""

TRACEBACK_CHAINED = """[2026-10-29 02:00:13 +0100] [4242] [ERROR] Error handling request /api/method/foo?x=1
Traceback (most recent call last):
  File "/x/db.py", line 10, in sql
    cursor.execute(q)
pymysql.err.OperationalError: (2006, 'MySQL server has gone away')

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/x/app.py", line 5, in application
    run()
  File "/x/handler.py", line 7, in run
    raise RuntimeError("db down")
RuntimeError: db down
"""


class TestTraceback(unittest.TestCase):
	def _feed(self, text, parser=None):
		parser = parser or traceback.TracebackParser("h1", bench="main", site="a.example.com", log_file="frappe.web.log")
		for line in text.splitlines():
			parser.feed(line)
		return parser

	def test_simple_traceback(self):
		_, events = self._feed(TRACEBACK_WEB).drain(NOW)
		self.assertEqual(len(events), 1)
		e = events[0]
		self.assertEqual((e["type"], e["category"], e["severity"]), ("error", "traceback", "warning"))
		self.assertEqual(e["payload"]["exception"], "frappe.exceptions.ValidationError")
		self.assertEqual(e["payload"]["message"], "Warehouse missing")
		self.assertIn("get_stock_balance", e["payload"]["last_frame"])
		self.assertIn("Traceback (most recent call last):", e["payload"]["traceback"])
		self.assertNotIn("next request ok", e["payload"]["traceback"])
		self.assertEqual(e["title"], "frappe.exceptions.ValidationError: Warehouse missing")
		self.assertEqual((e["site"], e["bench"]), ("a.example.com", "main"))
		expected_ts = datetime(2026, 10, 29, 2, 0, 13).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
		self.assertEqual(e["timestamp"], expected_ts)

	def test_chained_traceback_is_one_event(self):
		_, events = self._feed(TRACEBACK_CHAINED).drain(NOW)
		self.assertEqual(len(events), 1)
		e = events[0]
		self.assertEqual(e["payload"]["exception"], "RuntimeError")
		self.assertIn("OperationalError", e["payload"]["traceback"])
		self.assertEqual(e["payload"]["route"], "/api/method/foo")
		self.assertEqual(e["timestamp"], "2026-10-29T01:00:13Z")  # gunicorn logs +0100

	def test_same_fingerprint_aggregates(self):
		parser = self._feed(TRACEBACK_WEB)
		self._feed(TRACEBACK_WEB, parser)
		_, events = parser.drain(NOW)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["payload"]["count"], 2)

	def test_incomplete_block_waits_for_next_interval(self):
		lines = TRACEBACK_WEB.splitlines()
		parser = traceback.TracebackParser("h1")
		for line in lines[:4]:
			parser.feed(line)
		self.assertEqual(parser.drain(NOW)[1], [])
		for line in lines[4:]:
			parser.feed(line)
		self.assertEqual(len(parser.drain(NOW)[1]), 1)

	def test_exception_at_end_of_poll_flushes(self):
		parser = traceback.TracebackParser("h1")
		for line in TRACEBACK_WEB.splitlines()[:-1]:
			parser.feed(line)
		self.assertEqual(len(parser.drain(NOW)[1]), 1)


class TestScheduler(unittest.TestCase):
	def test_v15_plain_format(self):
		p = scheduler.parse_line(
			"2026-10-29 02:00:13,123 INFO scheduler Scheduled Job Failed: erpnext.accounts.utils.auto_create_fiscal_year for a.example.com"
		)
		self.assertEqual(p["method"], "erpnext.accounts.utils.auto_create_fiscal_year")
		self.assertEqual(p["site"], "a.example.com")
		self.assertIsNotNone(p["timestamp"])

	def test_other_statuses_ignored(self):
		self.assertIsNone(scheduler.parse_line("2026-10-29 02:00:13,123 INFO scheduler Scheduled Job Start: x.y.z for a"))
		self.assertIsNone(scheduler.parse_line("2026-10-29 02:00:14,123 INFO scheduler Scheduled Job Completed: x.y.z for a"))

	def test_json_format(self):
		p = scheduler.parse_line(
			'{"timestamp": "2026-10-29T02:00:13Z", "status": "failed", "method": "erpnext.x.daily", "site": "b.example.com", "exception": "KeyError"}'
		)
		self.assertEqual(p["method"], "erpnext.x.daily")
		self.assertEqual(p["site"], "b.example.com")
		self.assertEqual(p["timestamp"], "2026-10-29T02:00:13Z")
		self.assertEqual(p["error"], "KeyError")
		self.assertIsNone(scheduler.parse_line('{"status": "completed", "method": "x"}'))

	def test_enqueue_exception_and_background_jobs(self):
		p = scheduler.parse_line("2026-10-29 02:00:13,123 ERROR scheduler Exception in Enqueue Events for Site a.example.com")
		self.assertEqual((p["method"], p["site"]), ("enqueue_events", "a.example.com"))
		p = scheduler.parse_line(
			"2026-10-29 02:00:13,123 ERROR frappe.utils.background_jobs job erpnext.stock.reorder_item.reorder_item failed"
		)
		self.assertEqual(p["method"], "erpnext.stock.reorder_item.reorder_item")
		self.assertIsNone(scheduler.parse_line("2026-10-29 02:00:13,123 INFO scheduler all good"))

	def test_aggregation(self):
		parser = scheduler.SchedulerParser("h1", bench="main", site="a.example.com")
		parser.feed("2026-10-29 02:00:13,123 INFO scheduler Scheduled Job Failed: erpnext.x.nightly for a.example.com")
		parser.feed("2026-10-29 02:05:13,123 INFO scheduler Scheduled Job Failed: erpnext.x.nightly for a.example.com")
		parser.feed("2026-10-29 02:05:13,123 INFO scheduler Scheduled Job Failed: erpnext.y.other")
		_, events = parser.drain(NOW)
		self.assertEqual(len(events), 2)
		e = next(e for e in events if e["payload"]["method"] == "erpnext.x.nightly")
		self.assertEqual((e["type"], e["category"], e["severity"]), ("cron_failure", "scheduled_job", "critical"))
		self.assertEqual(e["payload"]["count"], 2)
		self.assertEqual(e["payload"]["job"], "nightly")
		self.assertEqual(e["bench"], "main")
		# site falls back to the per-site log's site
		other = next(e for e in events if e["payload"]["method"] == "erpnext.y.other")
		self.assertEqual(other["site"], "a.example.com")
		self.assertEqual(parser.drain(NOW)[1], [])


class TestWorker(unittest.TestCase):
	def test_patterns(self):
		crash = worker.parse_line(
			"02:00:13 Moving job to FailedJobRegistry (Work-horse terminated unexpectedly; waitpid returned 9 (signal 9); )"
		)
		self.assertEqual((crash["category"], crash["severity"]), ("worker_crash", "critical"))
		killed = worker.parse_line("Killed")
		self.assertEqual(killed["category"], "worker_killed")
		failed = worker.parse_line(
			"02:00:13 [Job 3f2a-11]: exception raised while executing (frappe.utils.background_jobs.execute_job)"
		)
		self.assertEqual(failed["category"], "job_failed")
		self.assertEqual(failed["method"], "frappe.utils.background_jobs.execute_job")
		self.assertEqual(failed["job_id"], "3f2a-11")
		self.assertIsNone(worker.parse_line("02:00:13 default: Job OK (3f2a-11)"))
		self.assertIsNone(worker.parse_line("02:00:13 Moving job to FailedJobRegistry"))

	def test_aggregation(self):
		parser = worker.WorkerParser("h1", bench="main")
		for _ in range(3):
			parser.feed("Work-horse terminated unexpectedly; waitpid returned 9")
		parser.feed("[Job abc]: exception raised while executing (frappe.utils.background_jobs.execute_job)")
		_, events = parser.drain(NOW)
		self.assertEqual(len(events), 2)
		crash = next(e for e in events if e["category"] == "worker_crash")
		self.assertEqual(crash["type"], "worker_failure")
		self.assertEqual(crash["payload"]["count"], 3)
		self.assertEqual(crash["bench"], "main")
		self.assertEqual(crash["timestamp"], "2026-10-29T02:01:00Z")


AUTH_LINES = [
	"Oct 29 02:00:01 bench-01 sshd[1001]: Invalid user admin from 198.51.100.7 port 50122",
	"Oct 29 02:00:02 bench-01 sshd[1001]: Failed password for invalid user admin from 198.51.100.7 port 50122 ssh2",
	"Oct 29 02:00:03 bench-01 sshd[1002]: Failed password for root from 198.51.100.7 port 50124 ssh2",
	"Oct 29 02:00:04 bench-01 sshd[1002]: message repeated 2 times: [ Failed password for root from 198.51.100.7 port 50124 ssh2]",
	"2026-10-29T03:00:05.000000+01:00 bench-01 sshd[1003]: Failed publickey for frappe from 2001:db8::1 port 2222 ssh2",
	"Oct 29 02:00:06 bench-01 sshd[1004]: Accepted publickey for root from 192.0.2.10 port 40000 ssh2: ED25519 SHA256:x",
	"Oct 29 02:00:07 bench-01 sshd[1005]: Accepted publickey for frappe from 192.0.2.10 port 40001 ssh2",
	"Oct 29 02:00:08 bench-01 sudo: frappe : TTY=pts/0 ; PWD=/home/frappe ; USER=root ; COMMAND=/bin/ls",
]


class TestAuth(unittest.TestCase):
	def test_parse_lines(self):
		parsed = [auth.parse_line(line) for line in AUTH_LINES]
		self.assertEqual(parsed[0]["kind"], "ssh_failed_login")
		self.assertIsNone(parsed[1])  # counted through the "Invalid user" line
		self.assertEqual(parsed[2]["user"], "root")
		self.assertEqual(parsed[3]["count"], 2)
		self.assertEqual(parsed[4]["ip"], "2001:db8::1")
		self.assertEqual(parsed[4]["timestamp"], "2026-10-29T02:00:05Z")
		self.assertEqual(parsed[5]["kind"], "ssh_root_login")
		self.assertIsNone(parsed[6])
		self.assertIsNone(parsed[7])

	def test_journal_cat_format(self):
		p = auth.parse_line("Failed password for root from 203.0.113.50 port 1234 ssh2")
		self.assertEqual(p["ip"], "203.0.113.50")
		self.assertIsNone(p["timestamp"])

	def test_aggregation_and_severity(self):
		parser = auth.AuthParser("bench-01")
		for line in AUTH_LINES:
			parser.feed(line)
		_, events = parser.drain(NOW)
		by_ip = {(e["category"], e["payload"]["ip"]): e for e in events}
		brute = by_ip[("ssh_failed_login", "198.51.100.7")]
		self.assertEqual(brute["type"], "security")
		self.assertEqual(brute["payload"]["count"], 4)
		self.assertEqual(sorted(brute["payload"]["users"]), ["admin", "root"])
		self.assertEqual(brute["severity"], "warning")
		self.assertIsNone(brute["bench"])
		root = by_ip[("ssh_root_login", "192.0.2.10")]
		self.assertEqual(root["severity"], "warning")

		for _ in range(25):
			parser.feed("Failed password for root from 203.0.113.50 port 1234 ssh2")
		_, events = parser.drain(NOW)
		self.assertEqual(events[0]["severity"], "critical")
		self.assertEqual(events[0]["payload"]["count"], 25)
		self.assertEqual(events[0]["timestamp"], "2026-10-29T02:01:00Z")


if __name__ == "__main__":
	unittest.main()
