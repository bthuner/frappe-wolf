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

	min_samples = cint(rule.min_samples) or 1
	resolve_threshold = rule.resolve_threshold if rule.get("use_resolve_threshold") else None
	fire_after = cint(rule.get("fire_after_evaluations")) or 1
	by_key = {conditions.group_key(rule.group_by, row): row for row in rows}
	open_by_key = {
		inc.group_key: inc.name
		for inc in frappe.get_all(
			"Wolf Incident",
			filters={"alert_rule": rule.name, "status": ("in", incidents.OPEN)},
			fields=["name", "group_key"],
			order_by="started_at asc",
		)
	}
	old_state = conditions.load_state(rule.get("evaluation_state"))
	state = {}

	# Every group with data, an open incident or a pending counter gets one step.
	for key in sorted(set(by_key) | set(open_by_key) | set(old_state)):
		row = by_key.get(key)
		breached = row is not None and conditions.is_breached(row, rule.operator, rule.threshold, min_samples)
		still = conditions.is_still_breached(row, rule.operator, rule.threshold, resolve_threshold, min_samples)
		new, action = conditions.hysteresis_step(
			old_state.get(key),
			breached,
			still,
			key in open_by_key,
			fire_after=fire_after,
			resolve_after=cint(rule.get("resolve_after_evaluations")) or 1,
			auto_resolve=bool(rule.auto_resolve),
		)
		if new:
			state[key] = new

		if action == "resolve":
			name = open_by_key[key]
			incidents.resolve(name)
			result["resolved"].append(name)
			if _flag(rule, "notify_on_resolve"):
				notify.enqueue(name, "resolved")
			continue
		if action not in ("fire", "update"):
			continue

		scope = conditions.incident_scope(rule.group_by, row)
		scope["label_key"] = row.get("label_key")
		if rule.require_recent_deployment and not incidents.latest_deployment(
			scope["host"], scope["bench"], scope["site"], minutes=cint(rule.deployment_window_minutes) or 30
		):
			if action == "fire" and fire_after > 1:
				# Keep the streak so the next breach after a deployment fires at once.
				state[key] = [fire_after - 1, 0]
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

	if state != old_state:
		frappe.db.set_value(
			"Wolf Alert Rule", rule.name, "evaluation_state", conditions.dump_state(state) if state else None, update_modified=False
		)
	if result["fired"]:
		frappe.db.set_value("Wolf Alert Rule", rule.name, "last_fired", now, update_modified=False)
	return result


def _flag(doc, field, default=1):
	"""Check fields added after a rule was created read as None on in-memory docs."""
	value = doc.get(field)
	return default if value is None else cint(value)


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


def _until():
	# Upper bound for rule windows: rows stamped in the future (older data, clock skew) are
	# ignored instead of keeping every window breached until real time catches up.
	return add_to_date(now_datetime(), minutes=1)


def query_metric(rule, since):
	agg = rule.aggregation or "avg"
	if agg not in conditions.AGGREGATIONS:
		frappe.throw(f"Unknown aggregation {agg}")
	params = {"metric": rule.metric_name, "since": since, "until": _until()}
	where = [
		"metric_name = %(metric)s",
		"`timestamp` between %(since)s and %(until)s",
		*_scope_filters(rule, params),
	]
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
	params.update(event_type=rule.event_type, since=since, until=_until())
	where = [
		"event_type = %(event_type)s",
		"last_seen between %(since)s and %(until)s",
		*_scope_filters(rule, params),
	]
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
