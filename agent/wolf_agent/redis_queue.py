"""Minimal RESP client, just enough to read RQ queues, registries, workers and Redis INFO."""

import logging
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

from .util import metric, utc_iso

log = logging.getLogger(__name__)

QUEUE_PREFIX = "rq:queue:"
FAILED_PREFIX = "rq:failed:"  # FailedJobRegistry (sorted set of job ids)
STARTED_PREFIX = "rq:wip:"  # StartedJobRegistry (sorted set; RQ 2 stores "<job_id>:<execution_id>")
JOB_PREFIX = "rq:job:"
WORKERS_KEY = "rq:workers"
DEFAULT_LONG_JOB_MINUTES = 30
FORGET_QUEUE_AFTER = timedelta(hours=24)


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
		try:
			if self.password:
				if self.username:
					self.command("AUTH", self.username, self.password)
				else:
					self.command("AUTH", self.password)
			if self.db:
				self.command("SELECT", self.db)
		except BaseException:
			self.__exit__()
			raise
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
	with RedisClient(url) as r:
		return _queue_lengths(r)


def _queue_lengths(r):
	lengths = {}
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


def parse_rq_time(value):
	"""RQ's `started_at` (`2026-10-29T02:00:13.123456Z`, older: no fraction) as aware UTC."""
	if not value:
		return None
	value = value.strip().rstrip("Z")
	for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
		try:
			return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
		except ValueError:
			continue
	return None


def parse_info(text):
	"""`INFO` reply → {field: value}, numbers converted."""
	info = {}
	for line in (text or "").splitlines():
		if not line or line.startswith("#") or ":" not in line:
			continue
		key, _, value = line.partition(":")
		try:
			info[key] = int(value)
		except ValueError:
			try:
				info[key] = float(value)
			except ValueError:
				info[key] = value
	return info


def _suffixes(r, prefix):
	return {key[len(prefix):] for key in r.scan_iter(prefix + "*")}


def _started_at(r, member):
	started = r.command("HGET", JOB_PREFIX + member, "started_at")
	if started is None and ":" in member:
		# RQ 2: "<job_id>:<execution_id>"
		started = r.command("HGET", JOB_PREFIX + member.rsplit(":", 1)[0], "started_at")
	return parse_rq_time(started)


class RedisProbe:
	"""Queue Redis of one bench: RQ queues, registries and workers, plus Redis memory/evictions.

	Counters that only grow (failed registry, evicted keys) are reported as the increase since
	the previous cycle, so a rule can say "more than N in 10 minutes". The first cycle has no
	baseline and reports no increase.
	"""

	def __init__(self, url, host, bench, long_job_minutes=DEFAULT_LONG_JOB_MINUTES):
		self.url = url
		self.host = host
		self.bench = bench
		self.long_job_seconds = max(float(long_job_minutes or DEFAULT_LONG_JOB_MINUTES), 1) * 60
		self.prev_failed = None
		self.prev_evicted = None
		# Redis deletes a list once it is empty: a queue seen before and gone now has 0 jobs,
		# which is reported, so the site sees "0" instead of a series that stopped. A queue
		# gone for FORGET_QUEUE_AFTER (removed from the bench) is no longer reported.
		self.known_queues = {}  # name -> last time its list existed

	def collect(self, now=None):
		now = now or datetime.now(timezone.utc)
		ts = utc_iso(now)
		out = []

		def add(name, value, labels=None):
			out.append(metric(name, value, ts, self.host, bench=self.bench, labels=labels))

		with RedisClient(self.url) as r:
			lengths = _queue_lengths(r)
			for name in lengths:
				self.known_queues[name] = now
			for name, seen in list(self.known_queues.items()):
				if now - seen > FORGET_QUEUE_AFTER:
					del self.known_queues[name]
				elif name not in lengths:
					lengths[name] = 0
			for name, length in sorted(lengths.items()):
				add("queue_length", length, {"queue": name})
			try:
				self._rq(r, now, set(lengths), add)
			except RedisError as e:
				# e.g. a restricted ACL user: keep the queue lengths
				_log_once(self, "rq", "RQ registries for bench %s: %s", self.bench, e)
			try:
				self._info(r, add)
			except RedisError as e:
				_log_once(self, "info", "Redis INFO for bench %s: %s", self.bench, e)
		return out

	def _rq(self, r, now, queues, add):
		failed_queues = _suffixes(r, FAILED_PREFIX)
		started_queues = _suffixes(r, STARTED_PREFIX)
		queues = queues | failed_queues | started_queues

		failed = {}
		for name in sorted(failed_queues):
			try:
				failed[name] = r.command("ZCARD", FAILED_PREFIX + name)
			except RedisError:
				continue
		for name, size in failed.items():
			add("rq_failed_jobs", size, {"queue": name})
		if self.prev_failed is not None:
			for name in sorted(set(failed) | set(self.prev_failed)):
				# registry entries also expire (failure_ttl): never report a negative increase
				add("rq_failed_jobs_added", max(failed.get(name, 0) - self.prev_failed.get(name, 0), 0), {"queue": name})
		self.prev_failed = failed

		for name in sorted(queues):
			long_running = 0
			if name in started_queues:
				try:
					members = r.command("ZRANGE", STARTED_PREFIX + name, 0, -1) or []
				except RedisError:
					members = []
				for member in members:
					started = _started_at(r, member)
					if started and (now - started).total_seconds() >= self.long_job_seconds:
						long_running += 1
			add("rq_long_running_jobs", long_running, {"queue": name})

		workers = r.command("SMEMBERS", WORKERS_KEY) or []
		# dead workers stay in the set until RQ cleans it up; their key has expired
		alive = sum(1 for key in workers if r.command("EXISTS", key))
		add("rq_workers", alive)

	def _info(self, r, add):
		info = parse_info(r.command("INFO", "memory"))
		info.update(parse_info(r.command("INFO", "stats")))
		used = info.get("used_memory")
		limit, of = info.get("maxmemory") or 0, "maxmemory"
		if not limit:
			limit, of = info.get("total_system_memory") or 0, "system"
		if isinstance(used, int) and isinstance(limit, int) and limit > 0:
			add("redis_used_memory_percent", round(100.0 * used / limit, 2), {"of": of})
		evicted = info.get("evicted_keys")
		if isinstance(evicted, int):
			if self.prev_evicted is not None:
				# a Redis restart resets the counter
				add("redis_evicted_keys", evicted - self.prev_evicted if evicted >= self.prev_evicted else evicted)
			self.prev_evicted = evicted


def _log_once(obj, key, msg, *args):
	seen = obj.__dict__.setdefault("_logged", set())
	if key not in seen:
		seen.add(key)
		log.warning(msg, *args)
