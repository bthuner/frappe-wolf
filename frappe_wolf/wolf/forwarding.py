"""Satellite forwarding helpers: payload shaping, batching and retry schedule.

Kept free of frappe imports so it can be unit tested without a bench.
"""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

PUSH_METHOD = "/api/method/frappe_wolf.api.ingest.push"
REGISTER_METHOD = "/api/method/frappe_wolf.api.agent.register"

# Events carry up to 32 KB of payload each, so keep requests well under nginx limits.
BATCH_SIZE = 100
MAX_BATCHES_PER_RUN = 10
MAX_OUTBOX = 10_000
BACKOFF_BASE = 60  # the forwarder runs every minute
MAX_BACKOFF = 900

PROTOCOL_FIELDS = ("category", "severity", "source", "title", "fingerprint", "host", "bench", "site")


def to_utc_iso(dt, tz_name):
	"""Format a naive datetime in `tz_name` (or an aware one) as `YYYY-MM-DDTHH:MM:SSZ`."""
	if isinstance(dt, str):
		dt = datetime.fromisoformat(dt)
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=ZoneInfo(tz_name))
	return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso():
	return to_utc_iso(datetime.now(timezone.utc), "UTC")


def protocol_event(data, tz_name):
	"""Turn a normalized event (schema.normalize_event) back into a protocol event (docs/protocol.md §2).

	The count is left out: the outbox keeps it in its own column so duplicates can bump it.
	"""
	payload = data.get("payload")
	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except ValueError:
			payload = {"value": payload}
	event = {"type": data["event_type"], "timestamp": to_utc_iso(data["timestamp"], tz_name)}
	event.update({k: data.get(k) for k in PROTOCOL_FIELDS})
	event["payload"] = payload
	return event


def with_count(event, count):
	return {**event, "count": max(int(count or 1), 1)}


def push_body(events, hostname, version, sent_at=None):
	return {
		"sent_at": sent_at or utc_now_iso(),
		"agent_version": version,
		"hostname": hostname,
		"metrics": [],
		"events": events,
	}


def register_body(token, scope, version, os_name=None, bench_path=None):
	"""A satellite registers like an agent, named after its site."""
	return {
		"registration_token": token,
		"kind": "satellite",
		"hostname": scope["site"],
		"host": scope.get("host"),
		"agent_version": version,
		"os": os_name,
		"benches": [{"name": scope.get("bench"), "path": bench_path, "sites": [scope["site"]], "host": scope.get("host")}],
	}


def chunks(rows, size=BATCH_SIZE, limit=MAX_BATCHES_PER_RUN):
	"""Split rows into at most `limit` batches of `size`."""
	out = [rows[i : i + size] for i in range(0, len(rows), size)]
	return out[:limit]


def backoff_seconds(failures, base=BACKOFF_BASE, cap=MAX_BACKOFF):
	"""Delay before the next attempt after `failures` consecutive failures: 1, 2, 4, 8 min, then 15 min."""
	if failures <= 0:
		return 0
	return min(cap, base * 2 ** (failures - 1))


def next_attempt(now, failures, outcome="retry"):
	# Rejected credentials will not fix themselves: wait the longest delay.
	delay = MAX_BACKOFF if outcome == "auth" else backoff_seconds(failures)
	return now + timedelta(seconds=delay)


def classify_status(code):
	"""`ok`, `auth` (credentials refused), `rejected` (retrying won't help) or `retry`."""
	if 200 <= code < 300:
		return "ok"
	if code in (401, 403):
		return "auth"
	if 400 <= code < 500 and code not in (408, 429):
		return "rejected"
	return "retry"


def error_detail(text, limit=500):
	"""Best human-readable message from a Frappe error response body."""
	text = text or ""
	try:
		data = json.loads(text)
	except ValueError:
		return text.strip()[:limit]
	if not isinstance(data, dict):
		return text.strip()[:limit]
	messages = []
	try:
		for raw in json.loads(data.get("_server_messages") or "[]"):
			msg = json.loads(raw) if isinstance(raw, str) else raw
			messages.append(str(msg.get("message") if isinstance(msg, dict) else msg))
	except (ValueError, TypeError):
		pass
	if messages:
		return "; ".join(messages)[:limit]
	if data.get("exception"):
		return str(data["exception"])[:limit]
	if data.get("exc_type"):
		return str(data["exc_type"])[:limit]
	return text.strip()[:limit]


def normalize_central_url(url):
	"""`https://wolf.example.com/` -> `https://wolf.example.com`; raises ValueError if unusable."""
	url = (url or "").strip().rstrip("/")
	parsed = urlparse(url)
	if parsed.scheme not in ("http", "https") or not parsed.netloc:
		raise ValueError(f"central URL must start with http:// or https://, got {url!r}")
	return url


def overflow(pending, cap=MAX_OUTBOX):
	"""How many of the oldest outbox rows to drop to stay under the cap."""
	return max(0, pending - cap)
