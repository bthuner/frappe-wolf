"""The incident page of the Wolf UI: detail, timeline pages and actions (docs/ui-api.md).

Reuses the incident query rules (wolf/incidents.py scope, wolf/access.py grants), the activity
history (wolf/workflow.get_activity) and the incident's own whitelisted methods for actions.
"""

import html

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_fullname, now_datetime

from frappe_wolf.api.ui import READ_ROLES
from frappe_wolf.wolf import access, events, incidents, schema, timeline, window
from frappe_wolf.wolf.spa import MANAGER_ROLES
from frappe_wolf.wolf.workflow import get_activity

DELIVERY_CAP = 2000  # deliveries read per incident; more are reported as truncated
ACTIONS = ("acknowledge", "resolve", "assign_to_me")


def _incident(name):
	frappe.only_for(READ_ROLES)
	doc = frappe.get_doc("Wolf Incident", name)
	doc.check_permission("read")
	return doc


def _can(doc):
	manager = bool(set(frappe.get_roles()) & set(MANAGER_ROLES)) or frappe.session.user == "Administrator"
	write = manager and frappe.has_permission("Wolf Incident", "write", doc=doc)
	open_ = doc.status != "Resolved" and not doc.merged_into
	return {
		"comment": True,  # read permission is enough, like Desk's comment box
		"acknowledge": write and doc.status == "Open" and not doc.merged_into,
		"resolve": write and open_,
		"assign_to_me": write and open_ and doc.assigned_to != frappe.session.user,
		"workflow": write,  # merge, split, postmortem (Desk)
	}


def _window(doc):
	padding = cint(frappe.db.get_single_value("Wolf Settings", "timeline_padding_minutes")) or 30
	start = frappe.utils.add_to_date(get_datetime(doc.started_at or doc.creation), minutes=-padding)
	end = get_datetime(doc.resolved_at) if doc.resolved_at else now_datetime()
	return start, end, padding


@frappe.whitelist()
def get_incident(incident):
	"""Everything the incident header needs, in one call."""
	doc = _incident(incident)
	tz = events.system_tz()
	start, end, padding = _window(doc)
	deployment = None
	if doc.deployment_at or doc.deployment_event:
		exists = bool(doc.deployment_event and frappe.db.exists("Wolf Event", doc.deployment_event))
		deployment = {
			"at": window.to_utc(doc.deployment_at, tz),
			"event": doc.deployment_event if exists else None,
			"available": exists,  # the incident keeps the time when retention deleted the event
		}
	rule = None
	if doc.alert_rule:
		rule = {"name": doc.alert_rule, "readable": frappe.has_permission("Wolf Alert Rule", "read", doc=doc.alert_rule)}
	return {
		"name": doc.name,
		"title": doc.title,
		"status": doc.status,
		"severity": doc.severity,
		"scope": {"site": doc.site, "bench": doc.bench, "host": doc.host},
		"summary": doc.summary,
		"started_at": window.to_utc(get_datetime(doc.started_at), tz),
		"last_event_at": window.to_utc(get_datetime(doc.last_event_at) if doc.last_event_at else None, tz),
		"acknowledged": {"at": window.to_utc(get_datetime(doc.acknowledged_at) if doc.acknowledged_at else None, tz),
						 "by": doc.acknowledged_by, "by_name": get_fullname(doc.acknowledged_by) if doc.acknowledged_by else None},  # fmt: skip
		"resolved": {"at": window.to_utc(get_datetime(doc.resolved_at) if doc.resolved_at else None, tz),
					 "by": doc.resolved_by, "by_name": get_fullname(doc.resolved_by) if doc.resolved_by else None},  # fmt: skip
		"assigned_to": {"user": doc.assigned_to, "name": get_fullname(doc.assigned_to)} if doc.assigned_to else None,
		"alert_rule": rule,
		"deployment": deployment,
		"silenced": bool(doc.silenced),
		"notified_at": window.to_utc(get_datetime(doc.notified_at) if doc.notified_at else None, tz),
		"escalated_at": window.to_utc(get_datetime(doc.escalated_at) if doc.escalated_at else None, tz),
		"merged_into": doc.merged_into,
		"split_from": doc.split_from,
		"postmortem": doc.postmortem,
		# event records linked to the incident, and the repeats they absorbed: not the same number
		"event_records": frappe.db.count("Wolf Event", {"incident": doc.name}),
		"event_count": doc.event_count,
		"timeline_window": {"since": window.to_utc(start, tz), "until": window.to_utc(end, tz), "padding_minutes": padding,
							"open_ended": not doc.resolved_at},  # fmt: skip
		"time_zone": tz,
		"can": _can(doc),
	}


