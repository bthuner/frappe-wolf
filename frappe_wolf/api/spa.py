"""Boot data for the Wolf UI (/wolf). The page embeds it; the SPA can reload it after login."""

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


@frappe.whitelist()
def get_boot():
	frappe.only_for(spa.READ_ROLES)
	return get_boot_data()
