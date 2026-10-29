"""Alert delivery: email, webhook (Slack/Mattermost compatible) and Raven."""

import frappe
import requests
from frappe.utils import escape_html, get_url

KIND_LABEL = {"fired": "FIRING", "reopened": "RE-OPENED", "resolved": "RESOLVED", "test": "TEST"}


def enqueue(incident, kind):
	frappe.enqueue(
		"frappe_wolf.wolf.notify.send",
		queue="short",
		incident=incident,
		kind=kind,
		enqueue_after_commit=True,
	)


def incident_url(name):
	base = frappe.db.get_single_value("Wolf Settings", "public_url")
	path = f"/app/wolf-incident/{name}"
	return base.rstrip("/") + path if base else get_url(path)


def render(inc, kind):
	scope = " / ".join(x for x in (inc.site, inc.bench, inc.host) if x) or "all"
	label = KIND_LABEL.get(kind, kind.upper())
	subject = f"[Wolf] {label} {inc.severity.upper()}: {inc.title}"
	url = incident_url(inc.name)
	lines = [
		f"{label} · {inc.severity} · {inc.title}",
		f"Scope: {scope}",
		f"Started: {inc.started_at}",
	]
	if inc.summary:
		lines.append(f"Details: {inc.summary}")
	if inc.deployment_at:
		lines.append(f"Deployment shortly before: {inc.deployment_at}")
	lines.append(url)
	text = "\n".join(lines)
	html = "<br>".join(escape_html(line) for line in lines[:-1]) + f'<br><a href="{escape_html(url)}">{escape_html(inc.name)}</a>'
	return subject, text, html


def recipients(value):
	return [r.strip() for r in (value or "").replace(",", "\n").splitlines() if r.strip()]


def send(incident, kind, channels=None):
	inc = frappe.get_doc("Wolf Incident", incident)
	if channels is None:
		if not inc.alert_rule:
			return
		channels = frappe.get_doc("Wolf Alert Rule", inc.alert_rule).channels
	subject, text, html = render(inc, kind)
	for ch in channels:
		if not ch.enabled:
			continue
		try:
			deliver(ch, inc, kind, subject, text, html)
		except Exception:
			frappe.log_error(title=f"[Wolf] {ch.channel_type} notification failed for {inc.name}")


def deliver(ch, inc, kind, subject, text, html):
	if ch.channel_type == "Email":
		to = recipients(ch.recipients)
		if to:
			frappe.sendmail(
				recipients=to,
				subject=subject,
				message=html,
				reference_doctype="Wolf Incident",
				reference_name=inc.name,
			)
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
					"started_at": str(inc.started_at),
					"url": incident_url(inc.name),
				},
			}
			requests.post(ch.webhook_url, json=body, timeout=10).raise_for_status()
	elif ch.channel_type == "Raven":
		if "raven" not in frappe.get_installed_apps():
			frappe.throw("Raven is not installed on this site")
		frappe.get_doc(
			{
				"doctype": "Raven Message",
				"channel_id": ch.raven_channel,
				"text": f"<p><strong>{escape_html(subject)}</strong></p><p>{html}</p>",
				"message_type": "Text",
			}
		).insert(ignore_permissions=True)
