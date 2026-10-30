import fnmatch
import os
import socketserver
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent.redis_queue import (  # noqa: E402
	RedisClient,
	RedisError,
	RedisProbe,
	parse_info,
	parse_rq_time,
	queue_lengths,
	queue_metrics,
)

WRONGTYPE = b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"


class FakeRedisHandler(socketserver.StreamRequestHandler):
	"""Speaks enough RESP for AUTH/SELECT/SCAN/LLEN/ZCARD/ZRANGE/HGET/SMEMBERS/EXISTS/INFO.

	Key values: int = list length, ("zset", [members]), dict = hash, set = set.
	"""

	def read_command(self):
		line = self.rfile.readline()
		if not line:
			return None
		n = int(line[1:-2])
		args = []
		for _ in range(n):
			size = int(self.rfile.readline()[1:-2])
			args.append(self.rfile.read(size + 2)[:-2].decode())
		return args

	def bulk(self, s):
		if s is None:
			return b"$-1\r\n"
		data = s.encode()
		return b"$%d\r\n%s\r\n" % (len(data), data)

	def array(self, items):
		return b"*%d\r\n" % len(items) + b"".join(self.bulk(i) for i in items)

	def handle(self):
		server = self.server
		while True:
			cmd = self.read_command()
			if cmd is None:
				return
			server.commands.append(cmd)
			name = cmd[0].upper()
			if name == "AUTH":
				ok = cmd[-1] == server.password
				self.wfile.write(b"+OK\r\n" if ok else b"-WRONGPASS invalid password\r\n")
			elif name == "SELECT":
				self.wfile.write(b"+OK\r\n")
			elif name == "SCAN":
				# two pages to exercise the cursor loop
				keys = sorted(k for k in server.keys if fnmatch.fnmatchcase(k, cmd[3]))
				half = len(keys) // 2
				cursor, page = ("7", keys[:half]) if cmd[1] == "0" else ("0", keys[half:])
				self.wfile.write(b"*2\r\n" + self.bulk(cursor) + self.array(page))
			elif name == "LLEN":
				value = server.keys.get(cmd[1])
				self.wfile.write(b":%d\r\n" % value if isinstance(value, int) else WRONGTYPE)
			elif name in ("ZCARD", "ZRANGE"):
				value = server.keys.get(cmd[1], ("zset", []))
				if not (isinstance(value, tuple) and value[0] == "zset"):
					self.wfile.write(WRONGTYPE)
				elif name == "ZCARD":
					self.wfile.write(b":%d\r\n" % len(value[1]))
				else:
					self.wfile.write(self.array(value[1]))
			elif name == "HGET":
				value = server.keys.get(cmd[1]) or {}
				self.wfile.write(self.bulk(value.get(cmd[2]) if isinstance(value, dict) else None))
			elif name == "SMEMBERS":
				self.wfile.write(self.array(sorted(server.keys.get(cmd[1]) or ())))
			elif name == "EXISTS":
				self.wfile.write(b":%d\r\n" % (cmd[1] in server.keys))
			elif name == "INFO" and server.info is not None:
				self.wfile.write(self.bulk(server.info.get(cmd[1], "") if len(cmd) > 1 else ""))
			else:
				self.wfile.write(b"-ERR unknown command\r\n")


class FakeRedis(socketserver.ThreadingTCPServer):
	allow_reuse_address = True
	daemon_threads = True

	def __init__(self, keys, password=None):
		super().__init__(("127.0.0.1", 0), FakeRedisHandler)
		self.keys = keys
		self.info = None
		self.password = password
		self.commands = []
		threading.Thread(target=self.serve_forever, daemon=True).start()

	@property
	def url(self):
		return f"redis://127.0.0.1:{self.server_address[1]}"


class TestRedis(unittest.TestCase):
	def setUp(self):
		self.server = FakeRedis({
			"rq:queue:abc123:short": 3,
			"rq:queue:abc123:default": 0,
			"rq:queue:abc123:long": 812,
			"rq:queue:weird": "a hash",
			"rq:queues": 99,
		})

	def tearDown(self):
		self.server.shutdown()
		self.server.server_close()

	def test_queue_lengths(self):
		self.assertEqual(
			queue_lengths(self.server.url),
			{"abc123:default": 0, "abc123:long": 812, "abc123:short": 3},
		)

	def test_queue_metrics(self):
		metrics = queue_metrics(self.server.url, "h1", "main")
		long_q = next(m for m in metrics if m["labels"]["queue"] == "abc123:long")
		self.assertEqual(long_q["metric_name"], "queue_length")
		self.assertEqual(long_q["value"], 812)
		self.assertEqual((long_q["host"], long_q["bench"]), ("h1", "main"))

	def test_auth_and_db(self):
		self.server.password = "s3cret"
		url = self.server.url.replace("redis://", "redis://:s3cret@") + "/2"
		self.assertEqual(len(queue_lengths(url)), 3)
		self.assertEqual(self.server.commands[0], ["AUTH", "s3cret"])
		self.assertEqual(self.server.commands[1], ["SELECT", "2"])

	def test_wrong_password_raises(self):
		self.server.password = "right"
		with self.assertRaises(RedisError):
			with RedisClient(self.server.url.replace("redis://", "redis://:wrong@")):
				pass

	def test_url_without_scheme(self):
		c = RedisClient("localhost:11000")
		self.assertEqual((c.host, c.port, c.db), ("localhost", 11000, 0))


