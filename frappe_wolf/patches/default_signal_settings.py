import frappe

DEFAULTS = {
	"capture_email_queue": 1,
	"email_queue_stale_minutes": 30,
	"capture_admin_countries": 1,
	"capture_app_security": 1,
	"ip_reputation_sources": "firehol_level1 https://iplists.firehol.org/files/firehol_level1.netset\n"
	"spamhaus_drop https://www.spamhaus.org/drop/drop.txt",
	"siem_port": 514,
	"siem_transport": "tcp",
	"siem_format": "ocsf",
	"siem_min_severity": "warning",
	"siem_event_types": "security",
	"siem_journal": 1,
	"siem_verify_tls": 1,
}


def execute():
	"""Settings added after install (email queue, GeoIP, application security): unset Check/Int
	values read as 0, so existing sites get the default."""
	for field, value in DEFAULTS.items():
		stored = frappe.db.sql(
			"select value from `tabSingles` where doctype = 'Wolf Settings' and field = %s", (field,)
		)
		if not stored:
			frappe.db.set_single_value("Wolf Settings", field, value)
