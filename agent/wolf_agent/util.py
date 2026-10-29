"""Shared helpers: timestamps, fingerprints and telemetry record builders."""

import hashlib
import re
from datetime import datetime, timezone

_ISO_RE = re.compile(
	r"^\[?(?P<dt>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.](?P<frac>\d+))?"
	r"\s*(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)
_SYSLOG_RE = re.compile(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}) (?P<time>\d{2}:\d{2}:\d{2})\b")


def utc_iso(dt=None):
	"""Format a datetime (aware, or naive local time) as `YYYY-MM-DDTHH:MM:SSZ`."""
	if dt is None:
		dt = datetime.now(timezone.utc)
	return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_log_timestamp(line, now=None):
	"""Best-effort timestamp from the start of a log line, as a UTC ISO string.

	Understands `2026-10-29 02:00:13,123`, ISO-8601 with offsets and classic
	syslog `Oct 29 02:00:13` (year inferred). Naive times are taken as local.
	"""
	m = _ISO_RE.match(line)
	if m:
		raw = m.group("dt").replace("T", " ")
		tz = m.group("tz")
		try:
			dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
		except ValueError:
			return None
		if tz:
			if tz == "Z":
				dt = dt.replace(tzinfo=timezone.utc)
			else:
				dt = datetime.strptime(raw + " " + tz.replace(":", ""), "%Y-%m-%d %H:%M:%S %z")
		return utc_iso(dt)

	m = _SYSLOG_RE.match(line)
	if m:
		now = now or datetime.now()
		try:
			dt = datetime.strptime(
				f"{now.year} {m.group('mon')} {m.group('day')} {m.group('time')}", "%Y %b %d %H:%M:%S"
			)
		except ValueError:
			return None
		# Syslog has no year: a date in the future means it was logged last year.
		if (dt - now).days >= 1:
			dt = dt.replace(year=dt.year - 1)
		return utc_iso(dt)
	return None


def fingerprint(*parts):
	return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def metric(name, value, timestamp, host, bench=None, site=None, labels=None):
	return {
		"metric_name": name,
		"value": value,
		"timestamp": timestamp,
		"host": host,
		"bench": bench,
		"site": site,
		"labels": labels or {},
	}


def event(
	type,
	category,
	severity,
	timestamp,
	host,
	title,
	fingerprint,
	payload,
	source,
	bench=None,
	site=None,
):
	return {
		"type": type,
		"category": category,
		"severity": severity,
		"timestamp": timestamp,
		"host": host,
		"bench": bench,
		"site": site,
		"source": source,
		"title": title,
		"fingerprint": fingerprint,
		"payload": payload,
	}


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_DURATION_UNIT = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_minutes(value):
	"""`15m`, `1h`, `90s`, `2d` or a bare number of minutes -> whole minutes (rounded up)."""
	m = _DURATION_RE.match(str(value or ""))
	if not m:
		raise ValueError(f"invalid duration {value!r} (use e.g. 15m, 1h, 90s)")
	seconds = int(m.group(1)) * _DURATION_UNIT[m.group(2).lower()]
	if seconds <= 0:
		raise ValueError("duration must be positive")
	return -(-seconds // 60)
