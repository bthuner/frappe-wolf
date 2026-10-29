# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

"""MTTA / MTTR per site, bench, host, rule or severity over a date range.

The query only filters; durations and percentiles are computed in Python (wolf/stats.py) so
the report behaves the same on MariaDB and Postgres. Incidents merged into another one are
left out: they would count the same outage twice.
"""

import frappe
from frappe import _
from frappe.utils import add_days, add_months, getdate, nowdate

from frappe_wolf.wolf import stats

FIELDS = ["name", "status", "severity", "started_at", "acknowledged_at", "resolved_at", "site", "bench", "host", "alert_rule"]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	group_by = filters.get("group_by") or "site"
	if group_by not in stats.GROUP_FIELDS:
		frappe.throw(_("Cannot group by {0}").format(group_by))
	rows = get_rows(filters)
	data = stats.group_metrics(rows, group_by)
	total = stats.overall(rows)
	if total and len(data) > 1:
		data.append({**total, "bold": 1})
	chart = None
	if data:
		groups = [d for d in data if d.get("group") != "Total"][:20]
		chart = {
			"data": {
				"labels": [str(d["group"]) for d in groups],
				"datasets": [
					{"name": _("MTTA p50 (min)"), "values": [d["mtta_p50"] or 0 for d in groups]},
					{"name": _("MTTR p50 (min)"), "values": [d["mttr_p50"] or 0 for d in groups]},
				],
			},
			"type": "bar",
		}
	return columns(group_by), data, None, chart


def get_rows(filters):
	to_date = getdate(filters.get("to_date") or nowdate())
	from_date = getdate(filters.get("from_date") or add_months(to_date, -1))
	conditions = {
		"started_at": ("between", [f"{from_date} 00:00:00", f"{add_days(to_date, 1)} 00:00:00"]),
		"merged_into": ("is", "not set"),
	}
	for field in ("site", "bench", "host", "alert_rule", "severity"):
		if filters.get(field):
			conditions[field] = filters.get(field)
	return frappe.get_list(
		"Wolf Incident", filters=conditions, fields=FIELDS, order_by="started_at asc", limit_page_length=0
	)


def columns(group_by):
	label = {"site": _("Site"), "bench": _("Bench"), "host": _("Host"), "alert_rule": _("Alert Rule"), "severity": _("Severity")}
	col = {"fieldname": "group", "label": label[group_by], "width": 220}
	if group_by == "alert_rule":
		col.update(fieldtype="Link", options="Wolf Alert Rule")
	else:
		col["fieldtype"] = "Data"
	minutes = {"fieldtype": "Float", "precision": 1, "width": 110}
	return [
		col,
		{"fieldname": "incidents", "label": _("Incidents"), "fieldtype": "Int", "width": 90},
		{"fieldname": "open", "label": _("Still Open"), "fieldtype": "Int", "width": 90},
		{"fieldname": "acknowledged", "label": _("Acknowledged"), "fieldtype": "Int", "width": 110},
		{"fieldname": "resolved", "label": _("Resolved"), "fieldtype": "Int", "width": 90},
		{"fieldname": "mtta", "label": _("MTTA (min)"), **minutes},
		{"fieldname": "mtta_p50", "label": _("MTTA p50"), **minutes},
		{"fieldname": "mtta_p90", "label": _("MTTA p90"), **minutes},
		{"fieldname": "mttr", "label": _("MTTR (min)"), **minutes},
		{"fieldname": "mttr_p50", "label": _("MTTR p50"), **minutes},
		{"fieldname": "mttr_p90", "label": _("MTTR p90"), **minutes},
	]
