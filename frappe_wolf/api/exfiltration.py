"""Unusual data exports (MITRE T1567 / T1030), per user against their own history.

Each export or list read adds its response size to the user's counter for the hour (Redis,
per site). Past a floor, the hour is compared with the user's largest hour over the last 14 days
(`user_export_bytes`, one series per user, written every hour by `flush_volumes`), and raises a
`data_exfiltration` event: warning at 5x by default, critical at 20x or without any history.
Rules: wolf/exfiltration.py. Settings: Wolf Settings → Flag unusual data exports.
"""

import json
import time
from datetime import timedelta

import frappe
import redis
from frappe.utils import cint, flt, now_datetime

from frappe_wolf.wolf import events, rollups, schema
from frappe_wolf.wolf import exfiltration as exf
from frappe_wolf.wolf.schema import fingerprint

METRIC = "user_export_bytes"
_COUNTER = "wolf_exfil|"  # + hour number: hash of user|bytes, user|requests, user|rows
_KEEP_SECONDS = 3 * 3600  # an hour not flushed (scheduler down) is dropped after this


def _hour(ts=None):
	return int((ts or time.time()) // 3600)


def _settings():
	return frappe.get_cached_doc("Wolf Settings")


def _response_bytes(response):
	length = getattr(response, "content_length", None)
	if length is not None:
		return int(length)
	if getattr(response, "direct_passthrough", False) or getattr(response, "is_streamed", False):
		return 0  # a file sent as is, or a stream: not read here
	try:
		return len(response.get_data())
	except Exception:
		return 0


def after_request(response=None, request=None):
	"""hooks.py after_request: count what the request took out, alert past the user's baseline."""
	try:
		if getattr(response, "status_code", None) != 200:
			return
		taken = exf.classify(getattr(request, "path", ""), getattr(request, "method", "GET"), frappe.form_dict)
		if not taken:
			return
		settings = _settings()
		if not cint(settings.get("capture_exfiltration")):
			return
		user = frappe.session.user
		size = _response_bytes(response)
		# methods answer in `message`, /api/resource in `data`
		answer = frappe.local.response
		rows = exf.rows_in(answer.get("message") if answer.get("message") is not None else answer.get("data")) if taken["kind"] == "list" else None
		hour_bytes, requests, hour_rows = _count(user, size, rows)
		floor = cint(settings.get("exfiltration_floor_mb") or exf.DEFAULT_FLOOR_MB) * 1_000_000
		if hour_bytes < floor:
			return
		factor = flt(settings.get("exfiltration_factor")) or exf.DEFAULT_FACTOR
		baseline = baseline_bytes(user)
		severity = exf.verdict(hour_bytes, baseline, floor, factor, max(exf.CRITICAL_FACTOR, factor), learning=is_learning())
		if severity and _first_this_hour(user, severity):
			_emit(user, severity, hour_bytes, baseline, factor, floor, requests, hour_rows, taken, size)
			frappe.db.commit()  # after_request runs after the request's own commit
	except Exception:
		frappe.logger("frappe_wolf").exception("could not count a data export")


def _count(user, size, rows):
	key = frappe.cache.make_key(f"{_COUNTER}{_hour()}")
	pipe = frappe.cache.pipeline()
	pipe.hincrby(key, f"{user}|bytes", size)
	pipe.hincrby(key, f"{user}|requests", 1)
	pipe.hincrby(key, f"{user}|rows", rows or 0)
	pipe.expire(key, _KEEP_SECONDS)
	hour_bytes, requests, hour_rows, _ = pipe.execute()
	return hour_bytes, requests, hour_rows


def _first_this_hour(user, severity):
	"""One event per user, hour and severity: a warning can still be followed by a critical."""
	key = frappe.cache.make_key(f"wolf_exfil_alerted|{_hour()}|{user}|{severity}")
	return bool(frappe.cache.set(key, 1, nx=True, ex=7200))


def baseline_bytes(user, now=None):
	"""The user's largest hour over the last 14 days, None without any (cached for 10 min)."""
	cache_key = f"wolf_exfil_baseline|{user}"
	cached = frappe.cache.get_value(cache_key, expires=True, use_local_cache=False)
	if cached is not None:
		return cached.get("value")
	label = schema.label_key({"user": user})
	rows = _aggregate(["label_key = %(label)s"], {"label": label}, now)
	value = rows[0].value if rows else None
	frappe.cache.set_value(cache_key, {"value": value}, expires_in_sec=600)
	return value


def is_learning(now=None):
	"""True while this site has less than a full baseline window of export history: every user
	then lacks one, and a first large export is a warning, not critical (cached for 1 h)."""
	cached = frappe.cache.get_value("wolf_exfil_learning", expires=True, use_local_cache=False)
	if cached is not None:
		return cached
	now = now or now_datetime()
	rows = _aggregate([], {}, now)
	first = min((r.first_at for r in rows if r.first_at), default=None)
	learning = first is None or first > now - timedelta(days=exf.BASELINE_DAYS - 1)
	frappe.cache.set_value("wolf_exfil_learning", learning, expires_in_sec=3600)
	return learning


def _aggregate(where, params, now=None):
	now = now or now_datetime()
	since = now - timedelta(days=exf.BASELINE_DAYS)
	resolution = rollups.resolution_for((now - since).total_seconds())
	conds = ["metric_name = %(metric)s", "site = %(site)s", *where]
	return rollups.aggregate(conds, {"metric": METRIC, "site": frappe.local.site, **params}, [], [], "max",
	                         resolution, since, now, [METRIC])  # fmt: skip


def _emit(user, severity, hour_bytes, baseline, factor, floor, requests, rows, taken, size):
	ratio = round(hour_bytes / baseline, 1) if baseline else None
	compared = f"{ratio}x their largest hour in {exf.BASELINE_DAYS} days" if ratio else "no export history"
	what = taken["label"] + (f" of {taken['doctype']}" if taken.get("doctype") else "")
	events.emit(
		"security",
		f"Unusual data export by {user}: {exf.megabytes(hour_bytes)} MB this hour, {compared}"[:255],
		severity=severity,
		category="data_exfiltration",
		source="frappe",
		payload={
			"user": user, "hour_bytes": hour_bytes, "hour_requests": requests, "hour_rows": rows or None,
			"baseline_bytes": baseline, "ratio": ratio, "factor": factor, "floor_bytes": floor,
			"last_request": {**taken, "bytes": size, "summary": what}, "ip": getattr(frappe.local, "request_ip", None),
		},
		fingerprint=fingerprint("data_exfiltration", user, _hour(), severity),
	)  # fmt: skip


def flush_volumes():
	"""Scheduler (hourly): each user's volume of the hours past, as `user_export_bytes` samples."""
	current = _hour()
	scope = events.local_scope()
	now = now_datetime()
	rows = []
	for hour in range(current - 3, current):
		key = frappe.cache.make_key(f"{_COUNTER}{hour}")
		# the counters are plain integers: read them with redis itself, not Frappe's pickled values
		raw = redis.Redis.hgetall(frappe.cache, key)
		if not raw:
			continue
		redis.Redis.delete(frappe.cache, key)
		for field, value in raw.items():
			user, _, what = field.decode().rpartition("|")
			if what != "bytes" or not int(value):
				continue
			labels = {"user": user}
			rows.append({
				"metric_name": METRIC, "value": int(value), "timestamp": now, **scope,
				"label_key": schema.label_key(labels), "labels": json.dumps(labels, sort_keys=True),
			})  # fmt: skip
	if rows:
		events.insert_metrics(rows)
		frappe.cache.delete_keys("wolf_exfil_baseline|")
	return len(rows)
