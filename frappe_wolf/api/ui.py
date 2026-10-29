"""Read APIs for the Wolf Overview page and incident form."""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from frappe_wolf import satellite
from frappe_wolf.wolf import access
from frappe_wolf.wolf.incidents import get_timeline, parse_payload
from frappe_wolf.wolf.series import bucket_series

RANGES = {"1h": 60, "6h": 360, "24h": 1440, "7d": 10080}
READ_ROLES = ("System Manager", "Wolf Manager", "Wolf User")


def _since(range_key):
	return add_to_date(now_datetime(), minutes=-RANGES.get(range_key, 1440))


def _filters(site=None, bench=None, host=None):
	return {k: v for k, v in (("site", site), ("bench", bench), ("host", host)) if v}


@frappe.whitelist()
def get_overview(range="24h", site=None, bench=None, host=None):
	frappe.only_for(READ_ROLES)
	since = _since(range)
	scope = _filters(site, bench, host)

	incidents = frappe.get_list(
		"Wolf Incident",
		filters={**scope, "started_at": (">=", since)},
		or_filters=None,
		fields=["name", "title", "status", "severity", "started_at", "resolved_at", "site", "bench", "host", "event_count", "deployment_at"],
		order_by="started_at desc",
		limit=50,
	)
	open_incidents = frappe.get_list(
		"Wolf Incident",
		filters={**scope, "status": ("in", ("Open", "Acknowledged"))},
		fields=["name", "title", "status", "severity", "started_at", "site", "bench", "host", "event_count"],
		order_by="started_at desc",
		limit=50,
	)
	event_rows = frappe.get_list(
		"Wolf Event",
		filters={**scope, "timestamp": (">=", since)},
		fields=["name", "title", "event_type", "category", "severity", "timestamp", "count", "site", "bench", "host", "incident", "source"],
		order_by="timestamp desc",
		limit=200,
	)
	allowed, allowed_params = access.sql_condition("Wolf Event")
	counts = frappe.db.sql(
		"""select event_type, severity, sum(`count`) as total from `tabWolf Event`
		where `timestamp` >= %(since)s {scope}{allowed} group by event_type, severity""".format(
			scope="".join(f" and `{k}` = %({k})s" for k in scope), allowed=allowed
		),
		{"since": since, **scope, **allowed_params},
		as_dict=True,
	)
	silence = cint(frappe.db.get_single_value("Wolf Settings", "agent_silence_minutes")) or 5
	agents = frappe.get_list(
		"Wolf Agent",
		fields=["name", "hostname", "kind", "status", "last_seen", "agent_version"],
		order_by="hostname asc",
	)
	cutoff = add_to_date(now_datetime(), minutes=-silence)
	for a in agents:
		a.online = bool(a.status == "Active" and a.last_seen and a.last_seen >= cutoff)

	return {
		"open_incidents": open_incidents,
		"incidents": incidents,
		"events": event_rows,
		"counts": counts,
		"agents": agents,
		"scopes": _known_scopes(),
		"wolf": satellite.status(),
	}


def _known_scopes():
	since = add_to_date(now_datetime(), days=-7)
	allowed, allowed_params = access.sql_condition("Wolf Event")
	out = {}
	for field in ("site", "bench", "host"):
		out[field] = frappe.db.sql_list(
			f"""select distinct `{field}` from `tabWolf Event`
			where `timestamp` >= %(since)s and coalesce(`{field}`, '') != ''{allowed}
			order by `{field}` limit 200""",
			{"since": since, **allowed_params},
		)
	# a satellite's hostname is its site name, not a host
	agents = frappe.get_list("Wolf Agent", filters={"kind": ("!=", "satellite")}, pluck="hostname")
	out["host"] = sorted(set(out["host"]) | set(agents))
	return out


@frappe.whitelist()
def get_metric_series(host, metrics=None, range="6h", site=None):
	"""Time series per metric (and label) for charts, bucketed to at most ~120 points."""
	frappe.only_for(READ_ROLES)
	metrics = frappe.parse_json(metrics) if metrics else ["cpu_usage", "memory_used_percent", "disk_used_percent", "load_per_core"]
	minutes = RANGES.get(range, 360)
	bucket = max(60, minutes * 60 // 120)
	params = {
		"host": host,
		"metrics": tuple(metrics),
		"since": _since(range),
		"until": add_to_date(now_datetime(), minutes=1),
	}
	site_clause = ""
	if site:
		site_clause = " and site = %(site)s"
		params["site"] = site
	allowed, allowed_params = access.sql_condition("Wolf Metric Sample")
	site_clause += allowed
	params.update(allowed_params)
	rows = frappe.db.sql(
		f"""select metric_name, label_key, `timestamp`, `value`
		from `tabWolf Metric Sample`
		where host = %(host)s and metric_name in %(metrics)s
			and `timestamp` between %(since)s and %(until)s {site_clause}""",
		params,
		as_dict=True,
	)
	series = bucket_series(rows, bucket)
	return {"bucket_seconds": bucket, "series": series}


@frappe.whitelist()
def get_incident_timeline(incident):
	doc = frappe.get_doc("Wolf Incident", incident)
	doc.check_permission("read")
	# the incident is visible, but its time window may hold other scopes' events
	rows = [r for r in get_timeline(doc) if r.incident == doc.name or access.visible("Wolf Event", r)]
	for r in rows:
		r.payload = parse_payload(r)
		r.linked = r.incident == doc.name
		r.pop("incident", None)
	return {"incident": doc.name, "started_at": doc.started_at, "resolved_at": doc.resolved_at, "events": rows}
