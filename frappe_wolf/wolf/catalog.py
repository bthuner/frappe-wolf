"""What each metric means, and the state of one signal series (no frappe imports).

The catalog names every metric the agent sends that a dashboard shows: its unit, its kind and
how often it arrives. `kind` decides how it may be read:

- gauge: a level now (disk used, backup age); the latest value is meaningful;
- interval_count: events counted per push interval (5xx responses, failed jobs added); sum
  them over a window, never read one sample as "the count".

`state` keeps apart what a dashboard must not merge: no data at all, data that stopped, a
value no rule watches, a value its rules accept, and a value a rule has opened an incident for.
"""

from datetime import timedelta

# Default agent cadences. An agent configured with a longer `tls.interval` or
# `backups.interval` will look stale sooner than it is: the server does not know the setting.
PUSH_SECONDS = 30
MIN_STALE = timedelta(minutes=5)

# metric -> (group, label, kind, unit, level, cadence seconds)
_SPEC = [
	("http_requests", "http", "Requests", "interval_count", "requests", "site", PUSH_SECONDS),
	("http_5xx", "http", "5xx responses", "interval_count", "responses", "site", PUSH_SECONDS),
	# a subset of 5xx: never added to it
	("http_502", "http", "502 responses (part of 5xx)", "interval_count", "responses", "site", PUSH_SECONDS),
	("rq_workers", "workers", "RQ workers", "gauge", "workers", "bench", PUSH_SECONDS),
	("queue_length", "workers", "Queue length", "gauge", "jobs", "bench", PUSH_SECONDS),
	("rq_failed_jobs_added", "workers", "Failed jobs added", "interval_count", "jobs", "bench", PUSH_SECONDS),
	# jobs held in RQ's failed registry now (they expire): not the number that failed lately
	("rq_failed_jobs", "workers", "Jobs in the failed registry", "gauge", "jobs", "bench", PUSH_SECONDS),
	("rq_long_running_jobs", "workers", "Long-running jobs", "gauge", "jobs", "bench", PUSH_SECONDS),
	("bench_processes_down", "processes", "Bench processes down", "gauge", "processes", "bench", PUSH_SECONDS),
	("redis_used_memory_percent", "redis", "Redis memory used", "gauge", "%", "bench", PUSH_SECONDS),
	("redis_evicted_keys", "redis", "Redis evicted keys", "interval_count", "keys", "bench", PUSH_SECONDS),
	("db_connections_used_percent", "database", "Database connections used", "gauge", "%", "bench", PUSH_SECONDS),
	("db_threads_connected", "database", "Database connections", "gauge", "connections", "bench", PUSH_SECONDS),
	("db_slow_queries", "database", "Slow queries", "interval_count", "queries", "bench", PUSH_SECONDS),
	("db_replication_running", "database", "Replication running", "gauge", "1 = yes", "bench", PUSH_SECONDS),
	("db_replication_lag_seconds", "database", "Replication lag", "gauge", "s", "bench", PUSH_SECONDS),
	("backup_age_hours", "backups", "Newest backup age", "gauge", "h", "site", 600),
	("tls_days_left", "tls", "Certificate expires in", "gauge", "days", "site", 3600),
	("cpu_usage", "host", "CPU", "gauge", "%", "host", PUSH_SECONDS),
	("memory_used_percent", "host", "Memory used", "gauge", "%", "host", PUSH_SECONDS),
	("disk_used_percent", "host", "Disk used", "gauge", "%", "host", PUSH_SECONDS),
	("load_per_core", "host", "Load per core", "gauge", "per core", "host", PUSH_SECONDS),
	("load_1m", "host", "Load (1 min)", "gauge", "", "host", PUSH_SECONDS),
	("load_5m", "host", "Load (5 min)", "gauge", "", "host", PUSH_SECONDS),
	("load_15m", "host", "Load (15 min)", "gauge", "", "host", PUSH_SECONDS),
	("swap_used_percent", "host", "Swap used", "gauge", "%", "host", PUSH_SECONDS),
]
CATALOG = {
	m: {"metric": m, "group": g, "label": label, "kind": kind, "unit": unit, "level": level, "cadence_seconds": cadence}
	for m, g, label, kind, unit, level, cadence in _SPEC
}
GROUPS = ("workers", "processes", "redis", "database", "backups", "tls", "host")

# what a gauge means for the reader, said once: backup age is not proof a restore works
CAVEATS = {
	"backup_age_hours": "Age of the newest backup file; it does not prove the backup restores.",
	"tls_days_left": "Days until the certificate served on port 443 expires; not a full validity check.",
}


def stale_after(metric):
	"""How long without a sample before a series counts as stale: three cadences, at least 5 min."""
	cadence = CATALOG.get(metric, {}).get("cadence_seconds", PUSH_SECONDS)
	return max(timedelta(seconds=3 * cadence), MIN_STALE)


