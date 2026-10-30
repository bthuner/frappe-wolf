"""Events and error groups for the Wolf UI explorers (docs/ui-api.md).

Wolf Events are deduplicated: one record holds its first time, last time and a count of the
repeats it absorbed. Lists here are lists of records, and an error group sums its records'
counts. Neither says how many times something happened inside a window when a record spans
the window's edge, and the responses say so in `basis`.
"""

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from frappe_wolf.api.scope import _scope, _window
from frappe_wolf.api.ui import READ_ROLES
from frappe_wolf.wolf import access, events, incidents, schema, timeline, window

EVENT_FIELDS = """name, title, event_type, category, severity, source, `timestamp`, last_seen, `count`,
	host, bench, site, incident, fingerprint"""


def _filters(win, scope, event_type=None, severity=None, category=None, q=None, extra=()):
	"""WHERE parts and params shared by the event list and error groups."""
	allowed, params = access.sql_condition("Wolf Event")
	# records active in the window: first seen before its end, last seen after its start
	where = ["coalesce(last_seen, `timestamp`) >= %(since)s", "`timestamp` <= %(until)s", *extra]
	params.update(scope, since=win.since, until=win.until)
	where += [f"`{k}` = %({k})s" for k in scope]
	for field, value, allowed_values in (
		("event_type", event_type, schema.EVENT_TYPES),
		("severity", severity, schema.SEVERITIES),
	):
		if value:
			if value not in allowed_values:
				frappe.throw(_("Unknown {0} {1}").format(field, value), frappe.ValidationError)
			where.append(f"`{field}` = %({field})s")
			params[field] = value
	if category:
		where.append("category = %(category)s")
		params["category"] = str(category)[:140]
	pattern = window.like_pattern(q)
	if pattern:
		where.append("title like %(q)s")
		params["q"] = pattern
	return " and ".join(where) + allowed, params


def _event_row(r, tz):
	return {
		"name": r.name, "title": r.title, "event_type": r.event_type, "category": r.category,
		"severity": r.severity, "source": r.source, "count": r.count or 1,
		"first_seen": window.to_utc(r.timestamp, tz), "last_seen": window.to_utc(r.last_seen or r.timestamp, tz),
		"site": r.site, "bench": r.bench, "host": r.host, "incident": r.incident, "fingerprint": r.fingerprint,
	}  # fmt: skip


@frappe.whitelist()
def list_events(range=None, since=None, until=None, site=None, bench=None, host=None,
				event_type=None, severity=None, category=None, q=None, cursor=None, limit=50):  # fmt: skip
	"""Event records active in the window, newest first, paged by (first seen, name)."""
	frappe.only_for(READ_ROLES)
	win, scope = _window(range, since, until), _scope(site, bench, host)
	tz = events.system_tz()
	limit = min(max(cint(limit) or 50, 1), 200)
	extra = []
	cursor_params = {}
	if cursor:
		try:
			at, _rank, name = timeline.decode_cursor(cursor)
		except ValueError as e:
			frappe.throw(str(e), frappe.ValidationError)
		extra.append("(`timestamp` < %(c_at)s or (`timestamp` = %(c_at)s and name < %(c_name)s))")
		cursor_params = {"c_at": at, "c_name": name}
	where, params = _filters(win, scope, event_type, severity, category, q, extra)
	rows = frappe.db.sql(
		f"select {EVENT_FIELDS} from `tabWolf Event` where {where} order by `timestamp` desc, name desc limit %(limit)s",
		{**params, **cursor_params, "limit": limit + 1},
		as_dict=True,
	)
	more = len(rows) > limit
	rows = rows[:limit]
	return {
		"events": [_event_row(r, tz) for r in rows],
		"paging": {
			"has_more": more,
			"next_cursor": timeline.encode_cursor((get_datetime(rows[-1].timestamp), 0, rows[-1].name)) if more else None,
		},
		"window": window.describe(win),
		"scope": scope,
		"basis": "event records active in the window (first seen before its end, last seen after its start); "
		"count is every repeat a record absorbed, inside the window or not",
		"time_zone": tz,
	}


@frappe.whitelist()
def get_event(name):
	"""One event record with its payload, for an expanded row (payload shown as text)."""
	frappe.only_for(READ_ROLES)
	doc = frappe.get_doc("Wolf Event", name)
	doc.check_permission("read")
	tz = events.system_tz()
	row = frappe._dict(doc.as_dict())
	return {**_event_row(row, tz), "payload": incidents.parse_payload(row)}


