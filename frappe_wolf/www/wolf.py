"""The Wolf UI: /wolf and every route under it (website_route_rules in hooks.py).

wolf.html is written by the frontend build (frontend/, `bench build --app frappe_wolf`). Guests
go to the login page and come back; signed-in users without a Wolf role get a 403.
"""

from urllib.parse import quote

import frappe
from frappe.sessions import get_csrf_token

from frappe_wolf.api.spa import get_boot_data
from frappe_wolf.wolf import spa

no_cache = 1
sitemap = 0


def get_context(context):
	denied = spa.access(frappe.session.user, frappe.get_roles())
	if denied == "login":
		path = frappe.local.request.full_path.rstrip("?") if frappe.local.request else "/wolf"
		frappe.local.flags.redirect_location = f"/login?redirect-to={quote(path, safe='/')}"
		raise frappe.Redirect(302)  # not 301: browsers cache that, and would skip /wolf after login
	if denied == "forbidden":
		raise frappe.PermissionError(frappe._("You need the Wolf User or Wolf Manager role to open Frappe Wolf."))

	context.no_header = 1
	context.no_breadcrumbs = 1
	# read by the frappe-ui jinjaBootData plugin into window.<key>
	context.boot = {
		"csrf_token": get_csrf_token(),
		"site_name": frappe.local.site,
		"wolf": get_boot_data(),
	}
	return context