def _events_after(doc, start, end, wanted, cursor_key, limit):
	"""Up to limit + 1 event rows after the cursor: linked ones, and visible context in scope."""
	clause, params = events.scope_clause(doc.host, doc.bench, doc.site)
	allowed, allowed_params = access.sql_condition("Wolf Event")
	parts = []
	if "evidence" in wanted:
		parts.append("incident = %(incident)s")
	if "context" in wanted:
		# context: in the incident's scope and window, not linked to it, and visible to the caller
		parts.append(f"(coalesce(incident, '') != %(incident)s and `timestamp` between %(start)s and %(end)s and {clause}{allowed})")
	if not parts:
		return [], False
	keyset = ""
	if cursor_key:
		at, rank, row_id = cursor_key
		params.update(c_at=at, c_name=row_id.removeprefix("event:"))
		# rows after (at, rank, id). Event rows have rank 0 and ids "event:<name>": after an event,
		# a later name at the same time; after a rank-0 row whose id sorts before "event:" (the
		# unavailable deployment), every event at that time; after a later family, a later time.
		if rank == timeline.RANK["event"] and row_id.startswith("event:"):
			keyset = "and (`timestamp` > %(c_at)s or (`timestamp` = %(c_at)s and name > %(c_name)s))"
		elif rank == timeline.RANK["event"] and row_id < "event:":
			keyset = "and `timestamp` >= %(c_at)s"
		else:
			keyset = "and `timestamp` > %(c_at)s"
	params.update(allowed_params, incident=doc.name, start=start, end=end, limit=limit + 1)
	rows = frappe.db.sql(
		f"""select name, title, event_type, category, severity, source, `timestamp`, last_seen,
			`count`, host, bench, site, incident, payload
		from `tabWolf Event`
		where ({' or '.join(parts)}) {keyset}
		order by `timestamp` asc, name asc
		limit %(limit)s""",
		params,
		as_dict=True,
	)
	for r in rows:
		r.payload = incidents.parse_payload(r)
	return rows, len(rows) > limit


@frappe.whitelist()
def get_timeline(incident, cursor=None, limit=100, families=None):
	"""One page of the incident timeline, oldest first (docs/ui-api.md)."""
	doc = _incident(incident)
	tz = events.system_tz()
	limit = min(max(cint(limit) or 100, 1), timeline.MAX_LIMIT)
	try:
		wanted = timeline.families(families)
		cursor_key = timeline.decode_cursor(cursor)
	except ValueError as e:
		frappe.throw(str(e), frappe.ValidationError)
	start, end, padding = _window(doc)

	keyed, totals, truncated = [], {}, {}
	event_rows, more_events = _events_after(doc, start, end, wanted, cursor_key, limit)
	keyed += [timeline.event_row(r, doc.name, tz, doc.deployment_event) for r in event_rows]
	if "context" in wanted and doc.deployment_at and doc.deployment_event and not any(
		r.name == doc.deployment_event for r in event_rows
	):
		if not frappe.db.exists("Wolf Event", doc.deployment_event):
			keyed.append(timeline.deployment_unavailable_row(doc.name, get_datetime(doc.deployment_at), tz))
	elif "context" in wanted and doc.deployment_at and not doc.deployment_event:
		keyed.append(timeline.deployment_unavailable_row(doc.name, get_datetime(doc.deployment_at), tz))

	if "activity" in wanted:
		entries = get_activity(doc)
		for e in entries:
			e["timestamp"] = get_datetime(e["timestamp"]) if e.get("timestamp") else None
			if e.get("kind") in ("comment", "note"):
				# the text as typed: tags dropped, entities decoded; the UI shows it as text
				e["plain"] = html.unescape(frappe.utils.strip_html((e.get("content") or "").replace("<br>", "\n")))
		keyed += [timeline.activity_row(e, doc.name, tz) for e in entries if e.get("timestamp")]
		totals["activity"] = len(entries)
	if "delivery" in wanted:
		deliveries = frappe.get_all(
			"Wolf Delivery",
			filters={"incident": doc.name},
			fields=["name", "at", "kind", "digest", "channel_type", "target", "action", "ok", "http_status", "error"],
			order_by="at asc, name asc",
			limit_page_length=DELIVERY_CAP,
			ignore_permissions=True,  # the incident's read permission covers its deliveries
		)
		keyed += [timeline.delivery_row(d, tz) for d in deliveries if d.at]
		total = frappe.db.count("Wolf Delivery", {"incident": doc.name})
		totals["delivery"] = total
		if total > len(deliveries):
			truncated["delivery"] = {"shown": len(deliveries), "total": total}

	rows, paging = timeline.page(keyed, limit, cursor_key, more_events)
	return {
		"incident": doc.name,
		"rows": rows,
		"paging": paging,
		"families": sorted(wanted),
		"totals": totals,
		"truncated": truncated,
		"window": {"since": window.to_utc(start, tz), "until": window.to_utc(end, tz), "padding_minutes": padding,
				   "open_ended": not doc.resolved_at},  # fmt: skip
		"time_zone": tz,
	}


