"""Time bucketing for metric charts, done in Python so the SQL stays portable."""

from datetime import datetime, timezone


def bucket_start(ts, seconds):
	"""Floor a naive datetime to a multiple of `seconds` (wall-clock arithmetic, no tz shift)."""
	epoch = int(ts.replace(tzinfo=timezone.utc).timestamp())
	return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc).replace(tzinfo=None)


def bucket_series(rows, seconds, counter=lambda name: name.startswith("http_")):
	"""rows: dicts with metric_name, label_key, timestamp, value (any order).

	A row may carry `count`: it then stands for that many samples whose sum is `value` (a
	rollup bucket), so averages stay weighted by samples. Returns {"metric [label]": [["YYYY-MM-DD HH:MM:SS", value], ...]} sorted by time, where
	value is the bucket's sum for counters (http_*) and its average otherwise.
	"""
	acc = {}
	for r in rows:
		key = r["metric_name"] + (f" [{r['label_key']}]" if r.get("label_key") else "")
		slot = acc.setdefault(key, {}).setdefault(bucket_start(r["timestamp"], seconds), [0.0, 0])
		slot[0] += float(r["value"])
		slot[1] += int(r.get("count") or 1)
	series = {}
	for key, buckets in acc.items():
		name = key.split(" [")[0]
		series[key] = [
			[start.strftime("%Y-%m-%d %H:%M:%S"), round(total if counter(name) else total / n, 2)]
			for start, (total, n) in sorted(buckets.items())
		]
	return series
