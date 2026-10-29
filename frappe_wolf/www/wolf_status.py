"""Public status page: /wolf-status/<slug> (see website_route_rules in hooks.py).

Guest access by design. Only Wolf Status Pages with Enabled ticked answer; everything else is
a 404. The template escapes every value.
"""

import frappe

from frappe_wolf.wolf.status_page import get_public_status

no_cache = 1
sitemap = 0


def get_context(context):
	data = get_public_status(frappe.form_dict.get("slug"))
	if not data:
		raise frappe.PageDoesNotExistError
	context.update(data)
	# the template sets <title> itself, escaped; no_header keeps web.html from printing it raw
	context.no_header = 1
	context.no_breadcrumbs = 1
	context.show_sidebar = 0
	return context
