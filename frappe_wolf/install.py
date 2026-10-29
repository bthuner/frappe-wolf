import frappe

from frappe_wolf.wolf.local_signals import after_migrate_marker
from frappe_wolf.wolf.presets import install_presets

ROLES = ("Wolf Manager", "Wolf User")


def ensure_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)


def ensure_settings():
	settings = frappe.get_single("Wolf Settings")
	if not settings.get_password("registration_token", raise_exception=False):
		settings.registration_token = frappe.generate_hash(length=32)
		settings.flags.ignore_permissions = True
		settings.save()


def after_install():
	ensure_roles()
	ensure_settings()
	install_presets()


def after_migrate():
	ensure_roles()
	install_presets()
	after_migrate_marker()
