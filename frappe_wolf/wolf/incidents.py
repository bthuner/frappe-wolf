"""Incident lifecycle: open / update / resolve, event correlation and timelines."""

import json

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from frappe_wolf.wolf.conditions import describe_group
from frappe_wolf.wolf.events import scope_clause

OPEN = ("Open", "Acknowledged")


def find_open(rule, key):
	return frappe.db.get_value(
		"Wolf Incident",
		{"alert_rule": rule, "group_key": key, "status": ("in", OPEN)},
		"name",
		order_by="started_at desc",
	)


def find_recently_resolved(rule, key, minutes):
	if not minutes:
		return None
	return frappe.db.get_value(
		"Wolf Incident",
		{
			"alert_rule": rule,
			"group_key": key,
			"status": "Resolved",
			"resolved_at": (">=", add_to_date(now_datetime(), minutes=-cint(minutes))),
		},
		"name",
		order_by="resolved_at desc",
	)


def latest_deployment(host=None, bench=None, site=None, minutes=30, before=None):
	before = before or now_datetime()
	clause, params = scope_clause(host, bench, site)
	params.update(since=add_to_date(before, minutes=-cint(minutes)), before=before)
	rows = frappe.db.sql(
		f"""select name, `timestamp` from `tabWolf Event`
		where event_type = 'deployment' and `timestamp` between %(since)s and %(before)s and {clause}
		order by `timestamp` desc limit 1""",
		params,
		as_dict=True,
	)
	return rows[0] if rows else None


def open_incident(rule, key, scope, summary, started_at=None):
	"""Create, update or reopen the incident for (rule, key).

	Returns (incident_name, kind) where kind is "fired", "reopened" or None when an
	already-open incident was just updated (no notification needed).
	"""
	now = now_datetime()
	name = find_open(rule.name, key)
	kind = None
	if not name:
		name = find_recently_resolved(rule.name, key, rule.cooldown_minutes)
		if name:
			kind = "reopened"
			frappe.db.set_value("Wolf Incident", name, {"status": "Open", "resolved_at": None})

	if name:
		frappe.db.set_value("Wolf Incident", name, {"last_event_at": now, "summary": summary})
		return name, kind

	settings = frappe.get_cached_doc("Wolf Settings")
	deploy = latest_deployment(
		scope.get("host"),
		scope.get("bench"),
		scope.get("site"),
		minutes=cint(settings.deployment_window_minutes) or 30,
	)
	doc = frappe.get_doc(
		{
			"doctype": "Wolf Incident",
			"title": f"{rule.rule_name}: {describe_group(rule.group_by, scope)}"[:255],
			"status": "Open",
			"severity": rule.severity,
			"alert_rule": rule.name,
			"group_key": key,
			"started_at": started_at or now,
			"last_event_at": now,
			"host": scope.get("host"),
			"bench": scope.get("bench"),
			"site": scope.get("site"),
			"summary": summary,
			"deployment_event": deploy.name if deploy else None,
			"deployment_at": deploy.timestamp if deploy else None,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name, "fired"


def resolve(name, reason="Condition cleared"):
	frappe.db.set_value(
		"Wolf Incident",
		name,
		{"status": "Resolved", "resolved_at": now_datetime(), "summary": reason},
	)


def link_events(incident, names):
	if not names:
		return
	frappe.db.sql(
		"""update `tabWolf Event` set incident = %(incident)s
		where name in %(names)s and ifnull(incident, '') = ''""",
		{"incident": incident, "names": tuple(names)},
	)
	refresh_event_count(incident)


def refresh_event_count(incident):
	count = frappe.db.count("Wolf Event", {"incident": incident})
	frappe.db.set_value("Wolf Incident", incident, "event_count", count, update_modified=False)


def attach_to_open_incident(event_name, event):
	"""Correlate a freshly ingested event with an open incident in the same scope."""
	if event["severity"] == "info" and event["event_type"] != "deployment":
		return None
	rows = frappe.db.sql(
		"""select name from `tabWolf Incident`
		where status in ('Open', 'Acknowledged') and (
			(ifnull(site, '') != '' and site = %(site)s)
			or (ifnull(bench, '') != '' and bench = %(bench)s and ifnull(host, '') in ('', %(host)s))
			or (host = %(host)s and ifnull(bench, '') = '' and ifnull(site, '') = '')
		)
		order by started_at desc limit 1""",
		{"site": event.get("site") or "", "bench": event.get("bench") or "", "host": event.get("host") or ""},
	)
	if not rows:
		return None
	incident = rows[0][0]
	frappe.db.set_value("Wolf Event", event_name, "incident", incident, update_modified=False)
	frappe.db.set_value("Wolf Incident", incident, "last_event_at", event["timestamp"], update_modified=False)
	refresh_event_count(incident)
	return incident


def get_timeline(incident, limit=300):
	"""Events linked to the incident plus everything in the same scope during its window."""
	inc = frappe.get_doc("Wolf Incident", incident) if isinstance(incident, str) else incident
	padding = cint(frappe.db.get_single_value("Wolf Settings", "timeline_padding_minutes")) or 30
	start = add_to_date(get_datetime(inc.started_at or inc.creation), minutes=-padding)
	end = get_datetime(inc.resolved_at) if inc.resolved_at else now_datetime()
	clause, params = scope_clause(inc.host, inc.bench, inc.site)
	params.update(incident=inc.name, start=start, end=end, limit=cint(limit))
	return frappe.db.sql(
		f"""select name, title, event_type, category, severity, source, `timestamp`, last_seen,
			`count`, host, bench, site, incident, payload
		from `tabWolf Event`
		where incident = %(incident)s
			or (`timestamp` between %(start)s and %(end)s and {clause})
		order by `timestamp` asc
		limit %(limit)s""",
		params,
		as_dict=True,
	)


def parse_payload(row):
	try:
		return json.loads(row.get("payload") or "{}")
	except ValueError:
		return {}
