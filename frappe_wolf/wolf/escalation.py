"""Escalate open, unacknowledged, unsilenced incidents once per open period."""

import frappe
from frappe.utils import get_datetime, now_datetime

from frappe_wolf.wolf import conditions, notify, silences


def run(now=None):
	now = now or now_datetime()
	rows = frappe.db.sql(
		"""select i.name, i.notified_at, i.alert_rule, i.host, i.bench, i.site, r.escalate_after_minutes
		from `tabWolf Incident` i
		inner join `tabWolf Alert Rule` r on r.name = i.alert_rule
		where i.status = 'Open'
			and coalesce(i.silenced, 0) = 0
			and i.escalated_at is null
			and i.notified_at is not null
			and coalesce(r.escalate_after_minutes, 0) > 0""",
		as_dict=True,
	)
	escalated = []
	for row in rows:
		if not conditions.escalation_due(get_datetime(row.notified_at), row.escalate_after_minutes, now):
			continue
		if silences.active_for(row, now):
			continue
		frappe.db.set_value("Wolf Incident", row.name, "escalated_at", now, update_modified=False)
		silences._comment(row.name, f"Not acknowledged after {row.escalate_after_minutes} min: escalated.")
		notify.enqueue(row.name, "escalated")
		escalated.append(row.name)
	return escalated
