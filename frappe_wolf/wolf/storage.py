"""Pure helpers for metric rollups and read-resolution selection (no frappe imports, unit tested).

Three resolutions are stored: raw samples, 1-minute and 1-hour rollups. A rollup row holds
count, sum, min, max and the last value (with its timestamp) of one series in one bucket, so
avg (sum / count), min, max, sum, count and last over any run of whole buckets are exact.

A read covers [since, until] with segments: the coarsest rollup allowed for the window up to
its watermark (end of the last rolled bucket), then finer rollups, then raw samples for the
tail that is not rolled yet. See plan_segments().
"""

import hashlib
from datetime import datetime, timedelta, timezone

RAW, MINUTE, HOUR = 0, 60, 3600
ROLLUPS = (MINUTE, HOUR)
LABELS = {RAW: "raw", MINUTE: "1m", HOUR: "1h"}

# Read raw samples for windows up to 3 h, 1-minute rollups up to 2 days, 1-hour beyond.
RAW_MAX_WINDOW = 3 * 3600
MINUTE_MAX_WINDOW = 2 * 86400

DEFAULT_RAW_HOURS = 48
DEFAULT_MINUTE_DAYS = 30
DEFAULT_HOUR_DAYS = 365

SERIES_KEY = ("metric_name", "host", "bench", "site", "label_key")

# Rollup values are decimal(21,9) columns, like raw values (schema.MAX_METRIC_VALUE bounds those).
MAX_STORED = 999_999_999_999


