"""The incident page of the Wolf UI: detail, timeline pages and actions (docs/ui-api.md).

Reuses the incident window and scope rules (wolf/incidents.py, wolf/events.py), the access
grants (wolf/access.py), the activity history (wolf/workflow.get_activity) and the incident's
own whitelisted methods for actions.
"""

import html
import re

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_fullname

from frappe_wolf.api.ui import READ_ROLES
from frappe_wolf.wolf import access, events, incidents, schema, timeline, window
from frappe_wolf.wolf.spa import MANAGER_ROLES
from frappe_wolf.wolf.workflow import ACTIVITY_LIMITS, activity_filters, get_activity

ACTIONS = ("acknowledge", "resolve", "assign_to_me")
EVENT_COLUMNS = """name, title, event_type, category, severity, source, `timestamp`, last_seen,
	`count`, host, bench, site, incident, payload"""


def _incident(name):
	frappe.only_for(READ_ROLES)
	doc = frappe.get_doc("Wolf Incident", name)
	doc.check_permission("read")
	return doc


def _allowed(doc, action):
	"""Whether `action` fits the incident's state now (the server's check, not only the button's)."""
	if doc.merged_into:
		return False
	if action == "acknowledge":
		return doc.status == "Open"
	if action == "resolve":
		return doc.status != "Resolved"
	if action == "assign_to_me":
		return doc.status != "Resolved" and doc.assigned_to != frappe.session.user
	return False


def _can(doc):
	manager = bool(set(frappe.get_roles()) & set(MANAGER_ROLES)) or frappe.session.user == "Administrator"
	write = manager and frappe.has_permission("Wolf Incident", "write", doc=doc)
	return {
		"comment": True,  # read permission is enough, like Desk's comment box
		**{action: write and _allowed(doc, action) for action in ACTIONS},
		"workflow": write,  # merge, split, postmortem (Desk)
	}


def _utc(value, tz):
	return window.to_utc(get_datetime(value), tz) if value else None


