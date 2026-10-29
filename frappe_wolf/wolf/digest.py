"""Hourly digests: info/warning notifications batched per destination."""

import frappe
from frappe.utils import add_to_date, escape_html, now_datetime

from frappe_wolf.wolf import conditions, notify

KEEP_FAILED_HOURS = 24


def queue(ch, inc, kind):
	frappe.get_doc(
		{
			"doctype": "Wolf Digest Item",
			"incident": inc.name,
			"kind": kind,
			"severity": inc.severity,
			"title": (inc.title or "")[:255],
			"alert_rule": inc.alert_rule,
			"channel_type": ch.channel_type,
			"destination": conditions.channel_destination(ch),
			"queued_at": now_datetime(),
		}
	).insert(ignore_permissions=True)


def send_all():
	items = frappe.get_all(
		"Wolf Digest Item",
		fields=["name", "incident", "kind", "severity", "title", "channel_type", "destination", "queued_at"],
		order_by="queued_at asc",
		limit_page_length=5000,
	)
	stale = add_to_date(now_datetime(), hours=-KEEP_FAILED_HOURS)
	for (channel_type, destination), group in conditions.group_digest(items).items():
		try:
			deliver(channel_type, destination, group)
			done = group
		except Exception:
			frappe.log_error(title=f"[Wolf] {channel_type} digest failed")
			done = [i for i in group if i.queued_at < stale]
		for item in done:
			frappe.delete_doc("Wolf Digest Item", item.name, ignore_permissions=True, force=True)
		frappe.db.commit()
	# Items without a destination can never be sent.
	frappe.db.sql("delete from `tabWolf Digest Item` where coalesce(destination, '') = ''")
	frappe.db.commit()


def render(items):
	subject = f"[Wolf] Digest: {len(items)} notification{'s' if len(items) != 1 else ''}"
	lines = []
	for i in items:
		label = notify.KIND_LABEL.get(i.kind, (i.kind or "").upper())
		lines.append(f"{notify._ts(i.queued_at)[11:16]} {label} {i.severity} · {i.title} · {notify.incident_url(i.incident)}")
	text = subject + "\n\n" + "\n".join(lines)
	html = f"<p><strong>{escape_html(subject)}</strong></p><ul>" + "".join(
		f"<li>{escape_html(line.rsplit(' · ', 1)[0])} · "
		f'<a href="{escape_html(notify.incident_url(i.incident))}">{escape_html(i.incident)}</a></li>'
		for line, i in zip(lines, items, strict=True)
	) + "</ul>"
	return subject, text, html


def deliver(channel_type, destination, items):
	subject, text, html = render(items)
	if channel_type == "Email":
		notify.send_email(destination.splitlines(), subject, html)
	elif channel_type == "Webhook":
		notify.post_webhook(
			destination,
			{
				"text": text,
				"digest": [
					{
						"incident": i.incident,
						"kind": i.kind,
						"severity": i.severity,
						"title": i.title,
						"at": notify._ts(i.queued_at),
						"url": notify.incident_url(i.incident),
					}
					for i in items
				],
			},
		)
	elif channel_type == "Raven":
		notify.post_raven(destination, html)
