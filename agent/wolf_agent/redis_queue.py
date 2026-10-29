"""Minimal RESP client, just enough to measure RQ queue lengths."""

import socket
from urllib.parse import unquote, urlparse

from .util import metric, utc_iso

QUEUE_PREFIX = "rq:queue:"


class RedisError(Exception):
	pass


class RedisClient:
	def __init__(self, url, timeout=3.0):
		u = urlparse(url if "://" in url else "redis://" + url)
		self.host = u.hostname or "127.0.0.1"
		self.port = u.port or 6379
		self.password = unquote(u.password) if u.password else None
		self.username = unquote(u.username) if u.username else None
		self.db = int(u.path.lstrip("/") or 0) if u.path.lstrip("/").isdigit() else 0
		self.timeout = timeout
		self.sock = None
		self.rfile = None

	def __enter__(self):
		self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
		self.rfile = self.sock.makefile("rb")
		if self.password:
			if self.username:
				self.command("AUTH", self.username, self.password)
			else:
				self.command("AUTH", self.password)
		if self.db:
			self.command("SELECT", self.db)
		return self

	def __exit__(self, *exc):
		if self.rfile:
			self.rfile.close()
		if self.sock:
			self.sock.close()

	def command(self, *args):
		out = [b"*%d\r\n" % len(args)]
		for arg in args:
			data = arg if isinstance(arg, bytes) else str(arg).encode()
			out.append(b"$%d\r\n%s\r\n" % (len(data), data))
		self.sock.sendall(b"".join(out))
		return self._read()

	def _read(self):
		line = self.rfile.readline()
		if not line:
			raise RedisError("connection closed")
		kind, rest = line[:1], line[1:-2]
		if kind == b"+":
			return rest.decode()
		if kind == b"-":
			raise RedisError(rest.decode())
		if kind == b":":
			return int(rest)
		if kind == b"$":
			n = int(rest)
			if n == -1:
				return None
			data = self.rfile.read(n + 2)
			return data[:-2].decode("utf-8", errors="replace")
		if kind == b"*":
			n = int(rest)
			if n == -1:
				return None
			return [self._read() for _ in range(n)]
		raise RedisError(f"unexpected reply {line!r}")

	def scan_iter(self, match, count=1000):
		cursor = "0"
		while True:
			cursor, keys = self.command("SCAN", cursor, "MATCH", match, "COUNT", count)
			yield from keys
			if cursor == "0":
				return


def queue_lengths(url):
	"""`{queue_name: length}` for every RQ queue list in this Redis."""
	lengths = {}
	with RedisClient(url) as r:
		for key in sorted(set(r.scan_iter(QUEUE_PREFIX + "*"))):
			try:
				lengths[key[len(QUEUE_PREFIX):]] = r.command("LLEN", key)
			except RedisError:
				continue  # not a list (e.g. another RQ structure)
	return lengths


def queue_metrics(url, host, bench, now=None):
	ts = utc_iso(now)
	return [
		metric("queue_length", length, ts, host, bench=bench, labels={"queue": name})
		for name, length in queue_lengths(url).items()
	]
