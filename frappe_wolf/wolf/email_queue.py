"""Email Queue backlog and errors on this site (central or satellite), checked every 5 minutes."""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from frappe_wolf.wolf import events
from frappe_wolf.wolf.schema import fingerprint

ERROR_WINDOW_MINUTES = 15
PENDING = ("Not Sent", "Sending")


def stats(stale_minutes, now=None):
	"""`{"backlog", "oldest", "errors", "latest_error"}` from the Email Queue table."""
	now = now or now_datetime()
	cutoff = add_to_date(now, minutes=-stale_minutes)
	backlog = frappe.db.sql(
		"""select count(*) as n, min(creation) as oldest from `tabEmail Queue`
		where status in %(pending)s and creation < %(cutoff)s
			and (send_after is null or send_after < %(cutoff)s)""",
		{"pending": PENDING, "cutoff": cutoff},
		as_dict=True,
	)[0]
	errors = frappe.db.sql(
		"""select count(*) as n, max(modified) as latest from `tabEmail Queue`
		where status = 'Error' and modified >= %(since)s""",
		{"since": add_to_date(now, minutes=-ERROR_WINDOW_MINUTES)},
		as_dict=True,
	)[0]
	return {
		"backlog": cint(backlog.n),
		"oldest": backlog.oldest,
		"errors": cint(errors.n),
		"latest_error": errors.latest,
	}


def check(now=None):
	"""Emit `email_queue_backlog` / `email_queue_error` events while the problem lasts.

	Each run bumps the same event (fingerprint per site), so the preset rules see one event
	that keeps counting and auto-resolve once a whole window passes without one.
	"""
	settings = frappe.get_cached_doc("Wolf Settings")
	if not cint(settings.get("capture_email_queue")):
		return None
	stale = cint(settings.get("email_queue_stale_minutes")) or 30
	found = stats(stale, now)
	site = frappe.local.site
	suspended = bool(cint(frappe.db.get_default("suspend_email_queue")))
	if found["backlog"]:
		events.emit(
			"error",
			f"{found['backlog']} email(s) waiting in the Email Queue for more than {stale} min",
			severity="warning",
			category="email_queue_backlog",
			source="frappe",
			payload={"pending": found["backlog"], "oldest": str(found["oldest"]), "stale_minutes": stale, "suspended": suspended},
			fingerprint=fingerprint("email_queue_backlog", site),
			dedupe_minutes=ERROR_WINDOW_MINUTES,
		)
	if found["errors"]:
		events.emit(
			"error",
			f"{found['errors']} email(s) failed to send in the last {ERROR_WINDOW_MINUTES} min",
			severity="warning",
			category="email_queue_error",
			source="frappe",
			payload={"errors": found["errors"], "latest": str(found["latest_error"])},
			fingerprint=fingerprint("email_queue_error", site),
			dedupe_minutes=ERROR_WINDOW_MINUTES,
		)
	return found
