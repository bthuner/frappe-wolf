import frappe


def execute():
	"""Sites installed before action links had no stored value; an unset Int reads as 0 (= off)."""
	stored = frappe.db.sql(
		"select value from `tabSingles` where doctype = 'Wolf Settings' and field = 'action_link_hours'"
	)
	if not stored:
		frappe.db.set_single_value("Wolf Settings", "action_link_hours", 24)