def rule_applies(rule, series):
	"""A rule watches a series when it is on its metric and its scope filters match."""
	if rule.get("metric_name") != series.get("metric_name"):
		return False
	return all(not rule.get(f"filter_{k}") or rule.get(f"filter_{k}") == series.get(k) for k in ("host", "bench", "site"))


def incident_matches(incident, series):
	"""An open incident is about a series when every scope field it names is the series'.

	A rule grouped by label (host_label) opens one incident per label: its group_key ends with
	"label_key=<label>", and only that series is concerned, not every disk or queue of the host.
	"""
	if not all(not incident.get(k) or incident.get(k) == series.get(k) for k in ("host", "bench", "site")):
		return False
	key = incident.get("group_key") or ""
	return "label_key=" not in key or key.endswith(f"label_key={series.get('label_key') or '-'}")


def is_stale(series, now):
	"""Samples stopped: none for `stale_after`, or none in the recent read (`recent` False)."""
	return series.get("recent") is False or now - series["last_at"] > stale_after(series["metric_name"])


def state(series, now, rules=(), incidents=()):
	"""The state of one series: last sample (`last_at`, may be None), rules and open incidents.

	- missing: no sample in the lookback (not collected here, not configured, or unsupported);
	- firing: a rule on it has an open incident for this scope, whether or not samples still
	  arrive (a host that went silent mid-incident stays on top; see is_stale for the rest);
	- stale: samples stopped arriving;
	- watched: rules apply and none has an open incident;
	- unwatched: a value, but no rule on it (the dashboard does not invent a threshold).
	"""
	if series.get("last_at") is None:
		return "missing"
	applicable = [r for r in rules if rule_applies(r, series)]
	names = {r["name"] for r in applicable}
	if any(i.get("alert_rule") in names and incident_matches(i, series) for i in incidents):
		return "firing"
	if is_stale(series, now):
		return "stale"
	return "watched" if applicable else "unwatched"


# --- exploring a metric ---------------------------------------------------------------------

# the label each metric carries (one per series), for filtering and grouping
LABELS = {
	"disk_used_percent": "mount", "queue_length": "queue", "rq_failed_jobs": "queue",
	"rq_failed_jobs_added": "queue", "rq_long_running_jobs": "queue", "tls_days_left": "domain",
	"bench_processes_down": "manager", "redis_used_memory_percent": "of",
	"db_connections_used_percent": "db", "db_threads_connected": "db", "db_slow_queries": "db", "db_replication_running": "db",
	"db_replication_lag_seconds": "db",
}  # fmt: skip

# what a bucket may say about each kind. A gauge is a level: its average, extremes or last value
# over the bucket. An interval count is a number of things per push: summed over the bucket it
# is the total, its average or maximum is per push interval.
AGGREGATIONS = {
	"gauge": ("avg", "min", "max", "last"),
	"interval_count": ("sum", "avg", "max"),
	"unknown": ("avg", "min", "max", "last"),  # a custom metric: read it as a level
}
DEFAULT_AGGREGATION = {"gauge": "avg", "interval_count": "sum", "unknown": "avg"}

# grouping a metric only by what its series carry: a host-level metric has no bench or site
GROUP_BYS = {
	"host": ("none", "host"),
	"bench": ("none", "host", "bench"),
	"site": ("none", "host", "bench", "site"),
	# a custom metric may carry labels the catalog cannot name: it may be split by them
	"unknown": ("none", "host", "bench", "site", "host_label"),
}


def describe(metric):
	"""What the explorer may do with a metric: catalog fields, aggregations, groupings, label."""
	spec = CATALOG.get(metric) or {
		"metric": metric, "group": "custom", "label": metric, "kind": "unknown", "unit": "",
		"level": "unknown", "cadence_seconds": PUSH_SECONDS,
	}  # fmt: skip
	label = LABELS.get(metric) or ("label" if spec["kind"] == "unknown" else None)
	group_bys = GROUP_BYS[spec["level"]] + (("host_label",) if label and "host_label" not in GROUP_BYS[spec["level"]] else ())
	return {
		**spec,
		"label_key": label,
		"aggregations": AGGREGATIONS[spec["kind"]],
		"default_aggregation": DEFAULT_AGGREGATION[spec["kind"]],
		"group_bys": group_bys,
		"caveat": CAVEATS.get(metric),
	}


def validate(metric, aggregation, group_by):
	"""(aggregation, group_by) checked against the metric's kind and level; ValueError if not allowed."""
	d = describe(metric)
	aggregation = aggregation or d["default_aggregation"]
	group_by = group_by or "none"
	if aggregation not in d["aggregations"]:
		raise ValueError(f"{aggregation} is not meaningful for {metric} ({d['kind']}): use {', '.join(d['aggregations'])}")
	if group_by not in d["group_bys"]:
		raise ValueError(f"{metric} cannot be grouped by {group_by}: use {', '.join(d['group_bys'])}")
	return aggregation, group_by
