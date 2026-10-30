import frappe

from frappe_wolf.patches import default_signal_settings, default_storage_settings
from frappe_wolf.wolf.local_signals import after_migrate_marker
from frappe_wolf.wolf.presets import install_presets
from frappe_wolf.wolf.rate import DEFAULT_PUSHES_PER_MINUTE

ROLES = ("Wolf Manager", "Wolf User")


def ensure_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)


def ensure_settings():
	settings = frappe.get_single("Wolf Settings")
	if settings.action_link_hours is None:
		settings.action_link_hours = 24
	if settings.max_clock_skew_seconds is None:
		settings.max_clock_skew_seconds = 300
	for field, value in default_signal_settings.DEFAULTS.items():
		if settings.get(field) is None:
			settings.set(field, value)
	for field, value in default_storage_settings.DEFAULTS.items():
		if settings.get(field) in (None, ""):
			settings.set(field, value)
	if settings.max_pushes_per_minute is None:
		settings.max_pushes_per_minute = DEFAULT_PUSHES_PER_MINUTE
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
