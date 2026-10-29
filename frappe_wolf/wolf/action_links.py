"""Frappe side of the signed acknowledge/resolve links (see links.py for the crypto)."""

import hashlib
import hmac

import frappe
from frappe.utils import cint
from frappe.utils.password import get_encryption_key

from frappe_wolf.wolf import links

METHOD = "/api/method/frappe_wolf.api.actions.incident_action"


def link_secret():
	"""Derived from the site's encryption key: per site, never stored, nothing to sync."""
	return hmac.new(get_encryption_key().encode(), b"frappe_wolf.action_links.v1", hashlib.sha256).hexdigest()


def link_hours():
	value = frappe.db.get_single_value("Wolf Settings", "action_link_hours")
	return 24 if value is None else cint(value)


def action_urls(incident, status, actor=None):
	"""{"ack": url, "resolve": url} for an unresolved incident, or {} when links are off."""
	hours = link_hours()
	if hours <= 0 or status == "Resolved":
		return {}
	from frappe_wolf.wolf.notify import public_base

	base = public_base() + METHOD
	actions = ("resolve",) if status == "Acknowledged" else links.ACTIONS
	return {a: links.build_url(base, link_secret(), incident, a, hours * 3600, actor=actor) for a in actions}
