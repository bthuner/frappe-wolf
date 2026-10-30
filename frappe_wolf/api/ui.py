"""Read APIs for the Wolf Overview page and incident form."""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from frappe_wolf import satellite
from frappe_wolf.wolf import access, inventory, rollups, schema, storage, window
from frappe_wolf.wolf.incidents import get_timeline, parse_payload
from frappe_wolf.wolf.series import bucket_series
from frappe_wolf.wolf.workflow import get_activity

# the chart ranges Desk offers (any range wolf/window.py parses is accepted)
RANGES = {"1h": 60, "6h": 360, "24h": 1440, "7d": 10080}
READ_ROLES = ("System Manager", "Wolf Manager", "Wolf User")


def _minutes(range_key, default="24h"):
	"""Length of a relative range ('1h', '24h', '15m', '30d': wolf/window.py) in minutes."""
	try:
		return int(window.parse_range(range_key or default).total_seconds() // 60)
	except schema.PayloadError as e:
		frappe.throw(str(e), frappe.ValidationError)


def _since(range_key):
	return add_to_date(now_datetime(), minutes=-_minutes(range_key))


def agent_cutoff():
	"""(minutes, cutoff): agents seen since the cutoff are online (Wolf Settings agent silence)."""
	silence = cint(frappe.db.get_single_value("Wolf Settings", "agent_silence_minutes")) or 5
	return silence, add_to_date(now_datetime(), minutes=-silence)


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
	agents = frappe.get_list(
		"Wolf Agent",
		fields=["name", "hostname", "kind", "status", "last_seen", "agent_version"],
		order_by="hostname asc",
	)
	_, cutoff = agent_cutoff()
	for a in agents:
		a.online = inventory.agent_state(a.status, a.last_seen, cutoff) == "fresh"

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
	"""Time series per metric (and label) for charts, bucketed to at most ~120 points.

	Reads raw samples for the 1 h range and minute or hour rollups for longer ones
	(plus the raw samples not rolled up yet), see wolf/rollups.py.
	"""
	frappe.only_for(READ_ROLES)
	metrics = frappe.parse_json(metrics) if metrics else ["cpu_usage", "memory_used_percent", "disk_used_percent", "load_per_core"]
	minutes = _minutes(range, "6h")
	resolution = rollups.resolution_for(minutes * 60)
	bucket = storage.chart_bucket(minutes * 60, resolution)
	params = {"host": host, "metrics": tuple(metrics)}
	where = ["host = %(host)s", "metric_name in %(metrics)s"]
	if site:
		where.append("site = %(site)s")
		params["site"] = site
	# restricted Wolf Users: same scope columns on samples and rollups
	allowed, allowed_params = access.sql_condition("Wolf Metric Sample")
	if allowed:
		where.append(allowed.removeprefix(" and "))
		params.update(allowed_params)
	rows = rollups.series_rows(where, params, resolution, _since(range), add_to_date(now_datetime(), minutes=1))
	series = bucket_series(rows, bucket)
	return {"bucket_seconds": bucket, "resolution": storage.LABELS[resolution], "series": series}


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
	return {
		"incident": doc.name,
		"started_at": doc.started_at,
		"resolved_at": doc.resolved_at,
		"events": rows,
		# comments, assignments and status changes, interleaved with events by render_timeline
		"activity": get_activity(doc),
	}


@frappe.whitelist()
def get_incident_deliveries(incident, limit=100):
	"""Notification attempts for an incident, newest first (no secrets are ever stored)."""
	frappe.get_doc("Wolf Incident", incident).check_permission("read")
	return frappe.get_all(
		"Wolf Delivery",
		filters={"incident": incident},
		fields=["at", "kind", "digest", "channel_type", "target", "action", "ok", "http_status", "error"],
		order_by="at desc, creation desc",
		limit_page_length=min(cint(limit) or 100, 500),
		ignore_permissions=True,
	)
