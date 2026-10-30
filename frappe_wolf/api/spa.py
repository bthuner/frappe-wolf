"""Boot data for the Wolf UI (/wolf). The page embeds it; the SPA can reload it after login."""

import os

import frappe

from frappe_wolf import __version__, satellite
from frappe_wolf.wolf import access, events, spa


def get_boot_data():
	user = frappe.session.user
	return spa.boot(
		user=user,
		full_name=frappe.utils.get_fullname(user),
		roles=frappe.get_roles(user),
		time_zone=events.system_tz(),
		server_version=__version__,
		is_satellite=satellite.is_satellite(),
		restricted=access.grants(user) is not None,
	)


def ui_built():
	"""The frontend build writes www/wolf.html; without it /wolf is a 404 (bench build skipped it)."""
	return os.path.exists(os.path.join(os.path.dirname(os.path.dirname(__file__)), "www", "wolf.html"))


def boot_session(bootinfo):
	"""hooks.py: tells Desk whether to offer the Wolf UI."""
	bootinfo.wolf_ui = ui_built()


@frappe.whitelist()
def get_boot():
	frappe.only_for(spa.READ_ROLES)
	return get_boot_data()