@frappe.whitelist(methods=["POST"])
def act(incident, action):
	"""Acknowledge, resolve or take an incident, through its own methods (and their checks)."""
	if action not in ACTIONS:
		frappe.throw(_("Unknown action {0}").format(action), frappe.ValidationError)
	doc = _incident(incident)
	frappe.only_for(MANAGER_ROLES)
	doc.check_permission("write")
	if doc.merged_into:
		frappe.throw(_("This incident was merged into {0}.").format(doc.merged_into))
	getattr(doc, action)()
	return get_incident(doc.name)


@frappe.whitelist()
def list_incidents(status="unresolved", cursor=None, limit=50, site=None, bench=None, host=None):
	"""Incidents the caller may see, newest first, paged by (started_at, name).

	status: "unresolved" (Open and Acknowledged), "resolved" or "all". Merged incidents are
	resolved and listed under resolved/all with `merged_into`.
	"""
	frappe.only_for(READ_ROLES)
	tz = events.system_tz()
	limit = min(max(cint(limit) or 50, 1), 200)
	try:
		scope = window.scope_filters(site=site, bench=bench, host=host)
	except schema.PayloadError as e:
		frappe.throw(str(e), frappe.ValidationError)
	conds = {"unresolved": "status in ('Open', 'Acknowledged')", "resolved": "status = 'Resolved'", "all": "1=1"}
	if status not in conds:
		frappe.throw(_("Unknown status filter {0}").format(status), frappe.ValidationError)
	allowed, params = access.sql_condition("Wolf Incident")
	where = [conds[status], *(f"`{k}` = %({k})s" for k in scope)]
	params.update(scope, limit=limit + 1)
	if cursor:
		try:
			at, _rank, name = timeline.decode_cursor(cursor)
		except ValueError as e:
			frappe.throw(str(e), frappe.ValidationError)
		where.append("(started_at < %(c_at)s or (started_at = %(c_at)s and name < %(c_name)s))")
		params.update(c_at=at, c_name=name)
	rows = frappe.db.sql(
		f"""select name, title, status, severity, started_at, resolved_at, acknowledged_at, site, bench, host,
			event_count, assigned_to, merged_into, deployment_at
		from `tabWolf Incident` where {' and '.join(where)}{allowed}
		order by started_at desc, name desc limit %(limit)s""",
		params,
		as_dict=True,
	)
	more = len(rows) > limit
	rows = rows[:limit]
	out = []
	for r in rows:
		out.append(
			{
				**{k: r[k] for k in ("name", "title", "status", "severity", "site", "bench", "host", "event_count", "merged_into")},
				"assigned_to": r.assigned_to,
				"assigned_name": get_fullname(r.assigned_to) if r.assigned_to else None,
				"started_at": window.to_utc(r.started_at, tz),
				"resolved_at": window.to_utc(r.resolved_at, tz),
				"acknowledged_at": window.to_utc(r.acknowledged_at, tz),
				"deployment_at": window.to_utc(r.deployment_at, tz),
			}
		)
	last = rows[-1] if rows and more else None
	return {
		"incidents": out,
		"paging": {"has_more": more, "next_cursor": timeline.encode_cursor((last.started_at, 0, last.name)) if last else None},
		"status": status,
		"scope": scope,
		"time_zone": tz,
	}
