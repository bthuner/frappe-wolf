"""Overview and site/bench/host dashboards of the Wolf UI (docs/ui-api.md).

Signals show the newest value of each series with its freshness and the alert rules that
apply to it; the dashboard never invents a threshold of its own. HTTP charts keep requests,
5xx and 502 apart (502 is part of 5xx) and keep gaps as gaps.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from frappe_wolf import satellite
from frappe_wolf.api.scope import _scope, _window
from frappe_wolf.api.ui import READ_ROLES, agent_cutoff
from frappe_wolf.wolf import access, catalog, events, health, inventory, rollups, storage, window

SERIES_KEY = ("metric_name", "host", "bench", "site", "label_key")
RECENT = timedelta(hours=2)  # the newest value is read from here (1m rollups + raw tail)
LOOKBACK_DAYS = 7  # a series silent longer than this counts as missing
MAX_SERIES = 300
STATE_ORDER = {"firing": 0, "stale": 1, "watched": 2, "unwatched": 3}
HTTP_METRICS = ("http_requests", "http_5xx", "http_502")
EMAIL_CATEGORIES = ("email_queue_backlog", "email_queue_error")


def _where(scope, metrics):
	where = ["metric_name in %(metrics)s", *(f"`{k}` = %({k})s" for k in scope)]
	params = {"metrics": tuple(metrics), **scope}
	allowed, allowed_params = access.sql_condition("Wolf Metric Rollup")  # same columns as samples
	if allowed:
		where.append(allowed.removeprefix(" and "))
		params.update(allowed_params)
	return where, params


def _rules(metrics, restricted):
	rows = frappe.get_all(
		"Wolf Alert Rule",
		filters={"enabled": 1, "signal_type": "metric", "metric_name": ("in", metrics)},
		fields=["name", "rule_name", "metric_name", "aggregation", "operator", "threshold", "window_minutes",
				"filter_host", "filter_bench", "filter_site", "severity"],  # fmt: skip
	)
	public = []
	for r in rows:
		# a restricted user sees what a rule checks, not which other sites or hosts it names
		shown = {k: r[k] for k in ("name", "rule_name", "metric_name", "aggregation", "operator", "threshold", "window_minutes", "severity")}
		if not restricted:
			shown.update({k: r[k] for k in ("filter_host", "filter_bench", "filter_site") if r[k]})
		public.append((r, shown))
	return public


@frappe.whitelist()
def get_signals(site=None, bench=None, host=None):
	"""Newest value, freshness and rule state of every signal series in scope (catalog.state)."""
	frappe.only_for(READ_ROLES)
	scope = _scope(site, bench, host)
	tz = events.system_tz()
	now = now_datetime()
	restricted = access.grants() is not None
	metrics = [m for m in catalog.CATALOG if m not in HTTP_METRICS]
	where, params = _where(scope, metrics)

	recent = rollups.aggregate(where, params, list(SERIES_KEY), [], "last", storage.MINUTE, now - RECENT, now)
	series = {tuple(r.get(k) for k in SERIES_KEY): {**{k: r.get(k) for k in SERIES_KEY}, "value": r.value, "last_at": get_datetime(r.last_at)}
			  for r in recent}  # fmt: skip
	# quieter series: when they were last seen, from the hourly rollups (no value: it is old)
	older = frappe.db.sql(
		f"""select metric_name, host, bench, site, label_key, max(last_ts) as last_at from `tabWolf Metric Rollup`
		where resolution = 3600 and bucket_start >= %(since)s and {' and '.join(where)}
		group by metric_name, host, bench, site, label_key""",
		{**params, "since": now - timedelta(days=LOOKBACK_DAYS)},
		as_dict=True,
	)
	for r in older:
		key = tuple(r.get(k) for k in SERIES_KEY)
		if key not in series:
			series[key] = {**{k: r.get(k) for k in SERIES_KEY}, "value": None, "last_at": get_datetime(r.last_at)}

	rules = _rules(metrics, restricted)
	incidents = frappe.get_list(
		"Wolf Incident",
		filters={"status": ("in", ("Open", "Acknowledged")), "alert_rule": ("in", [r["name"] for r, _ in rules] or [""])},
		fields=["name", "alert_rule", "host", "bench", "site", "severity"],
		limit_page_length=0,
	)
	out, counts = [], defaultdict(lambda: defaultdict(int))
	for s in series.values():
		spec = catalog.CATALOG[s["metric_name"]]
		st = catalog.state(s, now, [r for r, _ in rules], incidents)
		counts[spec["group"]][st] += 1
		names = {r["name"] for r, _ in rules if catalog.rule_applies(r, s)}
		out.append({
			**{k: s[k] for k in SERIES_KEY},
			"group": spec["group"], "label": spec["label"], "unit": spec["unit"], "kind": spec["kind"],
			"value": s["value"], "last_at": window.to_utc(s["last_at"], tz), "state": st,
			"rules": [shown for r, shown in rules if r["name"] in names],
			"incidents": [i.name for i in incidents if i.alert_rule in names and catalog.incident_matches(i, s)],
		})  # fmt: skip
	out.sort(key=lambda s: (STATE_ORDER[s["state"]], catalog.GROUPS.index(s["group"]), s["metric_name"], str(s["host"]), str(s["bench"]), str(s["site"]), str(s["label_key"])))
	seen = {s["metric_name"] for s in out}
	return {
		"scope": scope,
		"restricted": restricted,
		"series": out[:MAX_SERIES],
		"truncated": len(out) > MAX_SERIES,
		"total_series": len(out),
		# exact, whatever the series limit
		"counts": {g: dict(c) for g, c in counts.items()},
		# signals without any series in the lookback: not collected for this scope, not set up,
		# or not supported here. Never shown as zero.
		"missing": [catalog.CATALOG[m] for m in metrics if m not in seen],
		"caveats": catalog.CAVEATS,
		"email": _email(scope, now, tz),
		"lookback_days": LOOKBACK_DAYS,
		"time_zone": tz,
	}


def _email(scope, now, tz):
	"""Email queue problems the site recorded about itself (wolf/local_signals.py), last 24 h."""
	where = "".join(f" and `{k}` = %({k})s" for k in scope)
	allowed, params = access.sql_condition("Wolf Event")
	rows = frappe.db.sql(
		f"""select category, count(*) as records, max(coalesce(last_seen, `timestamp`)) as last_at from `tabWolf Event`
		where category in %(cats)s and coalesce(last_seen, `timestamp`) >= %(since)s{where}{allowed}
		group by category""",
		{**params, **scope, "cats": EMAIL_CATEGORIES, "since": now - timedelta(hours=24)},
		as_dict=True,
	)
	return {r.category: {"records": cint(r.records), "last_at": window.to_utc(get_datetime(r.last_at), tz)} for r in rows}


@frappe.whitelist()
def get_http(range=None, since=None, until=None, site=None, bench=None, host=None):
	"""Requests, 5xx and 502 per bucket over the window, with the deployments in it."""
	frappe.only_for(READ_ROLES)
	win, scope = _window(range, since, until), _scope(site, bench, host)
	tz = events.system_tz()
	seconds = (win.until - win.since).total_seconds()
	resolution = rollups.resolution_for(seconds)
	bucket = storage.chart_bucket(seconds, resolution)
	where, params = _where(scope, HTTP_METRICS)
	rows = rollups.series_rows(where, params, resolution, win.since, win.until)

	# interval counts: the sum per bucket, every label together. A bucket without samples is a
	# gap (None), not a zero.
	sums = defaultdict(dict)
	for r in rows:
		start = storage.floor(get_datetime(r["timestamp"]), bucket)
		sums[r["metric_name"]][start] = sums[r["metric_name"]].get(start, 0) + float(r["value"])
	first = storage.floor(win.since, bucket)
	buckets, t = [], first
	while t <= win.until:
		buckets.append(t)
		t += timedelta(seconds=bucket)
	series = {
		m: [[window.to_utc(b, tz), round(sums[m][b], 2) if b in sums[m] else None] for b in buckets] for m in HTTP_METRICS
	}

	ev_where = "".join(f" and `{k}` = %({k})s" for k in scope)
	allowed, ev_params = access.sql_condition("Wolf Event")
	deployments = frappe.db.sql(
		f"""select name, title, `timestamp`, host, bench, site, incident from `tabWolf Event`
		where event_type = 'deployment' and `timestamp` between %(since)s and %(until)s{ev_where}{allowed}
		order by `timestamp` desc limit 50""",
		{**ev_params, **scope, "since": win.since, "until": win.until},
		as_dict=True,
	)
	return {
		"window": window.describe(win),
		"scope": scope,
		"bucket_seconds": bucket,
		"resolution": storage.LABELS[resolution],
		"series": series,
		"has_data": {m: bool(sums[m]) for m in HTTP_METRICS},
		"deployments": [
			{"name": d.name, "title": d.title, "at": window.to_utc(d.timestamp, tz), "host": d.host, "bench": d.bench,
			 "site": d.site, "incident": d.incident}  # fmt: skip
			for d in deployments
		],
		"notes": ["502 responses are part of the 5xx count, not in addition to it."],
		"time_zone": tz,
	}


@frappe.whitelist()
def get_monitoring():
	"""Is Wolf itself working: rules and scheduler, ingest queue, rollups, agents, satellites."""
	frappe.only_for(READ_ROLES)
	tz = events.system_tz()
	silence, cutoff = agent_cutoff()
	agents = frappe.get_list(
		"Wolf Agent",
		fields=["name", "hostname", "kind", "status", "last_seen", "clock_skew_seconds", "agent_version"],
		order_by="hostname asc",
		limit_page_length=0,
	)
	marks = rollups.watermarks()
	return {
		"health": health.safe_health(),
		"rollups": {storage.LABELS[res]: window.to_utc(at, tz) for res, at in marks.items()},
		"agents": [
			{
				"name": a.name, "hostname": a.hostname, "kind": a.kind or "agent", "status": a.status,
				"state": inventory.agent_state(a.status, a.last_seen, cutoff),
				"last_seen": window.to_utc(a.last_seen, tz), "clock_skew_seconds": a.clock_skew_seconds,
				"agent_version": a.agent_version,
			}  # fmt: skip
			for a in agents
		],
		"silence_minutes": silence,
		"mode": satellite.status(),
		"time_zone": tz,
		"checked_at": window.to_utc(now_datetime(), tz),
		"note": "An online agent does not mean its watchdog has a fallback channel configured.",
	}
