import frappe


def execute():
	"""Give every Wolf Event a last_seen (first seen for old records), so window queries can
	use the last_seen index instead of coalesce(last_seen, timestamp)."""
	frappe.db.sql("update `tabWolf Event` set last_seen = `timestamp` where last_seen is null")
