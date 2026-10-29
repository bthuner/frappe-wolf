"""Validation and normalization of agent payloads (docs/protocol.md).

Kept free of frappe imports so it can be unit tested without a bench.
"""

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EVENT_TYPES = ("error", "cron_failure", "worker_failure", "security", "host_metric_alert", "deployment")
SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

MAX_PAYLOAD_BYTES = 32_000


class PayloadError(ValueError):
	pass


def parse_timestamp(value, tz_name, default=None):
	"""Parse an ISO-8601 timestamp and return a naive datetime in `tz_name`.

	Naive input is assumed to be UTC, as the protocol mandates UTC.
	"""
	if not value:
		if default is None:
			raise PayloadError("missing timestamp")
		return default
	if isinstance(value, datetime):
		dt = value
	else:
		try:
			dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
		except ValueError as e:
			raise PayloadError(f"invalid timestamp {value!r}") from e
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=timezone.utc)
	return dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def clamp_future(dt, now, max_future_seconds):
	"""Timestamps from the future (fast agent clock, time-zone change) become `now`.

	Returns (timestamp, clamped). They would otherwise sit inside every rule window.
	"""
	if max_future_seconds and dt and (dt - now).total_seconds() > max_future_seconds:
		return now, True
	return dt, False


def clock_skew_seconds(sent_at, tz_name, now):
	"""Agent clock minus server clock, from the push's `sent_at`; None if missing or invalid."""
	if not sent_at:
		return None
	try:
		return int((parse_timestamp(sent_at, tz_name) - now).total_seconds())
	except PayloadError:
		return None


def _text(value, length=140):
	if value is None:
		return None
	value = str(value).strip()
	return value[:length] or None


def label_key(labels):
	if not labels:
		return None
	return ",".join(f"{k}={labels[k]}" for k in sorted(labels))[:140]


def dump_payload(payload):
	if payload is None:
		return None
	text = json.dumps(payload, default=str, sort_keys=True)
	if len(text) > MAX_PAYLOAD_BYTES:
		text = json.dumps({"truncated": True, "preview": text[:MAX_PAYLOAD_BYTES]})
	return text


def normalize_metric(raw, tz_name, now, default_host=None):
	if not isinstance(raw, dict):
		raise PayloadError("metric must be an object")
	name = _text(raw.get("metric_name"))
	if not name:
		raise PayloadError("metric_name is required")
	try:
		value = float(raw.get("value"))
	except (TypeError, ValueError) as e:
		raise PayloadError(f"invalid value for {name}") from e
	if value != value or value in (float("inf"), float("-inf")):
		raise PayloadError(f"non-finite value for {name}")
	labels = raw.get("labels") or {}
	if not isinstance(labels, dict):
		raise PayloadError("labels must be an object")
	return {
		"metric_name": name,
		"value": value,
		"timestamp": parse_timestamp(raw.get("timestamp"), tz_name, default=now),
		"host": _text(raw.get("host")) or default_host,
		"bench": _text(raw.get("bench")),
		"site": _text(raw.get("site")),
		"label_key": label_key(labels),
		"labels": json.dumps(labels, sort_keys=True) if labels else None,
	}


def fingerprint(*parts):
	return hashlib.sha1("|".join(str(p or "") for p in parts).encode()).hexdigest()


def normalize_event(raw, tz_name, now, default_host=None):
	if not isinstance(raw, dict):
		raise PayloadError("event must be an object")
	event_type = raw.get("type") or raw.get("event_type")
	if event_type not in EVENT_TYPES:
		raise PayloadError(f"unknown event type {event_type!r}")
	severity = raw.get("severity") or "warning"
	if severity not in SEVERITIES:
		raise PayloadError(f"unknown severity {severity!r}")
	timestamp = parse_timestamp(raw.get("timestamp"), tz_name, default=now)
	payload = raw.get("payload")
	if payload is not None and not isinstance(payload, dict):
		payload = {"value": payload}
	try:
		count = max(int(raw.get("count") or (payload or {}).get("count") or 1), 1)
	except (TypeError, ValueError):
		count = 1
	host = _text(raw.get("host")) or default_host
	category = _text(raw.get("category"))
	title = _text(raw.get("title"), 255) or f"{event_type} on {raw.get('site') or host or 'unknown'}"
	return {
		"event_type": event_type,
		"category": category,
		"severity": severity,
		"source": _text(raw.get("source")),
		"title": title,
		"timestamp": timestamp,
		"last_seen": timestamp,
		"count": count,
		"fingerprint": _text(raw.get("fingerprint"))
		or fingerprint(event_type, category, host, raw.get("site"), title),
		"host": host,
		"bench": _text(raw.get("bench")),
		"site": _text(raw.get("site")),
		"payload": dump_payload(payload),
	}
