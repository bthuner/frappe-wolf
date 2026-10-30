"""Exact counts and the site/bench/host inventory for the Wolf UI (docs/ui-api.md).

Counts are computed in SQL with the caller's access rules (wolf/access.py), never from a
limited list, so a KPI is right however many records sit behind it. Nothing is cached: every
answer is the caller's own.
"""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from frappe_wolf.api.ui import READ_ROLES, agent_cutoff
from frappe_wolf.wolf import access, events, inventory, schema, window

UNRESOLVED = ("Open", "Acknowledged")
INVENTORY_DAYS = 7


def _window(range=None, since=None, until=None):
	try:
		return window.parse(now_datetime(), events.system_tz(), range=range, since=since, until=until)
	except schema.PayloadError as e:
		frappe.throw(str(e), frappe.ValidationError)


def _scope(site=None, bench=None, host=None):
	try:
		return window.scope_filters(site=site, bench=bench, host=host)
	except schema.PayloadError as e:
		frappe.throw(str(e), frappe.ValidationError)


def _where(doctype, scope, *conditions):
	"""(" and ..." SQL, params) for the scope arguments and the caller's access rules."""
	allowed, params = access.sql_condition(doctype)
	sql = "".join(f" and `{k}` = %({k})s" for k in scope) + allowed
	return "".join(f" and {c}" for c in conditions) + sql, {**scope, **params}


def _incident_counts(scope, win):
	where, params = _where("Wolf Incident", scope, "coalesce(merged_into, '') = ''")
	params.update(unresolved=UNRESOLVED, since=win.since, until=win.until)
	rows = frappe.db.sql(
		f"""select status, severity, count(*) as n from `tabWolf Incident`
		where status in %(unresolved)s {where} group by status, severity""",
		params,
		as_dict=True,
	)
	by_status, by_severity = dict.fromkeys(UNRESOLVED, 0), {"info": 0, "warning": 0, "critical": 0}
	for r in rows:
		by_status[r.status] = by_status.get(r.status, 0) + cint(r.n)
		by_severity[r.severity or "info"] = by_severity.get(r.severity or "info", 0) + cint(r.n)
	sites, host_wide = frappe.db.sql(
		f"""select count(distinct case when coalesce(site, '') != '' then site end),
			sum(case when coalesce(site, '') = '' then 1 else 0 end)
		from `tabWolf Incident` where status in %(unresolved)s {where}""",
		params,
	)[0]
	opened, resolved = frappe.db.sql(
		f"""select sum(case when started_at >= %(since)s and started_at <= %(until)s then 1 else 0 end),
			sum(case when resolved_at >= %(since)s and resolved_at <= %(until)s then 1 else 0 end)
		from `tabWolf Incident`
		where (started_at between %(since)s and %(until)s or resolved_at between %(since)s and %(until)s) {where}""",
		params,
	)[0]
	return {
		"unresolved": sum(by_status.values()),
		"unacknowledged": by_status["Open"],
		"critical_unresolved": by_severity["critical"],
		"by_status": by_status,
		"by_severity": by_severity,
		"sites_with_unresolved": cint(sites),
		"unresolved_without_site": cint(host_wide),  # host- or bench-wide incidents
		"opened_in_window": cint(opened),
		"resolved_in_window": cint(resolved),
		"basis": "unresolved now; opened/resolved by started_at/resolved_at in the window; merged incidents excluded",
	}


def _event_counts(scope, win):
	where, params = _where("Wolf Event", scope)
	params.update(since=win.since, until=win.until)
	rows = frappe.db.sql(
		f"""select event_type, severity, count(*) as records, sum(`count`) as occurrences
		from `tabWolf Event` where `timestamp` >= %(since)s and `timestamp` <= %(until)s {where}
		group by event_type, severity""",
		params,
		as_dict=True,
	)
	by_type = {}
	for r in rows:
		t = by_type.setdefault(r.event_type, {"records": 0, "occurrences": 0, "by_severity": {}})
		t["records"] += cint(r.records)
		t["occurrences"] += cint(r.occurrences)
		sev = t["by_severity"].setdefault(r.severity or "info", {"records": 0, "occurrences": 0})
		sev["records"] += cint(r.records)
		sev["occurrences"] += cint(r.occurrences)
	return {
		"records": sum(t["records"] for t in by_type.values()),
		"occurrences": sum(t["occurrences"] for t in by_type.values()),
		"by_type": by_type,
		# a deduplicated record counts every repeat it absorbed, including repeats after the window
		"basis": "records first seen in the window; occurrences are their reported counts",
	}


