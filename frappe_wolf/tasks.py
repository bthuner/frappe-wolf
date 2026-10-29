import json

import frappe
from frappe.utils import add_days, add_to_date, cint, now_datetime

from frappe_wolf import satellite
from frappe_wolf.wolf import digest, escalation, events, rules, silences


def evaluate_rules():
	if satellite.is_satellite():
		return
	rules.evaluate_all()


def forward_outbox():
	satellite.flush()


def check_incidents():
	"""Notify incidents whose maintenance window ended, then escalate unacknowledged ones."""
	if satellite.is_satellite():
		return
	silences.release_expired()
	escalation.run()
	frappe.db.commit()


def send_digests():
	if satellite.is_satellite():
		return
	digest.send_all()


def check_agents():
	"""Emit one `agent_silent` event per agent (or satellite site) that stopped pushing."""
	if satellite.is_satellite():
		return
	minutes = cint(frappe.db.get_single_value("Wolf Settings", "agent_silence_minutes")) or 5
	silent = frappe.get_all(
		"Wolf Agent",
		filters={
			"status": "Active",
			"silence_reported": 0,
			"last_seen": ("<", add_to_date(now_datetime(), minutes=-minutes)),
		},
		fields=["name", "hostname", "last_seen", "kind", "benches"],
	)
	for agent in silent:
		if agent.kind == "satellite":
			# a satellite pushes a heartbeat every minute: silence means its scheduler stopped
			title = f"Wolf satellite {agent.hostname} silent since {agent.last_seen}"
			scope = {**satellite_scope(agent), "site": agent.hostname}
		else:
			title = f"Wolf agent on {agent.hostname} silent since {agent.last_seen}"
			scope = {"host": agent.hostname, "bench": None, "site": None}
		events.emit(
			"host_metric_alert",
			title,
			severity="warning",
			category="agent_silent",
			source="wolf",
			payload={"agent": agent.name, "kind": agent.kind or "agent", "last_seen": str(agent.last_seen)},
			**scope,
		)
		frappe.db.set_value("Wolf Agent", agent.name, "silence_reported", 1, update_modified=False)
	frappe.db.commit()


def satellite_scope(agent):
	"""Host and bench a satellite reported at registration."""
	try:
		bench = (json.loads(agent.benches or "[]") or [{}])[0]
	except (ValueError, TypeError, KeyError):
		bench = {}
	if not isinstance(bench, dict):
		bench = {}
	return {"host": bench.get("host"), "bench": bench.get("name")}


def purge_old_data():
	settings = frappe.get_single("Wolf Settings")
	now = now_datetime()
	metric_days = cint(settings.metric_retention_days) or 14
	event_days = cint(settings.event_retention_days) or 90
	frappe.db.delete("Wolf Metric Sample", {"timestamp": ("<", add_days(now, -metric_days))})
	# Keep events attached to an unresolved incident, or linked as its deployment.
	frappe.db.sql(
		"""delete from `tabWolf Event`
		where `timestamp` < %(before)s
			and coalesce(incident, '') not in (
				select name from `tabWolf Incident` where status != 'Resolved'
			)
			and name not in (
				select deployment_event from `tabWolf Incident`
				where status != 'Resolved' and coalesce(deployment_event, '') != ''
			)""",
		{"before": add_days(now, -event_days)},
	)
	frappe.db.delete("Wolf Silence", {"ends_at": ("<", add_days(now, -event_days))})
	frappe.db.commit()
