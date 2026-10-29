import os
import socketserver
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent.redis_queue import RedisClient, RedisError, queue_lengths, queue_metrics  # noqa: E402


class FakeRedisHandler(socketserver.StreamRequestHandler):
	"""Speaks enough RESP for AUTH/SELECT/SCAN/LLEN over a dict of keys."""

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
		return b"$%d\r\n%s\r\n" % (len(s), s.encode())

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
				keys = sorted(k for k in server.keys if k.startswith("rq:queue:"))
				half = len(keys) // 2
				cursor, page = ("7", keys[:half]) if cmd[1] == "0" else ("0", keys[half:])
				out = b"*2\r\n" + self.bulk(cursor) + b"*%d\r\n" % len(page) + b"".join(self.bulk(k) for k in page)
				self.wfile.write(out)
			elif name == "LLEN":
				value = server.keys.get(cmd[1])
				if isinstance(value, int):
					self.wfile.write(b":%d\r\n" % value)
				else:
					self.wfile.write(b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n")
			else:
				self.wfile.write(b"-ERR unknown command\r\n")


class FakeRedis(socketserver.ThreadingTCPServer):
	allow_reuse_address = True
	daemon_threads = True

	def __init__(self, keys, password=None):
		super().__init__(("127.0.0.1", 0), FakeRedisHandler)
		self.keys = keys
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


if __name__ == "__main__":
	unittest.main()
