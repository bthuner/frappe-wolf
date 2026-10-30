import frappe

from frappe_wolf.wolf.rate import DEFAULT_PUSHES_PER_MINUTE


def execute():
	"""An unset Int single value reads as 0, which would turn the push rate limit off on existing sites."""
	stored = frappe.db.sql(
		"select value from `tabSingles` where doctype = 'Wolf Settings' and field = 'max_pushes_per_minute'"
	)
	if not stored:
		frappe.db.set_single_value("Wolf Settings", "max_pushes_per_minute", DEFAULT_PUSHES_PER_MINUTE)
