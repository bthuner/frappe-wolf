"""Alert rule evaluation, run every minute by the scheduler."""

import frappe
from frappe.utils import add_to_date, cint, flt, now_datetime

from frappe_wolf.wolf import conditions, events, incidents, notify

SCOPE_FIELDS = ("host", "bench", "site")


def evaluate_all():
	for name in frappe.get_all("Wolf Alert Rule", filters={"enabled": 1}, pluck="name"):
		rule = frappe.get_doc("Wolf Alert Rule", name)
		try:
			evaluate(rule)
			error = None
		except Exception:
			frappe.db.rollback()
			error = frappe.get_traceback()[-1000:]
			frappe.log_error(title=f"[Wolf] Alert rule {name} failed")
		frappe.db.set_value(
			"Wolf Alert Rule",
			name,
			{"last_evaluated": now_datetime(), "last_error": error},
			update_modified=False,
		)
		frappe.db.commit()


def evaluate(rule, now=None):
	"""Evaluate one rule. Returns {"fired": [...], "resolved": [...]} incident names."""
	now = now or now_datetime()
	result = {"fired": [], "updated": [], "resolved": []}
	if not conditions.in_active_window(now.time(), rule.active_from, rule.active_to):
		return result

	since = add_to_date(now, minutes=-max(cint(rule.window_minutes), 1))
	rows = query_metric(rule, since) if rule.signal_type == "metric" else query_events(rule, since)

	breached = {}
	for row in rows:
		if conditions.is_breached(row, rule.operator, rule.threshold, cint(rule.min_samples) or 1):
			breached[conditions.group_key(rule.group_by, row)] = row

	for key, row in breached.items():
		scope = {f: row.get(f) or row.get(f"{f}_hint") for f in SCOPE_FIELDS}
		scope["label_key"] = row.get("label_key")
		if rule.require_recent_deployment and not incidents.latest_deployment(
			scope["host"], scope["bench"], scope["site"], minutes=cint(rule.deployment_window_minutes) or 30
		):
			continue

		summary = (
			f"{describe_signal(rule)} = {conditions.format_value(row['value'])} "
			f"({rule.operator} {conditions.format_value(rule.threshold)}) over {rule.window_minutes} min"
		)
		name, kind = incidents.open_incident(rule, key, scope, summary, started_at=row.get("first_at"))
		if rule.signal_type == "event":
			incidents.link_events(name, matching_event_names(rule, since, row))
		elif kind:
			record_metric_alert(rule, name, scope, summary)
		if kind:
			result["fired"].append(name)
			notify.enqueue(name, kind)
		else:
			result["updated"].append(name)

	if rule.auto_resolve:
		for inc in frappe.get_all(
			"Wolf Incident",
			filters={"alert_rule": rule.name, "status": ("in", incidents.OPEN)},
			fields=["name", "group_key"],
		):
			if inc.group_key not in breached:
				incidents.resolve(inc.name)
				result["resolved"].append(inc.name)
				notify.enqueue(inc.name, "resolved")

	if result["fired"]:
		frappe.db.set_value("Wolf Alert Rule", rule.name, "last_fired", now, update_modified=False)
	return result


def describe_signal(rule):
	if rule.signal_type == "metric":
		return f"{rule.aggregation}({rule.metric_name})"
	return f"count({rule.event_type}{'/' + rule.event_category if rule.event_category else ''})"


def _scope_filters(rule, params, prefix=""):
	conds = []
	for field in SCOPE_FIELDS:
		value = rule.get(f"filter_{field}")
		if value:
			conds.append(f"{prefix}`{field}` = %(filter_{field})s")
			params[f"filter_{field}"] = value
	return conds


def _select_group(group_by):
	cols = conditions.group_columns(group_by)
	select = [f"`{c}`" for c in cols]
	# Scope hints let a site-grouped incident still know its bench and host.
	select += [f"max(`{f}`) as {f}_hint" for f in SCOPE_FIELDS if f not in cols]
	group = f"group by {', '.join(f'`{c}`' for c in cols)}" if cols else ""
	return ", ".join(select), group


def query_metric(rule, since):
	agg = rule.aggregation or "avg"
	if agg not in conditions.AGGREGATIONS:
		frappe.throw(f"Unknown aggregation {agg}")
	params = {"metric": rule.metric_name, "since": since}
	where = ["metric_name = %(metric)s", "`timestamp` >= %(since)s", *_scope_filters(rule, params)]
	select, group = _select_group(rule.group_by)

	if agg == "last":
		cols = conditions.group_columns(rule.group_by)
		raw = frappe.db.sql(
			f"""select host, bench, site, label_key, `value`, `timestamp` from `tabWolf Metric Sample`
			where {' and '.join(where)} order by `timestamp` desc""",
			params,
			as_dict=True,
		)
		latest = {}
		for r in raw:
			key = tuple(r.get(c) for c in cols)
			if key not in latest:
				latest[key] = dict(r, samples=0, first_at=r.timestamp, **{f"{f}_hint": r[f] for f in SCOPE_FIELDS})
			latest[key]["samples"] += 1
			latest[key]["first_at"] = r.timestamp
		return list(latest.values())

	expr = "count(*)" if agg == "count" else f"{agg}(`value`)"
	return frappe.db.sql(
		f"""select {select}, {expr} as value, count(*) as samples, min(`timestamp`) as first_at
		from `tabWolf Metric Sample`
		where {' and '.join(where)} {group}""",
		params,
		as_dict=True,
	)


def _event_where(rule, since, params):
	params.update(event_type=rule.event_type, since=since)
	where = ["event_type = %(event_type)s", "last_seen >= %(since)s", *_scope_filters(rule, params)]
	if rule.event_category:
		where.append("category = %(category)s")
		params["category"] = rule.event_category
	if rule.min_severity:
		where.append("severity in %(severities)s")
		params["severities"] = tuple(conditions.severities_at_least(rule.min_severity))
	return where


def query_events(rule, since):
	params = {}
	where = _event_where(rule, since, params)
	select, group = _select_group(rule.group_by)
	rows = frappe.db.sql(
		f"""select {select}, sum(`count`) as value, count(*) as samples, min(`timestamp`) as first_at
		from `tabWolf Event`
		where {' and '.join(where)} {group}""",
		params,
		as_dict=True,
	)
	# Without group by, an empty table still yields one row of NULLs.
	return [r for r in rows if r.samples]


def matching_event_names(rule, since, row):
	params = {}
	where = _event_where(rule, since, params)
	for col in conditions.group_columns(rule.group_by):
		if row.get(col) is None:
			where.append(f"`{col}` is null")
		else:
			where.append(f"`{col}` = %(grp_{col})s")
			params[f"grp_{col}"] = row[col]
	return frappe.db.sql_list(
		f"select name from `tabWolf Event` where {' and '.join(where)} limit 500",
		params,
	)


def record_metric_alert(rule, incident, scope, summary):
	name = events.emit(
		"host_metric_alert",
		f"{rule.rule_name}: {summary}",
		severity=rule.severity,
		category=rule.metric_name,
		source="wolf",
		payload={"rule": rule.name, "summary": summary, "threshold": flt(rule.threshold)},
		host=scope.get("host"),
		bench=scope.get("bench"),
		site=scope.get("site"),
	)
	incidents.link_events(incident, [name])
