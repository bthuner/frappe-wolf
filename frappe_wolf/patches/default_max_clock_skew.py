import frappe


def execute():
	"""An unset Int single value reads as 0, which would disable clamping on existing sites."""
	stored = frappe.db.sql(
		"select value from `tabSingles` where doctype = 'Wolf Settings' and field = 'max_clock_skew_seconds'"
	)
	if not stored:
		frappe.db.set_single_value("Wolf Settings", "max_clock_skew_seconds", 300)
