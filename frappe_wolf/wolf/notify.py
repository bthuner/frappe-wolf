"""Alert delivery: email, webhook (Slack/Mattermost compatible) and Raven."""

import frappe
import requests
from frappe.utils import escape_html, get_datetime, get_url, now_datetime

from frappe_wolf.wolf import conditions

KIND_LABEL = {
	"fired": "FIRING",
	"reopened": "RE-OPENED",
	"resolved": "RESOLVED",
	"escalated": "ESCALATED",
	"test": "TEST",
}
FIRING = ("fired", "reopened")
SILENCEABLE = ("fired", "reopened", "resolved")
ACTIONABLE = ("fired", "reopened", "escalated")


def enqueue(incident, kind):
	"""Decide now (silences, bookkeeping), deliver in a background job.

	Returns False when a maintenance window holds the notification back.
	"""
	from frappe_wolf.wolf import silences

	if kind in SILENCEABLE:
		inc = frappe.db.get_value(
			"Wolf Incident", incident, ["name", "alert_rule", "host", "bench", "site", "silenced"], as_dict=True
		)
		if kind == "resolved" and inc and inc.silenced:
			# Nobody heard about it firing, so nobody needs to hear it resolved.
			silences.release(incident, "Resolved during the maintenance window, no notification sent.")
			return False
		silence = silences.active_for(inc) if inc else None
		if silence:
			silences.hold(incident, silence, kind)
			return False
	if kind in FIRING:
		frappe.db.set_value(
			"Wolf Incident",
			incident,
			{"notified_at": now_datetime(), "escalated_at": None, "silenced": 0},
			update_modified=False,
		)
	frappe.enqueue(
		"frappe_wolf.wolf.notify.send",
		queue="short",
		incident=incident,
		kind=kind,
		enqueue_after_commit=True,
	)
	return True


def public_base():
	base = frappe.db.get_single_value("Wolf Settings", "public_url")
	return base.rstrip("/") if base else get_url().rstrip("/")


def incident_url(name):
	return f"{public_base()}/app/wolf-incident/{name}"


def scope_label(inc):
	return " / ".join(x for x in (inc.site, inc.bench, inc.host) if x) or "all"


def action_urls(inc, kind, actor=None):
	if kind not in ACTIONABLE:
		return {}
	from frappe_wolf.wolf.action_links import action_urls as build

	return build(inc.name, inc.status, actor=actor)


def render(inc, kind, actor=None):
	label = KIND_LABEL.get(kind, kind.upper())
	subject = f"[Wolf] {label} {inc.severity.upper()}: {inc.title}"
	url = incident_url(inc.name)
	lines = [
		f"{label} · {inc.severity} · {inc.title}",
		f"Scope: {scope_label(inc)}",
		f"Started: {_ts(inc.started_at)}",
	]
	if inc.summary:
		lines.append(f"Details: {inc.summary}")
	if inc.deployment_at:
		lines.append(f"Deployment shortly before: {_ts(inc.deployment_at)}")
	if kind == "escalated":
		lines.append(f"Not acknowledged since {_ts(inc.notified_at)}.")
	actions = action_urls(inc, kind, actor)
	html = "<br>".join(escape_html(line) for line in lines) + f'<br><a href="{escape_html(url)}">{escape_html(inc.name)}</a>'
	if actions:
		html += "<br>" + " · ".join(
			f'<a href="{escape_html(u)}">{"Acknowledge" if a == "ack" else "Resolve"}</a>' for a, u in actions.items()
		)
	lines.append(url)
	lines += [f"{'Acknowledge' if a == 'ack' else 'Resolve'}: {u}" for a, u in actions.items()]
	return subject, "\n".join(lines), html


def _ts(value):
	"""Datetime to the second, in the site's time zone (no microseconds in alerts)."""
	return get_datetime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def recipients(value):
	return [r.strip() for r in (value or "").replace(",", "\n").splitlines() if r.strip()]


def send(incident, kind, channels=None):
	from frappe_wolf.wolf import digest

	inc = frappe.get_doc("Wolf Incident", incident)
	if channels is None:
		if not inc.alert_rule:
			return
		rule = frappe.get_doc("Wolf Alert Rule", inc.alert_rule)
		channels = rule.get("escalation_channels") if kind == "escalated" else rule.channels
	subject, text, html = render(inc, kind)
	for ch in channels or []:
		if not ch.enabled:
			continue
		try:
			if conditions.should_digest(ch.get("digest"), inc.severity, kind):
				digest.queue(ch, inc, kind)
			else:
				deliver(ch, inc, kind, subject, text, html)
		except Exception:
			frappe.log_error(title=f"[Wolf] {ch.channel_type} notification failed for {inc.name}")


def deliver(ch, inc, kind, subject, text, html):
	if ch.channel_type == "Email":
		to = recipients(ch.recipients)
		if not to:
			return
		if not action_urls(inc, kind):
			send_email(to, subject, html, inc.name)
			return
		# One message per recipient so an acknowledgement can name who clicked.
		for address in to:
			s, _t, h = render(inc, kind, actor=address)
			send_email([address], s, h, inc.name)
	elif ch.channel_type == "Webhook":
		if ch.webhook_url:
			body = {
				"text": text,
				"incident": {
					"name": inc.name,
					"kind": kind,
					"title": inc.title,
					"status": inc.status,
					"severity": inc.severity,
					"site": inc.site,
					"bench": inc.bench,
					"host": inc.host,
					"started_at": _ts(inc.started_at),
					"url": incident_url(inc.name),
					"actions": action_urls(inc, kind),
				},
			}
			post_webhook(ch.webhook_url, body)
	elif ch.channel_type == "Raven":
		post_raven(ch.raven_channel, f"<p><strong>{escape_html(subject)}</strong></p><p>{html}</p>")


def send_email(to, subject, html, incident=None):
	frappe.sendmail(
		recipients=to,
		subject=subject,
		message=html,
		reference_doctype="Wolf Incident" if incident else None,
		reference_name=incident,
	)


def post_webhook(url, body):
	requests.post(url, json=body, timeout=10).raise_for_status()


def post_raven(channel, html):
	if "raven" not in frappe.get_installed_apps():
		frappe.throw("Raven is not installed on this site")
	frappe.get_doc(
		{"doctype": "Raven Message", "channel_id": channel, "text": html, "message_type": "Text"}
	).insert(ignore_permissions=True)
