"""Dokos preset alert rules, installed (but never overwritten) on install/migrate."""

import frappe

PRESETS = [
	{
		"rule_name": "502 spike after deployment",
		"description": "Nginx returned 502s for a site shortly after a deployment on its bench.",
		"signal_type": "metric", "metric_name": "http_502", "aggregation": "sum",
		"operator": ">=", "threshold": 10, "window_minutes": 5, "group_by": "site",
		"require_recent_deployment": 1, "deployment_window_minutes": 30,
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "5xx spike",
		"description": "Sustained 5xx responses on a site, deployment or not.",
		"signal_type": "metric", "metric_name": "http_5xx", "aggregation": "sum",
		"operator": ">=", "threshold": 25, "window_minutes": 5, "group_by": "site",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "Nightly cron failure (01:00-04:00)",
		"description": "A scheduled job failed during the nightly maintenance window.",
		"signal_type": "event", "event_type": "cron_failure",
		"operator": ">=", "threshold": 1, "window_minutes": 15, "group_by": "site",
		"active_from": "01:00:00", "active_to": "04:00:00",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 240,
	},
	{
		"rule_name": "Scheduler failure",
		"description": "Any failed scheduled job outside the nightly window.",
		"signal_type": "event", "event_type": "cron_failure",
		"operator": ">=", "threshold": 1, "window_minutes": 15, "group_by": "site",
		"active_from": "04:00:00", "active_to": "01:00:00",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Worker crash",
		"description": "A background worker died or a job was killed.",
		"signal_type": "event", "event_type": "worker_failure",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "bench",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 30,
	},
	{
		"rule_name": "Worker queue backlog",
		"description": "Jobs pile up in an RQ queue for 10 minutes.",
		"signal_type": "metric", "metric_name": "queue_length", "aggregation": "min",
		"operator": ">", "threshold": 500, "window_minutes": 10, "min_samples": 3, "group_by": "host_label",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "SSH brute force",
		"description": "Many failed SSH logins on a host.",
		"signal_type": "event", "event_type": "security", "event_category": "ssh_failed_login",
		"operator": ">=", "threshold": 20, "window_minutes": 10, "group_by": "host",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Repeated failed Frappe logins",
		"description": "Several failed desk logins on a site.",
		"signal_type": "event", "event_type": "security", "event_category": "frappe_failed_login",
		"operator": ">=", "threshold": 5, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Admin login from new IP",
		"description": "A privileged user logged in from an IP never seen for them.",
		"signal_type": "event", "event_type": "security", "event_category": "new_admin_ip",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Privileged role granted",
		"description": "A user received System Manager / Administrator.",
		"signal_type": "event", "event_type": "security", "event_category": "privileged_user",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Disk almost full",
		"description": "A mount stayed above 90% for 5 minutes.",
		"signal_type": "metric", "metric_name": "disk_used_percent", "aggregation": "min",
		"operator": ">", "threshold": 90, "window_minutes": 5, "min_samples": 3, "group_by": "host_label",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "High CPU",
		"description": "CPU stayed above 90% for 10 minutes.",
		"signal_type": "metric", "metric_name": "cpu_usage", "aggregation": "min",
		"operator": ">", "threshold": 90, "window_minutes": 10, "min_samples": 5, "group_by": "host",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "High memory",
		"description": "Memory stayed above 90% for 10 minutes.",
		"signal_type": "metric", "metric_name": "memory_used_percent", "aggregation": "min",
		"operator": ">", "threshold": 90, "window_minutes": 10, "min_samples": 5, "group_by": "host",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "High load",
		"description": "Load per core averaged above 2 for 10 minutes.",
		"signal_type": "metric", "metric_name": "load_per_core", "aggregation": "avg",
		"operator": ">", "threshold": 2, "window_minutes": 10, "min_samples": 5, "group_by": "host",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "Agent silent",
		"description": "A registered Wolf agent stopped reporting.",
		"signal_type": "event", "event_type": "host_metric_alert", "event_category": "agent_silent",
		"operator": ">=", "threshold": 1, "window_minutes": 15, "group_by": "host",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 0,
	},
]  # fmt: skip


def install_presets():
	for preset in PRESETS:
		if frappe.db.exists("Wolf Alert Rule", preset["rule_name"]):
			continue
		doc = frappe.get_doc({"doctype": "Wolf Alert Rule", "enabled": 1, "is_preset": 1, **preset})
		doc.flags.ignore_permissions = True
		doc.insert()
