"""HTTP transport to the Wolf App, with an on-disk spool for failed batches."""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request

from . import __version__
from .util import utc_iso

log = logging.getLogger(__name__)

PUSH_METHOD = "/api/method/frappe_wolf.api.ingest.push"
DEPLOYMENT_METHOD = "/api/method/frappe_wolf.api.ingest.deployment"
STATUS_METHOD = "/api/method/frappe_wolf.api.ingest.status"
REGISTER_METHOD = "/api/method/frappe_wolf.api.agent.register"
MAX_METRICS = 5000
MAX_EVENTS = 1000
SPOOL_CAP_BYTES = 50 * 1024 * 1024
MAX_BACKOFF = 300


class AuthError(Exception):
	"""The Wolf App rejected the agent credentials (HTTP 401/403)."""


class TransportError(Exception):
	"""Network error or server-side failure: worth retrying later."""


class RejectedError(Exception):
	"""The server refused the batch itself (4xx): retrying won't help."""


def post_json(url, body, headers=None, verify_tls=True, timeout=15):
	data = json.dumps(body).encode()
	req = urllib.request.Request(url, data=data, method="POST")
	req.add_header("Content-Type", "application/json")
	req.add_header("Accept", "application/json")
	req.add_header("User-Agent", f"wolf-agent/{__version__}")
	for key, value in (headers or {}).items():
		req.add_header(key, value)
	context = None
	if url.startswith("https") and not verify_tls:
		context = ssl._create_unverified_context()
	try:
		with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
			raw = resp.read()
	except urllib.error.HTTPError as e:
		detail = e.read()[:500].decode("utf-8", errors="replace")
		if e.code in (401, 403):
			raise AuthError(f"HTTP {e.code}: {detail}") from e
		if 400 <= e.code < 500 and e.code not in (408, 429):
			raise RejectedError(f"HTTP {e.code}: {detail}") from e
		raise TransportError(f"HTTP {e.code}: {detail}") from e
	except (urllib.error.URLError, OSError) as e:
		raise TransportError(str(e)) from e
	try:
		return json.loads(raw or b"{}").get("message") or {}
	except ValueError as e:
		raise TransportError(f"invalid JSON response: {raw[:200]!r}") from e


def split_batches(metrics, events, max_metrics=MAX_METRICS, max_events=MAX_EVENTS):
	"""Yield `(metrics, events)` chunks respecting the per-request limits."""
	m_chunks = [metrics[i : i + max_metrics] for i in range(0, len(metrics), max_metrics)] or [[]]
	e_chunks = [events[i : i + max_events] for i in range(0, len(events), max_events)] or [[]]
	for i in range(max(len(m_chunks), len(e_chunks))):
		m = m_chunks[i] if i < len(m_chunks) else []
		e = e_chunks[i] if i < len(e_chunks) else []
		if m or e:
			yield m, e


class Spool:
	"""Directory of pending request bodies, oldest first, capped in size."""

	def __init__(self, directory, cap_bytes=SPOOL_CAP_BYTES):
		self.dir = directory
		self.cap = cap_bytes
		self.seq = 0

	def _files(self):
		try:
			names = sorted(n for n in os.listdir(self.dir) if n.endswith(".json"))
		except FileNotFoundError:
			return []
		return [os.path.join(self.dir, n) for n in names]

	def put(self, body):
		os.makedirs(self.dir, exist_ok=True)
		self.seq += 1
		path = os.path.join(self.dir, f"{time.time_ns():020d}-{self.seq:06d}.json")
		with open(path, "w") as f:
			json.dump(body, f)
		self._enforce_cap()

	def _enforce_cap(self):
		files = self._files()
		sizes = {p: os.path.getsize(p) for p in files}
		total = sum(sizes.values())
		for path in files:
			if total <= self.cap:
				break
			log.warning("spool over %d bytes, dropping %s", self.cap, os.path.basename(path))
			total -= sizes[path]
			os.remove(path)

	def items(self):
		for path in self._files():
			try:
				with open(path) as f:
					yield path, json.load(f)
			except (OSError, ValueError):
				log.warning("dropping unreadable spool file %s", path)
				try:
					os.remove(path)
				except OSError:
					pass

	def remove(self, path):
		try:
			os.remove(path)
		except FileNotFoundError:
			pass

	def __len__(self):
		return len(self._files())


