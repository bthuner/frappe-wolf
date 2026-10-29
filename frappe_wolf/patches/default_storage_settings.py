import frappe

DEFAULTS = {
	"raw_retention_hours": 48,
	"minute_retention_days": 30,
	"hour_retention_days": 365,
	"ingest_mode": "async",
}


def execute():
	"""Store defaults for the storage settings: an unset Int single value reads as 0 on existing sites."""
	stored = set(
		frappe.db.sql_list(
			"select field from `tabSingles` where doctype = 'Wolf Settings' and field in %(fields)s",
			{"fields": tuple(DEFAULTS)},
		)
	)
	for field, value in DEFAULTS.items():
		if field not in stored:
			frappe.db.set_single_value("Wolf Settings", field, value)
