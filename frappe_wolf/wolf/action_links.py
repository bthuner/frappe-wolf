"""Frappe side of the signed acknowledge/resolve links (see links.py for the crypto)."""

import hashlib
import hmac
from urllib.parse import urlencode

import frappe
from frappe.utils import cint
from frappe.utils.password import get_encryption_key

from frappe_wolf.wolf import links

METHOD = "/api/method/frappe_wolf.api.actions.incident_action"
RESPONSE_METHOD = "/api/method/frappe_wolf.api.actions.response_action"
MAX_RESPONSE_LINKS = 5


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


def response_links(incident, status, actor=None):
	"""[{"label", "url"}] for the response actions Wolf proposes on this incident now, or [] when
	links are off. Each link only says what to confirm: the page asks for a Wolf Manager's login."""
	hours = link_hours()
	if hours <= 0 or status == "Resolved":
		return []
	# read once per request or job: render, message and each channel ask again
	cache = getattr(frappe.local, "wolf_response_proposals", None)
	if cache is None:
		cache = frappe.local.wolf_response_proposals = {}
	if incident not in cache:
		from frappe_wolf.api.response import _proposals

		cache[incident] = _proposals(frappe.get_doc("Wolf Incident", incident))[:MAX_RESPONSE_LINKS]
	from frappe_wolf.wolf import response
	from frappe_wolf.wolf.notify import public_base

	base = public_base() + RESPONSE_METHOD
	out = []
	for p in cache[incident]:
		duration = response.DEFAULT_HOURS if p["action"] == "block_ip" else None
		params = links.build_response_params(
			link_secret(), incident, p["action"], p["target"], hours * 3600,
			agent=p["agent"] if p["action"] == "block_ip" else None, hours=duration, actor=actor,
		)  # fmt: skip
		out.append({"label": response.label(p["action"], p["target"], duration, p.get("host")), "url": f"{base}?{urlencode(params)}"})
	return out