def floor(ts, seconds):
	"""Floor a naive datetime to a multiple of `seconds` (wall-clock arithmetic, no tz shift)."""
	epoch = ts.replace(tzinfo=timezone.utc).timestamp()
	whole = int(epoch // seconds * seconds)
	return datetime.fromtimestamp(whole, tz=timezone.utc).replace(tzinfo=None)


def ceil(ts, seconds):
	start = floor(ts, seconds)
	return start if start == ts else start + timedelta(seconds=seconds)


def pick_resolution(window_seconds, raw_hours=DEFAULT_RAW_HOURS, minute_days=DEFAULT_MINUTE_DAYS):
	"""Resolution to read for a look-back window: RAW, MINUTE or HOUR.

	Raw for short windows (rules of a few minutes, the 1 h chart), 1-minute rollups for hours,
	1-hour rollups for days. A resolution is only picked if its retention covers the window.
	"""
	raw_hours = raw_hours or DEFAULT_RAW_HOURS
	minute_days = minute_days or DEFAULT_MINUTE_DAYS
	if window_seconds <= min(RAW_MAX_WINDOW, raw_hours * 3600):
		return RAW
	if window_seconds <= min(MINUTE_MAX_WINDOW, minute_days * 86400):
		return MINUTE
	return HOUR


def chart_bucket(window_seconds, resolution, points=120):
	"""Chart bucket size: about `points` buckets, at least a minute, a multiple of the resolution."""
	step = max(resolution, 60)
	bucket = max(60, window_seconds // points)
	return -(-bucket // step) * step


def plan_segments(resolution, since, until, minute_until=None, hour_until=None):
	"""Split [since, until] into reads: [(resolution, start, end), ...] oldest first.

	Rollup segments cover buckets with start <= bucket_start < end, raw segments samples with
	start <= timestamp <= end. `minute_until` / `hour_until` are the rollup watermarks (None
	when nothing was rolled yet). A rollup segment starts at the first whole bucket after
	`since`, so up to one bucket at the head of a long window is left out.
	"""
	segments, cursor = [], since
	for res, watermark in ((HOUR, hour_until), (MINUTE, minute_until)):
		if res > resolution or resolution == RAW or not watermark:
			continue
		start = ceil(cursor, res)
		end = min(watermark, floor(until, res))
		if end > start:
			segments.append((res, start, end))
			cursor = end
	if cursor <= until:
		segments.append((RAW, cursor, until))
	return segments


def new_partial(value, ts):
	return {"count": 1, "sum": value, "min": value, "max": value, "last": value, "last_ts": ts}


def merge(a, b):
	"""Combine two partial aggregates (dicts with count, sum, min, max, last, last_ts)."""
	if a is None:
		return dict(b)
	if b is None:
		return dict(a)
	out = {
		"count": a["count"] + b["count"],
		"sum": a["sum"] + b["sum"],
		"min": min(a["min"], b["min"]),
		"max": max(a["max"], b["max"]),
	}
	latest = b if (b["last_ts"], b["last"]) > (a["last_ts"], a["last"]) else a
	out["last"], out["last_ts"] = latest["last"], latest["last_ts"]
	if "last_label" in latest:  # aggregate() reads: label of the latest sample
		out["last_label"] = latest["last_label"]
	return out


def rollup_samples(rows, seconds):
	"""Raw samples -> buckets.

	rows: (metric_name, host, bench, site, label_key, value, timestamp) tuples.
	Returns {(metric_name, host, bench, site, label_key, bucket_start): partial}.
	"""
	out = {}
	for metric_name, host, bench, site, label_key, value, ts in rows:
		key = (metric_name, host, bench, site, label_key, floor(ts, seconds))
		out[key] = merge(out.get(key), new_partial(float(value), ts))
	return out


def rollup_rollups(rows, seconds):
	"""Finer rollup rows -> coarser buckets.

	rows: (metric_name, host, bench, site, label_key, bucket_start, count, sum, min, max, last, last_ts).
	"""
	out = {}
	for metric_name, host, bench, site, label_key, start, count, total, low, high, last, last_ts in rows:
		key = (metric_name, host, bench, site, label_key, floor(start, seconds))
		part = {
			"count": int(count),
			"sum": float(total),
			"min": float(low),
			"max": float(high),
			"last": float(last),
			"last_ts": last_ts,
		}
		out[key] = merge(out.get(key), part)
	return out


def clamp_sum(partial):
	"""Bound a partial's sum to the column range. True if it was clamped.

	A sum of many large values (a custom metric in bytes) can exceed the column even when each
	value fits; an INSERT error would stop the rollup job, and with it the purge, for good.
	"""
	total = partial["sum"]
	if abs(total) <= MAX_STORED:
		return False
	partial["sum"] = MAX_STORED if total > 0 else -MAX_STORED
	return True


def rollup_name(resolution, key):
	"""Deterministic row name, so a bucket can never be stored twice."""
	text = "|".join([str(resolution)] + ["\\0" if part is None else str(part) for part in key])
	return hashlib.sha1(text.encode()).hexdigest()[:32]


def finalize(partial, aggregation):
	"""Value of one aggregation from a partial; None if there is no sample."""
	if not partial or not partial["count"]:
		return None
	if aggregation == "avg":
		return partial["sum"] / partial["count"]
	if aggregation == "count":
		return partial["count"]
	if aggregation in ("min", "max", "sum", "last"):
		return partial[aggregation]
	raise ValueError(f"unknown aggregation {aggregation!r}")


def late_restart(timestamps, recent, oldest_allowed, seconds, watermark=None):
	"""Watermark to restart rolling from after inserting samples with these timestamps, or None.

	Samples older than `recent` may belong to a bucket that is rolled already (agent spool
	replay, a slow queue), and so do samples behind the current `watermark` however recent
	they are (the wall clock going back, as at the DST fall-back hour): rolling restarts from
	the bucket of the oldest of them. Samples older than `oldest_allowed` are ignored: raw
	samples that old may be purged already, and re-rolling their buckets from what is left
	would lose data.
	"""
	late = [
		t
		for t in timestamps
		if t is not None and oldest_allowed <= t and (t < recent or (watermark is not None and t < watermark))
	]
	return floor(min(late), seconds) if late else None


def late_horizon(now, raw_hours):
	"""Oldest sample time that may still be re-rolled: raw retention, less a margin for the purge.

	The margin is an hour, or a quarter of the retention when that is shorter, so a short raw
	retention does not turn late re-rolling off.
	"""
	retention = timedelta(hours=raw_hours)
	return now - retention + min(timedelta(hours=1), retention / 4)