def _agent_counts(scope):
	silence, cutoff = agent_cutoff()
	host = scope.get("host")
	out = {kind: {"fresh": 0, "stale": 0, "never_seen": 0, "disabled": 0} for kind in ("agent", "satellite")}
	# Wolf Agent rows the caller may read (agent_query); with a host scope, an agent is that
	# host and a satellite counts when one of its benches runs there
	rows = frappe.get_list("Wolf Agent", fields=["hostname", "kind", "status", "last_seen", "benches"], limit_page_length=0)
	for a in rows:
		kind = a.kind or "agent"
		if host and (a.hostname != host if kind == "agent" else host not in inventory.satellite_hosts(a)):
			continue
		counts = out.setdefault(kind, {"fresh": 0, "stale": 0, "never_seen": 0, "disabled": 0})
		counts[inventory.agent_state(a.status, a.last_seen, cutoff)] += 1
	return {**out, "silence_minutes": silence, "basis": "agents are hosts: site and bench filters do not apply"}


@frappe.whitelist()
def get_counts(range=None, since=None, until=None, site=None, bench=None, host=None):
	"""Exact counts for the overview and entity dashboards, filtered by the caller's access."""
	frappe.only_for(READ_ROLES)
	win, scope = _window(range, since, until), _scope(site, bench, host)
	return {
		"window": window.describe(win),
		"scope": scope,
		"restricted": access.grants() is not None,
		"incidents": _incident_counts(scope, win),
		"events": _event_counts(scope, win),
		"agents": _agent_counts(scope),
	}


def _observed(doctype, ts_col, since, extra=""):
	"""(host, bench, site, last_seen) seen in `doctype` since `since` (None: no time bound)."""
	allowed, params = access.sql_condition(doctype)
	bound = f"{ts_col} >= %(since)s" if since else "1=1"
	return frappe.db.sql(
		f"""select host, bench, site, max({ts_col}) as last_seen from `tab{doctype}`
		where {bound} {extra}{allowed} group by host, bench, site""",
		{**params, "since": since},
		as_dict=True,
	)


@frappe.whitelist()
def get_inventory(days=INVENTORY_DAYS):
	"""Hosts > benches > sites the caller may see, with where each one was learnt from."""
	frappe.only_for(READ_ROLES)
	days = min(max(cint(days) or INVENTORY_DAYS, 1), 90)
	since = add_to_date(now_datetime(), days=-days)
	tz = events.system_tz()
	# every registration, not only the Wolf Agent rows the caller may read: a host agent's row
	# names a host, yet its benches list the sites a site-scoped user is granted. Each triple is
	# checked against the grants below.
	agents = frappe.get_all("Wolf Agent", fields=["hostname", "kind", "benches", "last_seen"])
	triples = inventory.agent_triples(agents)
	# a deduplicated event keeps its first `timestamp`; `last_seen` is when it last happened
	triples += inventory.observed_triples(_observed("Wolf Event", "coalesce(last_seen, `timestamp`)", since), "events")
	# hourly rollups cover the window cheaply; minute rollups the hours not closed yet
	triples += inventory.observed_triples(
		_observed("Wolf Metric Rollup", "bucket_start", since, "and resolution = 3600"), "metrics"
	)
	triples += inventory.observed_triples(
		_observed("Wolf Metric Rollup", "bucket_start", add_to_date(now_datetime(), hours=-2), "and resolution = 60"),
		"metrics",
	)
	# unresolved incidents whatever their age, as get_counts counts them
	triples += inventory.observed_triples(
		_observed("Wolf Incident", "coalesce(last_event_at, started_at)", None, "and status in ('Open', 'Acknowledged')"),
		"incidents",
	)
	# Wolf Agent rows went through permission_query_conditions; registered benches still name
	# sites the user may not be granted, so every triple is checked against the grants
	tree = inventory.build(inventory.visible(triples, access.grants()))

	def utc(node):
		node["last_seen"] = window.to_utc(node["last_seen"], tz)
		for key in ("benches", "sites"):
			for child in node.get(key) or []:
				utc(child)
		return node

	tree["hosts"] = [utc(h) for h in tree["hosts"]]
	tree["unplaced_sites"] = [utc(s) for s in tree["unplaced_sites"]]
	return {**tree, "days": days, "restricted": access.grants() is not None}
