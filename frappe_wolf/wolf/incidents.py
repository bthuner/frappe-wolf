"""Incident lifecycle: open / update / resolve, event correlation and timelines."""

import json

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from frappe_wolf.wolf import live
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
			# a merged incident stays closed; breaches go to its merge target instead
			"merged_into": ("is", "not set"),
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


def seen_when_resolved(rule, key, newest_at):
	"""True when the last resolved incident of (rule, key) was resolved after `newest_at`, the
	newest matching record: whoever resolved it had it in front of them. Any cooldown."""
	resolved_at = frappe.db.get_value(
		"Wolf Incident",
		{"alert_rule": rule, "group_key": key, "status": "Resolved", "merged_into": ("is", "not set")},
		"resolved_at",
		order_by="resolved_at desc",
	)
	return bool(resolved_at) and get_datetime(newest_at) <= get_datetime(resolved_at)


def open_incident(rule, key, scope, summary, started_at=None, newest_at=None):
	"""Create, update or reopen the incident for (rule, key).

	Returns (incident_name, kind) where kind is "fired", "reopened" or None when an
	already-open incident was just updated (no notification needed). Returns (None, None)
	when `newest_at` (the newest matching record, event rules) is not newer than the
	resolution of the just-resolved incident: whoever resolved it had already seen it.
	"""
	now = now_datetime()
	name = find_open(rule.name, key)
	kind = None
	if not name:
		from frappe_wolf.wolf.workflow import merge_target_for

		# Merged by hand into another open incident: keep feeding that one, notify nobody again.
		target = merge_target_for(rule.name, key)
		if target:
			frappe.db.set_value("Wolf Incident", target, "last_event_at", now, update_modified=False)
			live.incident_changed(target)
			return target, None
		if newest_at and seen_when_resolved(rule.name, key, newest_at):
			return None, None
		name = find_recently_resolved(rule.name, key, rule.cooldown_minutes)
		if name:
			kind = "reopened"
			frappe.db.set_value("Wolf Incident", name, {"status": "Open", "resolved_at": None, "resolved_by": None})
			from frappe_wolf.api import journal

			journal.incident_set(name, {"status": "Open"}, "reopened by its rule")
			# no Version records it: say it where the incident's activity is read
			frappe.get_doc("Wolf Incident", name).add_comment(
				"Info", f"Reopened by rule {rule.rule_name}: it matched again after the incident was resolved."
			)

	if name:
		frappe.db.set_value("Wolf Incident", name, {"last_event_at": now, "summary": summary})
		live.incident_changed(name)
		return name, kind

	settings = frappe.get_cached_doc("Wolf Settings")
	# the change that preceded the incident: a rule evaluates after the fact, and a deployment
	# made since the incident started (a fix, a migrate on restart) is not what came before it
	# made since the incident started (a fix, a migrate on restart) is not what came before it.
	# started_at can be early (a metric window's first sample): then the latest before now.
	where = (scope.get("host"), scope.get("bench"), scope.get("site"))
	minutes = cint(settings.deployment_window_minutes) or 30
	deploy = (started_at and latest_deployment(*where, minutes=minutes, before=min(get_datetime(started_at), now))) or (
		latest_deployment(*where, minutes=minutes, before=now)
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
		{"status": "Resolved", "resolved_at": now_datetime(), "resolved_by": None, "summary": reason},
	)
	live.incident_changed(name)
	from frappe_wolf.api import journal

	journal.incident_set(name, {"status": "Resolved", "summary": reason}, "resolved automatically")


def link_events(incident, names):
	if not names:
		return
	frappe.db.sql(
		"""update `tabWolf Event` set incident = %(incident)s
		where name in %(names)s and coalesce(incident, '') = ''""",
		{"incident": incident, "names": tuple(names)},
	)
	refresh_event_count(incident)


def refresh_event_count(incident):
	count = frappe.db.count("Wolf Event", {"incident": incident})
	frappe.db.set_value("Wolf Incident", incident, "event_count", count, update_modified=False)
	live.incident_changed(incident)


def attach_to_open_incident(event_name, event):
	"""Correlate a freshly ingested event with an open incident in the same scope."""
	if event["severity"] == "info" and event["event_type"] != "deployment":
		return None
	rows = frappe.db.sql(
		"""select name from `tabWolf Incident`
		where status in ('Open', 'Acknowledged') and (
			(coalesce(site, '') != '' and site = %(site)s)
			or (coalesce(site, '') = '' and coalesce(bench, '') != '' and bench = %(bench)s
				and coalesce(host, '') in ('', %(host)s))
			or (host = %(host)s and coalesce(bench, '') = '' and coalesce(site, '') = '')
		)
		order by
			-- most specific scope first: a site event belongs to the site's incident
			-- rather than to a host-wide one (disk, load) opened later
			case
				when coalesce(site, '') != '' and site = %(site)s then 0
				when coalesce(bench, '') != '' and bench = %(bench)s then 1
				else 2
			end,
			started_at desc
		limit 1""",
		{"site": event.get("site") or "", "bench": event.get("bench") or "", "host": event.get("host") or ""},
	)
	if not rows:
		return None
	incident = rows[0][0]
	frappe.db.set_value("Wolf Event", event_name, "incident", incident, update_modified=False)
	frappe.db.set_value("Wolf Incident", incident, "last_event_at", event["timestamp"], update_modified=False)
	refresh_event_count(incident)
	return incident


def incident_window(inc):
	"""(start, end, padding_minutes) of an incident's timeline: its start less the padding, to its
	resolution or now. Desk, the postmortem and the Wolf UI read the same window."""
	padding = cint(frappe.db.get_single_value("Wolf Settings", "timeline_padding_minutes")) or 30
	start = add_to_date(get_datetime(inc.started_at or inc.creation), minutes=-padding)
	end = get_datetime(inc.resolved_at) if inc.resolved_at else now_datetime()
	return start, end, padding


def get_timeline(incident, limit=300):
	"""Events linked to the incident plus everything in the same scope during its window."""
	inc = frappe.get_doc("Wolf Incident", incident) if isinstance(incident, str) else incident
	start, end, _padding = incident_window(inc)
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
