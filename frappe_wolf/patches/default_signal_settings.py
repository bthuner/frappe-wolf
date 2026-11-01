import frappe

DEFAULTS = {
	"capture_email_queue": 1,
	"email_queue_stale_minutes": 30,
	"capture_admin_countries": 1,
	"capture_app_security": 1,
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
