"""Acknowledge / resolve an incident from a signed link in an alert.

GET only shows a confirmation page, so mail scanners and link previews that
fetch URLs cannot act on an incident. The button POSTs the same signed
parameters back, and only the POST changes the incident.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import escape_html, now_datetime

from frappe_wolf.wolf import links
from frappe_wolf.wolf.action_links import METHOD, link_secret


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@rate_limit(limit=60, seconds=10 * 60)
def incident_action(incident=None, action=None, expires=None, sig=None, actor=None):
	actor = (actor or "").strip() or None
	ok, reason = links.verify(link_secret(), incident, action, expires, sig, actor)
	if not ok:
		if reason == "expired":
			return _page(_("Link expired"), _("This link has expired. Open the incident in Wolf instead."), "orange", 410)
		return _page(_("Invalid link"), _("This link is not valid."), "red", 403)

	inc = frappe.db.get_value("Wolf Incident", incident, ["name", "title", "status", "severity"], as_dict=True)
	if not inc:
		return _page(_("Not found"), _("This incident no longer exists."), "red", 404)

	label = _(links.ACTION_LABEL[action])
	summary = f"<p><strong>{escape_html(inc.name)}</strong> · {escape_html(inc.severity)} · {escape_html(inc.title)}</p>"
	done = (action == "ack" and inc.status != "Open") or (action == "resolve" and inc.status == "Resolved")
	if done:
		return _page(label, summary + "<p>" + _("Nothing to do: the incident is already {0}.").format(_(inc.status)) + "</p>", "blue")

	if frappe.request and frappe.request.method != "POST":
		return _page(label, summary + _confirm_form(incident, action, expires, sig, actor, label), "orange")

	who = frappe.session.user if frappe.session.user != "Guest" else (actor or "link")
	doc = frappe.get_doc("Wolf Incident", incident)
	if action == "ack":
		doc.status = "Acknowledged"
		doc.acknowledged_by = who
		doc.acknowledged_at = now_datetime()
	else:
		doc.status = "Resolved"
		doc.resolved_by = who
	doc.flags.ignore_permissions = True
	doc.save()
	doc.add_comment("Info", f"{'Acknowledged' if action == 'ack' else 'Resolved'} from an alert link by {who}.")
	frappe.db.commit()
	return _page(label, summary + "<p>" + _("Done: the incident is now {0}.").format(_(doc.status)) + "</p>", "green")


def _confirm_form(incident, action, expires, sig, actor, label):
	fields = {"incident": incident, "action": action, "expires": expires, "sig": sig}
	if actor:
		fields["actor"] = actor
	if frappe.session.user != "Guest":
		fields["csrf_token"] = frappe.sessions.get_csrf_token()
	hidden = "".join(
		f'<input type="hidden" name="{escape_html(k)}" value="{escape_html(str(v))}">' for k, v in fields.items()
	)
	return (
		f'<form method="post" action="{METHOD}">{hidden}'
		f'<button type="submit" class="btn btn-primary">{escape_html(label)}</button></form>'
	)


def _page(title, html, color, status=200):
	frappe.respond_as_web_page(title, html, indicator_color=color, http_status_code=status)
