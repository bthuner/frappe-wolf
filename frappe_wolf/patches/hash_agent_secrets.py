import frappe

from frappe_wolf.api.agent import migrate_legacy_secret


def execute():
	"""Agent secrets used to be a reversible Password field. Keep only a salted hash.

	Registered agents keep working with the secret in their config; nothing to re-register.
	"""
	for name in frappe.get_all("Wolf Agent", filters={"secret_hash": ("is", "not set")}, pluck="name"):
		migrate_legacy_secret(name)
	# leftovers, e.g. undecryptable rows or agents that already have a hash
	frappe.db.delete("__Auth", {"doctype": "Wolf Agent", "fieldname": "agent_secret"})