@frappe.whitelist()
def get_incident(incident):
	"""Everything the incident header needs, in one call."""
	doc = _incident(incident)
	tz = events.system_tz()
	start, end, padding = incidents.incident_window(doc)
	deployment = None
	if doc.deployment_at or doc.deployment_event:
		exists = bool(doc.deployment_event and frappe.db.exists("Wolf Event", doc.deployment_event))
		deployment = {
			"at": _utc(doc.deployment_at, tz),
			"event": doc.deployment_event if exists else None,
			"available": exists,  # the incident keeps the time when retention deleted the event
		}
	rule = None
	if doc.alert_rule:
		rule = {"name": doc.alert_rule, "readable": frappe.has_permission("Wolf Alert Rule", "read", doc=doc.alert_rule)}

	def who(user):
		return {"by": user, "by_name": get_fullname(user) if user else None}

	return {
		"name": doc.name,
		"title": doc.title,
		"status": doc.status,
		"severity": doc.severity,
		"scope": {"site": doc.site, "bench": doc.bench, "host": doc.host},
		"summary": doc.summary,
		"started_at": _utc(doc.started_at or doc.creation, tz),
		"last_event_at": _utc(doc.last_event_at, tz),
		"acknowledged": {"at": _utc(doc.acknowledged_at, tz), **who(doc.acknowledged_by)},
		"resolved": {"at": _utc(doc.resolved_at, tz), **who(doc.resolved_by)},
		"assigned_to": {"user": doc.assigned_to, "name": get_fullname(doc.assigned_to)} if doc.assigned_to else None,
		"alert_rule": rule,
		"deployment": deployment,
		"silenced": bool(doc.silenced),
		"notified_at": _utc(doc.notified_at, tz),
		"escalated_at": _utc(doc.escalated_at, tz),
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
	parts, params = [], {}
	if "evidence" in wanted:
		parts.append("incident = %(incident)s")
	# an incident without any scope has no context: "same scope" would mean every site
	if "context" in wanted and (doc.host or doc.bench or doc.site):
		clause, params = events.scope_clause(doc.host, doc.bench, doc.site)
		allowed, allowed_params = access.sql_condition("Wolf Event")
		params.update(allowed_params)
		parts.append(f"(coalesce(incident, '') != %(incident)s and `timestamp` between %(start)s and %(end)s and {clause}{allowed})")
	if not parts:
		return [], False
	keyset, keyset_params = timeline.keyset_sql(cursor_key, "event", "`timestamp`")
	params.update(keyset_params, incident=doc.name, start=start, end=end, limit=limit + 1)
	rows = frappe.db.sql(
		f"""select {EVENT_COLUMNS} from `tabWolf Event`
		where ({' or '.join(parts)}) {keyset}
		order by `timestamp` asc, name asc
		limit %(limit)s""",
		params,
		as_dict=True,
	)
	for r in rows:
		r.payload = incidents.parse_payload(r)
	return rows, len(rows) > limit


def _deployment_rows(doc, start, tz):
	"""The deployment before the incident when the context query cannot see it: deleted by
	retention (time only), or older than the timeline window (it is linked by its own setting)."""
	if not doc.deployment_at and not doc.deployment_event:
		return []
	if not doc.deployment_event or not frappe.db.exists("Wolf Event", doc.deployment_event):
		return [timeline.deployment_unavailable_row(doc.name, get_datetime(doc.deployment_at), tz)] if doc.deployment_at else []
	ev = frappe.db.sql(f"select {EVENT_COLUMNS} from `tabWolf Event` where name = %s", doc.deployment_event, as_dict=True)[0]
	if ev.incident == doc.name or get_datetime(ev.timestamp) >= start or not access.visible("Wolf Event", ev):
		return []  # in the window (or linked): the event query returns it
	ev.payload = incidents.parse_payload(ev)
	return [timeline.event_row(ev, doc.name, tz, doc.deployment_event)]


def _plain(content):
	"""Comment HTML (Wolf's escaped text, or Desk's editor paragraphs) as the text typed."""
	text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", content or "", flags=re.I)
	text = html.unescape(frappe.utils.strip_html(text))
	return re.sub(r"\n{3,}", "\n\n", text).strip()


def _activity(doc, tz):
	entries = get_activity(doc)
	for e in entries:
		e["timestamp"] = get_datetime(e["timestamp"]) if e.get("timestamp") else None
		if e.get("kind") in ("comment", "note"):
			e["plain"] = _plain(e.get("content"))
	rows = [timeline.activity_row(e, doc.name, tz) for e in entries if e.get("timestamp")]
	# get_activity keeps the newest Versions and Comments; say when older ones were left out
	truncated = {}
	for doctype, cap in ACTIVITY_LIMITS.items():
		total = frappe.db.count(doctype, activity_filters(doc.name)[doctype])
		if total > cap:
			truncated[doctype.lower()] = {"shown": cap, "total": total}
	return rows, len(entries), truncated


def _deliveries_after(doc, cursor_key, limit, tz):
	keyset, params = timeline.keyset_sql(cursor_key, "delivery", "`at`")
	params.update(incident=doc.name, limit=limit + 1)
	rows = frappe.db.sql(
		f"""select name, `at`, kind, digest, channel_type, target, action, ok, http_status, error
		from `tabWolf Delivery` where incident = %(incident)s and `at` is not null {keyset}
		order by `at` asc, name asc limit %(limit)s""",
		params,
		as_dict=True,
	)
	return [timeline.delivery_row(d, tz) for d in rows], len(rows) > limit


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
	start, end, padding = incidents.incident_window(doc)

	# each source gives its first limit + 1 rows after the cursor: the first `limit` of their
	# merge are the page (timeline.page)
	keyed, totals, truncated, more = [], {}, {}, False
	event_rows, more_events = _events_after(doc, start, end, wanted, cursor_key, limit)
	keyed += [timeline.event_row(r, doc.name, tz, doc.deployment_event) for r in event_rows]
	more |= more_events
	if "context" in wanted:
		keyed += _deployment_rows(doc, start, tz)
	if "activity" in wanted:
		rows, count, cut = _activity(doc, tz)
		keyed += rows
		totals["activity"] = count
		truncated.update({f"activity_{k}": v for k, v in cut.items()})
	if "delivery" in wanted:
		rows, more_deliveries = _deliveries_after(doc, cursor_key, limit, tz)
		keyed += rows
		more |= more_deliveries
		totals["delivery"] = frappe.db.count("Wolf Delivery", {"incident": doc.name})

	rows, paging = timeline.page(keyed, limit, cursor_key, more)
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
	"""Acknowledge, resolve or take an incident, through its own methods (and their checks).

	The incident's state is checked here too: a page left open may offer a button that no
	longer applies (resolved by the rule engine or a colleague meanwhile).
	"""
	if action not in ACTIONS:
		frappe.throw(_("Unknown action {0}").format(action), frappe.ValidationError)
	doc = _incident(incident)
	frappe.only_for(MANAGER_ROLES)
	doc.check_permission("write")
	if doc.merged_into:
		frappe.throw(_("This incident was merged into {0}.").format(doc.merged_into))
	if not _allowed(doc, action):
		frappe.throw(
			_("This incident is {0} now: refresh to see what changed.").format(_(doc.status).lower()),
			frappe.ValidationError,
		)
	getattr(doc, action)()
	return get_incident(doc.name)


@frappe.whitelist()
def list_incidents(status="unresolved", cursor=None, limit=50, site=None, bench=None, host=None):
	"""Incidents the caller may see, newest first, paged by (start, name).

	status: "unresolved" (Open and Acknowledged), "resolved" or "all". Merged incidents are
	resolved and listed under resolved/all with `merged_into`. An incident without started_at
	(an old hand-made one) is placed at its creation time.
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
		where.append("(coalesce(started_at, creation) < %(c_at)s or (coalesce(started_at, creation) = %(c_at)s and name < %(c_name)s))")
		params.update(c_at=at, c_name=name)
	rows = frappe.db.sql(
		f"""select name, title, status, severity, coalesce(started_at, creation) as started_at, resolved_at,
			acknowledged_at, site, bench, host, event_count, assigned_to, merged_into, deployment_at
		from `tabWolf Incident` where {' and '.join(where)}{allowed}
		order by coalesce(started_at, creation) desc, name desc limit %(limit)s""",
		params,
		as_dict=True,
	)
	more = len(rows) > limit
	rows = rows[:limit]
	out = [
		{
			**{k: r[k] for k in ("name", "title", "status", "severity", "site", "bench", "host", "event_count", "merged_into")},
			"assigned_to": r.assigned_to,
			"assigned_name": get_fullname(r.assigned_to) if r.assigned_to else None,
			"started_at": window.to_utc(r.started_at, tz),
			"resolved_at": window.to_utc(r.resolved_at, tz),
			"acknowledged_at": window.to_utc(r.acknowledged_at, tz),
			"deployment_at": window.to_utc(r.deployment_at, tz),
		}
		for r in rows
	]
	last = rows[-1] if rows and more else None
	return {
		"incidents": out,
		"paging": {"has_more": more, "next_cursor": timeline.encode_cursor((get_datetime(last.started_at), 0, last.name)) if last else None},
		"status": status,
		"scope": scope,
		"time_zone": tz,
	}
