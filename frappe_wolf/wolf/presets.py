"""Dokos preset alert rules, installed (but never overwritten) on install/migrate.

No frappe import at module level: the preset list is checked by the pure unit tests.
"""


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
	{
		"rule_name": "Scheduler stalled",
		"description": "This site's scheduled jobs (and so Wolf's rules) stopped running. Recorded "
		"by agent pushes while stalled, it fires once the scheduler runs again; the live alert "
		"comes from the agent watchdog's fallback channel.",
		"signal_type": "event", "event_type": "host_metric_alert", "event_category": "scheduler_stalled",
		"operator": ">=", "threshold": 1, "window_minutes": 15, "group_by": "site",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	# --- RQ and the queue Redis (agent) ---
	{
		"rule_name": "Background jobs failing",
		"description": "Jobs keep landing in RQ's failed-job registry (rq_failed_jobs_added).",
		"signal_type": "metric", "metric_name": "rq_failed_jobs_added", "aggregation": "sum",
		"operator": ">=", "threshold": 5, "window_minutes": 10, "group_by": "bench",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Long-running background job",
		"description": "A started RQ job has run longer than the agent's rq.long_job_minutes (default 30) "
		"for 15 minutes.",
		"signal_type": "metric", "metric_name": "rq_long_running_jobs", "aggregation": "min",
		"operator": ">", "threshold": 0, "window_minutes": 15, "min_samples": 3, "group_by": "host_label",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "No background workers",
		"description": "No live RQ worker is registered in the bench's queue Redis.",
		"signal_type": "metric", "metric_name": "rq_workers", "aggregation": "max",
		"operator": "<", "threshold": 1, "window_minutes": 5, "min_samples": 3, "group_by": "bench",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "Queue Redis memory high",
		"description": "The queue Redis stayed above 90% of maxmemory (or of system memory) for 10 minutes.",
		"signal_type": "metric", "metric_name": "redis_used_memory_percent", "aggregation": "min",
		"operator": ">", "threshold": 90, "window_minutes": 10, "min_samples": 5, "group_by": "bench",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Queue Redis evicting keys",
		"description": "The queue Redis evicted keys: queued jobs are being lost.",
		"signal_type": "metric", "metric_name": "redis_evicted_keys", "aggregation": "sum",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "bench",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	# --- backups ---
	{
		"rule_name": "No backup for 26 h",
		"description": "The newest database backup of a site is older than 26 hours (backup_age_hours).",
		"signal_type": "metric", "metric_name": "backup_age_hours", "aggregation": "last",
		"operator": ">", "threshold": 26, "window_minutes": 60, "group_by": "site",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Backup failed",
		"description": "An Error Log reported a failed backup (local or S3/Dropbox/Google Drive).",
		"signal_type": "event", "event_type": "error", "event_category": "backup_failed",
		"operator": ">=", "threshold": 1, "window_minutes": 60, "group_by": "site",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	# --- TLS ---
	{
		"rule_name": "TLS certificate expiring within 14 days",
		"description": "A site domain's certificate expires soon and was not renewed (tls_days_left).",
		"signal_type": "metric", "metric_name": "tls_days_left", "aggregation": "last",
		"operator": "<", "threshold": 14, "window_minutes": 180, "group_by": "host_label",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 0,
	},
	{
		"rule_name": "TLS certificate expiring within 3 days",
		"description": "A site domain's certificate expires within 3 days (or has expired).",
		"signal_type": "metric", "metric_name": "tls_days_left", "aggregation": "last",
		"operator": "<", "threshold": 3, "window_minutes": 180, "group_by": "host_label",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 0,
	},
	# --- MariaDB ---
	{
		"rule_name": "Database connections high",
		"description": "MariaDB used more than 85% of max_connections for 5 minutes.",
		"signal_type": "metric", "metric_name": "db_connections_used_percent", "aggregation": "min",
		"operator": ">", "threshold": 85, "window_minutes": 5, "min_samples": 3, "group_by": "host_label",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "Slow queries",
		"description": "MariaDB logged 20 or more slow queries (long_query_time) in 10 minutes.",
		"signal_type": "metric", "metric_name": "db_slow_queries", "aggregation": "sum",
		"operator": ">=", "threshold": 20, "window_minutes": 10, "group_by": "host_label",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Replication lag",
		"description": "A MariaDB replica stayed more than 5 minutes behind its primary for 10 minutes.",
		"signal_type": "metric", "metric_name": "db_replication_lag_seconds", "aggregation": "min",
		"operator": ">", "threshold": 300, "window_minutes": 10, "min_samples": 5, "group_by": "host_label",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	{
		"rule_name": "Replication stopped",
		"description": "A MariaDB replica's IO or SQL thread is not running.",
		"signal_type": "metric", "metric_name": "db_replication_running", "aggregation": "max",
		"operator": "<", "threshold": 1, "window_minutes": 5, "min_samples": 3, "group_by": "host_label",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	# --- bench processes ---
	{
		"rule_name": "Bench process down",
		"description": "A supervisor/systemd process of the bench stayed down (stopped, crashed or "
		"restart loop) for 3 minutes. Use a silence for planned `bench stop`.",
		"signal_type": "metric", "metric_name": "bench_processes_down", "aggregation": "min",
		"operator": ">", "threshold": 0, "window_minutes": 3, "min_samples": 3, "group_by": "bench",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 30,
	},
	# --- site-side signals (Wolf App, satellites included) ---
	{
		"rule_name": "Email queue backlog",
		"description": "Emails waited in the Email Queue longer than Wolf Settings → Email Queue Stale Minutes.",
		"signal_type": "event", "event_type": "error", "event_category": "email_queue_backlog",
		"operator": ">=", "threshold": 1, "window_minutes": 15, "group_by": "site",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Email sending errors",
		"description": "Email Queue entries went to Error status.",
		"signal_type": "event", "event_type": "error", "event_category": "email_queue_error",
		"operator": ">=", "threshold": 1, "window_minutes": 15, "group_by": "site",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Admin login from new country",
		"description": "A privileged user logged in from a country none of their earlier logins came "
		"from (needs Wolf Settings → GeoIP Database).",
		"signal_type": "event", "event_type": "security", "event_category": "admin_login_new_country",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	# --- cyber: host and web attack signals (agent auth and nginx parsers) ---
	{
		"rule_name": "SSH login after brute force",
		"description": "An SSH login succeeded from an address that failed 5+ times in the hour before: "
		"the brute force probably worked. Lock the account and check the session (MITRE T1110).",
		"signal_type": "event", "event_type": "security", "event_category": "ssh_login_after_failures",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "host",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Repeated sudo failures",
		"description": "Wrong sudo passwords or users outside sudoers trying to escalate (MITRE T1548.003).",
		"signal_type": "event", "event_type": "security", "event_category": "sudo_failure",
		"operator": ">=", "threshold": 3, "window_minutes": 10, "group_by": "host",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "New system account",
		"description": "useradd created a local account on a host (persistence, MITRE T1136.001). "
		"Silence it during planned provisioning.",
		"signal_type": "event", "event_type": "security", "event_category": "account_created",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "host",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "User added to admin group",
		"description": "A user joined sudo, wheel, admin, root, adm, docker or lxd on a host "
		"(MITRE T1098).",
		"signal_type": "event", "event_type": "security", "event_category": "admin_group_change",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "host",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Web attack scan",
		"description": "100+ requests in 10 minutes probing a site for secrets, CMS panels, path "
		"traversal or injections (MITRE T1595). Scans are constant on the Internet: this "
		"catches the heavy ones.",
		"signal_type": "event", "event_type": "security", "event_category": "web_attack_probe",
		"operator": ">=", "threshold": 100, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Sensitive file served",
		"description": "nginx answered 2xx to a request for .env, .git/, site_config.json, a private "
		"key or a database dump: the file was probably leaked. Rotate the secrets (MITRE T1552.001).",
		"signal_type": "event", "event_type": "security", "event_category": "web_sensitive_file_exposed",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	# --- cyber: Frappe application security (site signals, Wolf Settings → Flag risky application changes) ---
	{
		"rule_name": "Server Script can run new code",
		"description": "A Server Script was created, enabled or had its code, type or trigger changed "
		"(MITRE T1059.006). Critical for API and scheduled scripts or guest access.",
		"signal_type": "event", "event_type": "security", "event_category": "server_script_change",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Client Script can run new code",
		"description": "A Client Script was created, enabled or changed: it runs in every user's browser "
		"on that form (MITRE T1059.007).",
		"signal_type": "event", "event_type": "security", "event_category": "client_script_change",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "System Console used",
		"description": "Someone ran Python or SQL in the System Console (MITRE T1059.006).",
		"signal_type": "event", "event_type": "security", "event_category": "console_execution",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 60,
	},
	{
		"rule_name": "Security settings weakened",
		"description": "System Settings lowered a defence: 2FA or the password policy off, lockout "
		"loosened, longer sessions, guest uploads (MITRE T1562.001).",
		"signal_type": "event", "event_type": "security", "event_category": "security_settings_weakened",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Permission rules changed",
		"description": "Permission rules changed (Role Permission Manager, Data Import, REST or code), a "
		"Role Profile granting more, a User Permission letting a user see more, or a Website user "
		"given desk access (MITRE T1098).",
		"signal_type": "event", "event_type": "security", "event_category": "permission_change",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 60,
	},
	{
		"rule_name": "API key generated",
		"description": "A new API secret was written for a user: anyone holding it acts as that "
		"user (MITRE T1098.001). Critical for privileged users.",
		"signal_type": "event", "event_type": "security", "event_category": "api_key_generated",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Webhook to a new host",
		"description": "A Webhook was created, enabled or pointed at another host: it sends documents "
		"out of the site (MITRE T1567).",
		"signal_type": "event", "event_type": "security", "event_category": "webhook_external",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "warning", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	# --- cyber: posture (hourly score, api/security.py) ---
	{
		"rule_name": "Security score low",
		"description": "A site's security score (checks Wolf could prove passed, out of 100) is below 60. "
		"Open Wolf → Security for the failing checks and their fixes.",
		"signal_type": "metric", "metric_name": "security_score", "aggregation": "last",
		"operator": "<", "threshold": 60, "window_minutes": 180, "group_by": "site",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Bench service exposed",
		"description": "MariaDB, a bench Redis, socket.io or gunicorn listens beyond loopback on a host "
		"(exposed_services). Redis has no password by default.",
		"signal_type": "metric", "metric_name": "exposed_services", "aggregation": "last",
		"operator": ">", "threshold": 0, "window_minutes": 30, "group_by": "host",
		"severity": "critical", "auto_resolve": 1, "cooldown_minutes": 0,
	},
	{
		"rule_name": "Development settings in production",
		"description": "developer_mode, allow_tests, a plain-HTTP host_name or allow_cors * in a site's "
		"site_config.json (site_config_risks).",
		"signal_type": "metric", "metric_name": "site_config_risks", "aggregation": "last",
		"operator": ">", "threshold": 0, "window_minutes": 30, "group_by": "site",
		"severity": "warning", "auto_resolve": 1, "cooldown_minutes": 0,
	},
	# --- deception: honeytokens (Wolf Settings → Threat intelligence and deception) ---
	{
		"rule_name": "Honeytoken touched",
		"description": "A decoy user, API key or path was used. Nobody legitimate knows them: treat it "
		"as an intrusion until proven otherwise.",
		"signal_type": "event", "event_type": "security", "event_category": "honeytoken_triggered",
		"operator": ">=", "threshold": 1, "window_minutes": 10, "group_by": "site",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
	# --- compliance: NIS2 / GDPR deadlines on flagged incidents (api/compliance.py) ---
	{
		"rule_name": "Regulatory deadline",
		"description": "A NIS2 or GDPR reporting deadline of a flagged incident is within 6 h, or has "
		"passed. Add the channels of whoever reports to the CSIRT / CNIL.",
		"signal_type": "event", "event_type": "security", "event_category": "regulatory_deadline",
		"operator": ">=", "threshold": 1, "window_minutes": 15, "group_by": "site",
		"severity": "critical", "auto_resolve": 0, "cooldown_minutes": 0,
	},
]  # fmt: skip


def install_presets():
	import frappe

	for preset in PRESETS:
		if frappe.db.exists("Wolf Alert Rule", preset["rule_name"]):
			continue
		doc = frappe.get_doc({"doctype": "Wolf Alert Rule", "enabled": 1, "is_preset": 1, **preset})
		doc.flags.ignore_permissions = True
		doc.insert()
