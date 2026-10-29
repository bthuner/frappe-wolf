import frappe
from frappe.utils import add_days, add_to_date, cint, now_datetime

from frappe_wolf.wolf import events, rules


def evaluate_rules():
	rules.evaluate_all()


def check_agents():
	"""Emit one `agent_silent` event per agent that stopped pushing."""
	minutes = cint(frappe.db.get_single_value("Wolf Settings", "agent_silence_minutes")) or 5
	silent = frappe.get_all(
		"Wolf Agent",
		filters={
			"status": "Active",
			"silence_reported": 0,
			"last_seen": ("<", add_to_date(now_datetime(), minutes=-minutes)),
		},
		fields=["name", "hostname", "last_seen"],
	)
	for agent in silent:
		events.emit(
			"host_metric_alert",
			f"Wolf agent on {agent.hostname} silent since {agent.last_seen}",
			severity="warning",
			category="agent_silent",
			source="wolf",
			payload={"agent": agent.name, "last_seen": str(agent.last_seen)},
			host=agent.hostname,
			bench=None,
			site=None,
		)
		frappe.db.set_value("Wolf Agent", agent.name, "silence_reported", 1, update_modified=False)
	frappe.db.commit()


def purge_old_data():
	settings = frappe.get_single("Wolf Settings")
	now = now_datetime()
	metric_days = cint(settings.metric_retention_days) or 14
	event_days = cint(settings.event_retention_days) or 90
	frappe.db.delete("Wolf Metric Sample", {"timestamp": ("<", add_days(now, -metric_days))})
	# Keep events still attached to an unresolved incident.
	frappe.db.sql(
		"""delete e from `tabWolf Event` e
		left join `tabWolf Incident` i on i.name = e.incident
		where e.`timestamp` < %(before)s and (i.name is null or i.status = 'Resolved')""",
		{"before": add_days(now, -event_days)},
	)
	frappe.db.commit()