def rq_time(dt):
	return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TestRedisProbe(unittest.TestCase):
	NOW = datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)

	def setUp(self):
		started_long = rq_time(self.NOW - timedelta(minutes=45))
		started_short = rq_time(self.NOW - timedelta(minutes=2))
		self.server = FakeRedis({
			"rq:queue:b1:long": 4,
			"rq:failed:b1:long": ("zset", ["j1", "j2"]),
			"rq:failed:b1:short": ("zset", ["j3"]),
			# RQ 1.x member, RQ 2.x "<job>:<execution>" member, a job whose hash expired
			"rq:wip:b1:long": ("zset", ["site::job-a", "job-b:exec-1", "gone"]),
			"rq:wip:b1:short": ("zset", ["job-c"]),
			"rq:job:site::job-a": {"started_at": started_long},
			"rq:job:job-b": {"started_at": started_long},
			"rq:job:job-c": {"started_at": started_short},
			"rq:workers": {"rq:worker:w1", "rq:worker:dead"},
			"rq:worker:w1": {"state": "busy"},
		})
		self.server.info = {
			"memory": "# Memory\r\nused_memory:250\r\nmaxmemory:1000\r\ntotal_system_memory:8000\r\n",
			"stats": "# Stats\r\nevicted_keys:10\r\nkeyspace_hits:3\r\n",
		}

	def tearDown(self):
		self.server.shutdown()
		self.server.server_close()

	def collect(self, probe):
		return {(m["metric_name"], m["labels"].get("queue")): m for m in probe.collect(self.NOW)}

	def test_registries_workers_and_memory(self):
		got = self.collect(RedisProbe(self.server.url, "h1", "b1", long_job_minutes=30))
		self.assertEqual(got[("queue_length", "b1:long")]["value"], 4)
		self.assertEqual(got[("rq_failed_jobs", "b1:long")]["value"], 2)
		self.assertEqual(got[("rq_failed_jobs", "b1:short")]["value"], 1)
		self.assertEqual(got[("rq_long_running_jobs", "b1:long")]["value"], 2)
		self.assertEqual(got[("rq_long_running_jobs", "b1:short")]["value"], 0)
		self.assertEqual(got[("rq_workers", None)]["value"], 1)  # the dead worker's key expired
		mem = got[("redis_used_memory_percent", None)]
		self.assertEqual((mem["value"], mem["labels"]), (25.0, {"of": "maxmemory"}))
		self.assertEqual((mem["host"], mem["bench"]), ("h1", "b1"))
		# counters need a baseline: nothing on the first cycle
		self.assertNotIn(("rq_failed_jobs_added", "b1:long"), got)
		self.assertNotIn(("redis_evicted_keys", None), got)

	def test_emptied_queue_reports_zero(self):
		probe = RedisProbe(self.server.url, "h1", "b1")
		self.assertEqual(self.collect(probe)[("queue_length", "b1:long")]["value"], 4)
		del self.server.keys["rq:queue:b1:long"]  # RQ drained it; Redis deleted the empty list
		got = self.collect(probe)
		self.assertEqual(got[("queue_length", "b1:long")]["value"], 0)
		self.assertIn(("rq_long_running_jobs", "b1:long"), got)
		# a queue never seen is not invented
		self.assertNotIn(("queue_length", "b1:default"), got)

	def test_long_job_threshold(self):
		got = self.collect(RedisProbe(self.server.url, "h1", "b1", long_job_minutes=60))
		self.assertEqual(got[("rq_long_running_jobs", "b1:long")]["value"], 0)

	def test_counter_increases(self):
		probe = RedisProbe(self.server.url, "h1", "b1")
		self.collect(probe)
		self.server.keys["rq:failed:b1:long"] = ("zset", ["j1", "j2", "j4", "j5", "j6"])
		self.server.keys["rq:failed:b1:short"] = ("zset", [])  # entries expired (failure_ttl)
		self.server.info["stats"] = "evicted_keys:17\r\n"
		got = self.collect(probe)
		self.assertEqual(got[("rq_failed_jobs_added", "b1:long")]["value"], 3)
		self.assertEqual(got[("rq_failed_jobs_added", "b1:short")]["value"], 0)
		self.assertEqual(got[("redis_evicted_keys", None)]["value"], 7)
		self.server.info["stats"] = "evicted_keys:2\r\n"  # Redis restarted
		self.assertEqual(self.collect(probe)[("redis_evicted_keys", None)]["value"], 2)

	def test_memory_falls_back_to_system_memory(self):
		self.server.info["memory"] = "used_memory:800\r\nmaxmemory:0\r\ntotal_system_memory:8000\r\n"
		mem = self.collect(RedisProbe(self.server.url, "h1", "b1"))[("redis_used_memory_percent", None)]
		self.assertEqual((mem["value"], mem["labels"]), (10.0, {"of": "system"}))

	def test_info_refused_keeps_queue_metrics(self):
		self.server.info = None  # INFO answers -ERR (e.g. an ACL without INFO)
		got = self.collect(RedisProbe(self.server.url, "h1", "b1"))
		self.assertIn(("queue_length", "b1:long"), got)
		self.assertIn(("rq_workers", None), got)
		self.assertNotIn(("redis_used_memory_percent", None), got)

	def test_parsers(self):
		self.assertEqual(
			parse_rq_time("2026-10-29T02:00:13.5Z"), datetime(2026, 10, 29, 2, 0, 13, 500000, tzinfo=timezone.utc)
		)
		self.assertEqual(parse_rq_time("2026-10-29T02:00:13Z").second, 13)
		self.assertIsNone(parse_rq_time("yesterday"))
		self.assertIsNone(parse_rq_time(None))
		self.assertEqual(parse_info("# S\r\na:1\r\nb:x\r\nc:1.5\r\n"), {"a": 1, "b": "x", "c": 1.5})


if __name__ == "__main__":
	unittest.main()
