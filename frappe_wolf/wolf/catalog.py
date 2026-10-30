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
	("db_slow_queries", "database", "Slow queries", "interval_count", "queries", "bench", PUSH_SECONDS),
	("db_replication_running", "database", "Replication running", "gauge", "1 = yes", "bench", PUSH_SECONDS),
	("db_replication_lag_seconds", "database", "Replication lag", "gauge", "s", "bench", PUSH_SECONDS),
	("backup_age_hours", "backups", "Newest backup age", "gauge", "h", "site", 600),
	("tls_days_left", "tls", "Certificate expires in", "gauge", "days", "site", 3600),
	("cpu_usage", "host", "CPU", "gauge", "%", "host", PUSH_SECONDS),
	("memory_used_percent", "host", "Memory used", "gauge", "%", "host", PUSH_SECONDS),
	("disk_used_percent", "host", "Disk used", "gauge", "%", "host", PUSH_SECONDS),
	("load_per_core", "host", "Load per core", "gauge", "per core", "host", PUSH_SECONDS),
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
	"""An open incident is about a series when every scope field it names is the series'."""
	return all(not incident.get(k) or incident.get(k) == series.get(k) for k in ("host", "bench", "site"))


def state(series, now, rules=(), incidents=()):
	"""The state of one series: last sample (`last_at`, may be None), rules and open incidents.

	- missing: no sample in the lookback (not collected here, not configured, or unsupported);
	- stale: samples stopped arriving;
	- firing: a rule on it has an open incident for this scope;
	- watched: rules apply and none has an open incident;
	- unwatched: a value, but no rule on it (the dashboard does not invent a threshold).
	"""
	if series.get("last_at") is None:
		return "missing"
	if now - series["last_at"] > stale_after(series["metric_name"]):
		return "stale"
	applicable = [r for r in rules if rule_applies(r, series)]
	names = {r["name"] for r in applicable}
	if any(i.get("alert_rule") in names and incident_matches(i, series) for i in incidents):
		return "firing"
	return "watched" if applicable else "unwatched"