class Shipper:
	def __init__(self, cfg, spool_dir=None, clock=time.monotonic):
		self.url = cfg["url"].rstrip("/")
		self.headers = {"X-Wolf-Agent-Id": cfg["agent_id"], "X-Wolf-Agent-Secret": cfg["agent_secret"]}
		self.hostname = cfg["hostname"]
		self.verify_tls = cfg.get("verify_tls", True)
		self.push_interval = int(cfg.get("push_interval") or 30)
		self.spool = Spool(os.path.join(spool_dir or cfg["spool_dir"], "outbox"))
		self.clock = clock
		self.failures = 0
		self.retry_at = 0.0
		self.health = None  # Wolf's own health from the last successful push (protocol §4)

	def _body(self, metrics, events):
		return {
			"sent_at": utc_iso(),
			"agent_version": __version__,
			"hostname": self.hostname,
			"metrics": metrics,
			"events": events,
		}

	def _post(self, body):
		resp = post_json(self.url + PUSH_METHOD, body, self.headers, self.verify_tls)
		interval = resp.get("push_interval")
		if isinstance(interval, (int, float)) and interval >= 5:
			self.push_interval = int(interval)
		if isinstance(resp, dict) and isinstance(resp.get("health"), dict):
			self.health = resp["health"]
		return resp

	def _spool(self, bodies):
		for body in bodies:
			if body["metrics"] or body["events"]:  # heartbeats are not worth replaying
				self.spool.put(body)

	def _failed(self, error):
		self.failures += 1
		delay = min(MAX_BACKOFF, 5 * 2 ** (self.failures - 1))
		self.retry_at = self.clock() + delay
		log.warning("push failed (%s); spooling, retry in %ss", error, delay)

	def send(self, metrics, events):
		"""Ship a cycle's telemetry. Raises AuthError on rejected credentials."""
		self.health = None
		# an empty body still goes out: it is the agent's heartbeat (last seen)
		bodies = [self._body(m, e) for m, e in split_batches(metrics, events)] or [self._body([], [])]
		if self.clock() < self.retry_at:
			self._spool(bodies)
			return False

		# drain older spooled batches first so the server sees data in order
		for path, body in self.spool.items():
			try:
				self._post(body)
			except TransportError as e:
				self._failed(e)
				self._spool(bodies)
				return False
			except RejectedError as e:
				log.error("server rejected spooled batch %s, dropping it: %s", os.path.basename(path), e)
			self.spool.remove(path)

		for i, body in enumerate(bodies):
			try:
				self._post(body)
			except TransportError as e:
				self._failed(e)
				self._spool(bodies[i:])
				return False
			except RejectedError as e:
				log.error("server rejected batch, dropping it: %s", e)
		self.failures = 0
		self.retry_at = 0.0
		return True

	def mark_deployment(self, bench, site=None, note=None, source="manual"):
		body = {"bench": bench, "site": site, "note": note, "source": source, "timestamp": utc_iso()}
		return post_json(self.url + DEPLOYMENT_METHOD, body, self.headers, self.verify_tls)


	def status(self, timeout=10):
		return post_json(self.url + STATUS_METHOD, {}, self.headers, self.verify_tls, timeout=timeout)


def register(url, token, hostname, benches, os_name=None, verify_tls=True):
	body = {
		"registration_token": token,
		"hostname": hostname,
		"agent_version": __version__,
		"os": os_name,
		"benches": [{"name": b["name"], "path": b["path"], "sites": b["sites"]} for b in benches],
	}
	return post_json(url.rstrip("/") + REGISTER_METHOD, body, verify_tls=verify_tls)
