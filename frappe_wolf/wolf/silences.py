"""Maintenance windows: incidents keep opening, notifications are held back."""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from frappe_wolf.wolf import conditions

MAX_MINUTES = 24 * 60


def active(now=None):
	now = now or now_datetime()
	return frappe.get_all(
		"Wolf Silence",
		filters={"starts_at": ("<=", now), "ends_at": (">", now)},
		fields=["name", "reason", "host", "bench", "site", "alert_rule", "ends_at"],
		order_by="ends_at desc",
	)


def active_for(inc, now=None):
	"""The first active silence matching an incident (dict-like with host/bench/site/alert_rule)."""
	scope = {f: inc.get(f) for f in ("host", "bench", "site")}
	for silence in active(now):
		if conditions.silence_matches(silence, scope, inc.get("alert_rule")):
			return silence
	return None


def hold(incident, silence, kind):
	values = {"silence": silence.name}
	if kind != "resolved":
		values["silenced"] = 1
	frappe.db.set_value("Wolf Incident", incident, values, update_modified=False)
	_comment(
		incident,
		f"{kind.capitalize()} notification held back by maintenance window {silence.name}"
		+ (f" ({silence.reason})" if silence.reason else "")
		+ f" until {silence.ends_at}.",
	)


def release(incident, note):
	frappe.db.set_value("Wolf Incident", incident, "silenced", 0, update_modified=False)
	_comment(incident, note)


def release_expired():
	"""Incidents still open when their silence ends get the notification they missed."""
	from frappe_wolf.wolf import notify

	held = frappe.get_all(
		"Wolf Incident",
		filters={"silenced": 1, "status": ("in", ("Open", "Acknowledged"))},
		fields=["name", "alert_rule", "host", "bench", "site", "status"],
	)
	if not held:
		return []
	silences = active()
	released = []
	for inc in held:
		scope = {f: inc.get(f) for f in ("host", "bench", "site")}
		if any(conditions.silence_matches(s, scope, inc.alert_rule) for s in silences):
			continue
		release(inc.name, "Maintenance window over and the incident is still open: notifying now.")
		if inc.status == "Open":
			notify.enqueue(inc.name, "fired")
		released.append(inc.name)
	return released


def create(minutes, host=None, bench=None, site=None, reason=None, source="api", deployment_event=None, starts_at=None):
	minutes = cint(minutes)
	if minutes <= 0:
		return None
	starts_at = starts_at or now_datetime()
	doc = frappe.get_doc(
		{
			"doctype": "Wolf Silence",
			"starts_at": starts_at,
			"ends_at": add_to_date(starts_at, minutes=min(minutes, MAX_MINUTES)),
			"host": host,
			"bench": bench,
			"site": site,
			"reason": (reason or "")[:255] or None,
			"source": source,
			"deployment_event": deployment_event,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _comment(incident, text):
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Wolf Incident",
			"reference_name": incident,
			"content": text,
		}
	).insert(ignore_permissions=True)
