"""Acknowledge / resolve an incident, or confirm a proposed response action, from a signed link
in an alert.

GET only shows a confirmation page, so mail scanners and link previews that
fetch URLs cannot act on an incident. The button POSTs the same signed
parameters back, and only the POST changes the incident.

A response link (block an IP, disable a user...) also needs a logged-in Wolf Manager: the
signature only says what to confirm. The confirmation goes through `response.approve`, with
all its rules (still proposed, System Managers protected, approver recorded).
"""

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, escape_html, now_datetime

from frappe_wolf.wolf import links
from frappe_wolf.wolf.action_links import METHOD, RESPONSE_METHOD, link_secret


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
	return _form(METHOD, fields, label)


def _form(method, fields, label):
	if frappe.session.user != "Guest":
		fields = {**fields, "csrf_token": frappe.sessions.get_csrf_token()}
	hidden = "".join(
		f'<input type="hidden" name="{escape_html(k)}" value="{escape_html(str(v))}">' for k, v in fields.items()
	)
	return (
		f'<form method="post" action="{method}">{hidden}'
		f'<button type="submit" class="btn btn-primary">{escape_html(label)}</button></form>'
	)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@rate_limit(limit=60, seconds=10 * 60)
def response_action(incident=None, action=None, target=None, agent=None, hours=None, expires=None, sig=None, actor=None):
	from frappe_wolf.api import response as response_api
	from frappe_wolf.wolf import response

	params = {"incident": incident, "action": action, "target": target, "agent": agent, "hours": hours,
	          "expires": expires, "actor": actor}  # fmt: skip
	params = {k: v for k, v in params.items() if v not in (None, "")}
	ok, reason = links.verify_response(link_secret(), {**params, "sig": sig})
	if not ok:
		if reason == "expired":
			return _page(_("Link expired"), _("This link has expired. Open the incident in Wolf instead."), "orange", 410)
		return _page(_("Invalid link"), _("This link is not valid."), "red", 403)
	inc = frappe.db.get_value("Wolf Incident", incident, ["name", "title", "status", "severity", "host"], as_dict=True)
	if not inc:
		return _page(_("Not found"), _("This incident no longer exists."), "red", 404)

	what = response.label(action, target, cint(hours) or None, inc.host if action != "block_ip" else
	                      (frappe.db.get_value("Wolf Agent", agent, "hostname") if agent else inc.host))  # fmt: skip
	summary = (f"<p><strong>{escape_html(inc.name)}</strong> · {escape_html(inc.severity)} · {escape_html(inc.title)}</p>"
	           f"<p>{escape_html(_('Proposed response'))}: <strong>{escape_html(what)}</strong></p>")  # fmt: skip
	taken = frappe.db.get_value("Wolf Response Action", {"incident": incident, "action": action, "target": target,
	                            "status": ("in", ("Approved", "Applied"))}, ["name", "status", "approved_by"], as_dict=True)  # fmt: skip
	if taken:
		return _page(what, summary + "<p>" + _("Nothing to do: {0} is already {1} (approved by {2}).").format(
			escape_html(taken.name), _(taken.status).lower(), escape_html(taken.approved_by)) + "</p>", "blue")  # fmt: skip
	if frappe.session.user == "Guest":
		login = "/login?" + urlencode({"redirect-to": frappe.request.full_path if frappe.request else RESPONSE_METHOD})
		return _page(what, summary + "<p>" + _("Only a Wolf Manager can confirm a response action.") +
		             f' <a class="btn btn-primary" href="{escape_html(login)}">{escape_html(_("Log in to confirm"))}</a></p>', "orange")  # fmt: skip
	if not response_api._is_manager():
		return _page(what, summary + "<p>" + _("Only a Wolf Manager or System Manager can confirm a response action.") + "</p>",
		             "red", 403)  # fmt: skip
	if frappe.request and frappe.request.method != "POST":
		return _page(what, summary + _form(RESPONSE_METHOD, {**params, "sig": sig}, _("Confirm: {0}").format(what)), "orange")

	sent_to = f" sent to {actor}" if actor else ""
	try:
		row = response_api.approve(incident, action, target, agent=agent, duration_hours=hours,
		                           reason=f"confirmed from an alert link{sent_to}")  # fmt: skip
	except (frappe.ValidationError, frappe.PermissionError) as e:
		frappe.db.rollback()
		frappe.clear_messages()
		return _page(what, summary + "<p>" + escape_html(str(e)) + "</p>", "red", 409)
	frappe.db.commit()
	note = {"Approved": _("approved: the agent applies it at its next push"), "Applied": _("applied: {0}").format(row.get("result")),
	        "Failed": _("failed: {0}").format(row.get("result"))}[row["status"]]  # fmt: skip
	return _page(what, summary + "<p>" + escape_html(f"{row['name']} {note}.") + "</p>",
	             "red" if row["status"] == "Failed" else "green")


def _page(title, html, color, status=200):
	frappe.respond_as_web_page(title, html, indicator_color=color, http_status_code=status)