@frappe.whitelist()
def list_error_groups(range=None, since=None, until=None, site=None, bench=None, host=None, q=None, cursor=None, limit=50):
	"""Error records grouped by fingerprint, for groups active in the window, latest first."""
	frappe.only_for(READ_ROLES)
	win, scope = _window(range, since, until), _scope(site, bench, host)
	tz = events.system_tz()
	limit = min(max(cint(limit) or 50, 1), 100)
	where, params = _filters(win, scope, "error", q=q, extra=["coalesce(fingerprint, '') != ''"])
	having = ""
	if cursor:
		try:
			at, _rank, fp = timeline.decode_cursor(cursor)
		except ValueError as e:
			frappe.throw(str(e), frappe.ValidationError)
		having = "having max(coalesce(last_seen, `timestamp`)) < %(c_at)s or (max(coalesce(last_seen, `timestamp`)) = %(c_at)s and fingerprint < %(c_fp)s)"
		params.update(c_at=at, c_fp=fp)
	groups = frappe.db.sql(
		f"""select fingerprint, count(*) as records, sum(`count`) as occurrences, min(`timestamp`) as first_seen,
			max(coalesce(last_seen, `timestamp`)) as last_seen, count(distinct site) as sites,
			count(distinct case when coalesce(site, '') = '' then host end) as hosts_without_site,
			count(distinct incident) as incidents
		from `tabWolf Event` where {where}
		group by fingerprint {having}
		order by max(coalesce(last_seen, `timestamp`)) desc, fingerprint desc
		limit %(limit)s""",
		{**params, "limit": limit + 1},
		as_dict=True,
	)
	more = len(groups) > limit
	groups = groups[:limit]
	latest = _latest_records([g.fingerprint for g in groups], where, params)
	out = []
	for g in groups:
		rep = latest.get(g.fingerprint) or {}
		out.append({
			"fingerprint": g.fingerprint, "title": rep.get("title"), "severity": rep.get("severity"),
			"category": rep.get("category"), "source": rep.get("source"),
			"records": cint(g.records), "occurrences": cint(g.occurrences),
			"first_seen": window.to_utc(get_datetime(g.first_seen), tz), "last_seen": window.to_utc(get_datetime(g.last_seen), tz),
			"sites": cint(g.sites), "host_wide": cint(g.hosts_without_site) > 0, "incidents": cint(g.incidents),
			"latest": {k: rep.get(k) for k in ("name", "site", "bench", "host")},
		})  # fmt: skip
	last = groups[-1] if more else None
	return {
		"groups": out,
		"paging": {
			"has_more": more,
			"next_cursor": timeline.encode_cursor((get_datetime(last.last_seen), 0, last.fingerprint)) if last else None,
		},
		"window": window.describe(win),
		"scope": scope,
		"basis": "error records active in the window, grouped by fingerprint; occurrences add up each record's "
		"reported count, so a record spanning the window's edge counts all its repeats",
		"time_zone": tz,
	}


def _latest_records(fingerprints, where, params):
	"""The newest visible record of each group: its title stands for the group."""
	if not fingerprints:
		return {}
	rows = frappe.db.sql(
		f"""select name, fingerprint, title, severity, category, source, site, bench, host,
			coalesce(last_seen, `timestamp`) as seen
		from `tabWolf Event` where {where} and fingerprint in %(fps)s""",
		{**params, "fps": tuple(fingerprints)},
		as_dict=True,
	)
	best = {}
	for r in rows:
		if r.fingerprint not in best or (r.seen, r.name) > (best[r.fingerprint].seen, best[r.fingerprint].name):
			best[r.fingerprint] = r
	return best


@frappe.whitelist()
def get_error_group(fingerprint, limit=50):
	"""One error group: where it happened, the incidents it is in, its newest record in full
	(the representative traceback) and its records, newest first. Visible records only."""
	frappe.only_for(READ_ROLES)
	tz = events.system_tz()
	limit = min(max(cint(limit) or 50, 1), 200)
	allowed, params = access.sql_condition("Wolf Event")
	params["fp"] = str(fingerprint)[:140]
	base = f"fingerprint = %(fp)s and event_type = 'error'{allowed}"
	by_scope = frappe.db.sql(
		f"""select site, bench, host, count(*) as records, sum(`count`) as occurrences, min(`timestamp`) as first_seen,
			max(coalesce(last_seen, `timestamp`)) as last_seen
		from `tabWolf Event` where {base} group by site, bench, host
		order by max(coalesce(last_seen, `timestamp`)) desc""",
		params,
		as_dict=True,
	)
	if not by_scope:
		frappe.throw(_("No error group {0} that you can see.").format(fingerprint), frappe.DoesNotExistError)
	rows = frappe.db.sql(
		f"select {EVENT_FIELDS}, payload from `tabWolf Event` where {base} order by `timestamp` desc, name desc limit %(limit)s",
		{**params, "limit": limit + 1},
		as_dict=True,
	)
	total = sum(cint(s.records) for s in by_scope)
	newest = rows[0]
	incident_names = sorted({r.incident for r in rows if r.incident} | set(
		frappe.db.sql_list(f"select distinct incident from `tabWolf Event` where {base} and coalesce(incident, '') != ''", params)
	))  # fmt: skip
	# incidents the caller may read; the others are counted, not named
	visible = frappe.get_list(
		"Wolf Incident", filters={"name": ("in", incident_names or [""])}, fields=["name", "title", "status", "severity"], limit_page_length=0
	)
	return {
		"fingerprint": params["fp"],
		"title": newest.title,
		"severity": newest.severity,
		"category": newest.category,
		"records": total,
		"occurrences": sum(cint(s.occurrences) for s in by_scope),
		"first_seen": window.to_utc(get_datetime(min(s.first_seen for s in by_scope)), tz),
		"last_seen": window.to_utc(get_datetime(max(s.last_seen for s in by_scope)), tz),
		"scopes": [
			{"site": s.site, "bench": s.bench, "host": s.host, "records": cint(s.records), "occurrences": cint(s.occurrences),
			 "first_seen": window.to_utc(get_datetime(s.first_seen), tz), "last_seen": window.to_utc(get_datetime(s.last_seen), tz)}  # fmt: skip
			for s in by_scope
		],
		"incidents": [{"name": i.name, "title": i.title, "status": i.status, "severity": i.severity} for i in visible],
		"incidents_not_visible": len(incident_names) - len(visible),
		"representative": {**_event_row(newest, tz), "payload": incidents.parse_payload(newest)},
		"recent_records": [_event_row(r, tz) for r in rows[:limit]],
		"more_records": len(rows) > limit,
		"basis": "every visible record of the group, whatever its age; occurrences add up reported counts",
		"time_zone": tz,
		"checked_at": window.to_utc(now_datetime(), tz),
	}
