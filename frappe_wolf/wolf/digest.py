"""Hourly digests: info/warning notifications batched per destination."""

import frappe
from frappe.utils import add_to_date, escape_html, now_datetime

from frappe_wolf.wolf import channels as wolf_channels
from frappe_wolf.wolf import conditions, notify

KEEP_FAILED_HOURS = 24


def destination_of(ch):
	"""Legacy channels merge on their address; native ones on a secret-free hash."""
	if ch.channel_type in wolf_channels.NATIVE:
		return notify.channel_identity(ch)
	return conditions.channel_destination(ch)


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
			"destination": destination_of(ch),
			"channel_row": ch.name if ch.channel_type in wolf_channels.NATIVE else None,
			"queued_at": now_datetime(),
		}
	).insert(ignore_permissions=True)


def send_all():
	items = frappe.get_all(
		"Wolf Digest Item",
		fields=[
			"name", "incident", "kind", "severity", "title", "alert_rule",
			"channel_type", "destination", "channel_row", "queued_at",
		],
		order_by="queued_at asc",
		limit_page_length=5000,
	)
	stale = add_to_date(now_datetime(), hours=-KEEP_FAILED_HOURS)
	for (channel_type, destination), group in conditions.group_digest(items).items():
		try:
			deliver(channel_type, destination, group)
			done = group
		except Exception:
			# Webhook destinations are URLs that may embed a token: log them redacted.
			urls = [destination] if channel_type == "Webhook" else []
			frappe.log_error(
				title=f"[Wolf] {channel_type} digest failed",
				message=wolf_channels.redact(frappe.get_traceback(), urls=urls),
			)
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
	if channel_type in wolf_channels.NATIVE:
		deliver_native(channel_type, items, subject, text, html)
		return
	ch = frappe._dict(
		channel_type=channel_type, recipients=destination, webhook_url=destination, raven_channel=destination
	)
	target = notify.channel_target(ch)
	try:
		status = deliver_legacy(channel_type, destination, items, subject, text, html)
	except Exception as e:
		record_group(items, channel_type, target, notify._failure(ch, {}, e))
		raise
	record_group(items, channel_type, target, {"ok": 1, "http_status": status, "error": None, "action": None})


def deliver_legacy(channel_type, destination, items, subject, text, html):
	if channel_type == "Email":
		notify.send_email(destination.splitlines(), subject, html)
	elif channel_type == "Webhook":
		return notify.post_webhook(
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
	return None


def record_group(items, channel_type, target, out):
	"""One Wolf Delivery per incident of a digest."""
	for item in {i.incident: i for i in items}.values():
		notify.record(
			item.incident,
			item.get("alert_rule"),
			{**out, "channel_type": channel_type, "target": target, "kind": item.kind},
			digest=True,
		)


def channel_row(items):
	"""The Wolf Alert Channel row a native digest is sent with (secrets live there)."""
	for name in dict.fromkeys(i.get("channel_row") for i in items if i.get("channel_row")):
		if frappe.db.exists("Wolf Alert Channel", name):
			return frappe.get_doc("Wolf Alert Channel", name)
	return None


def deliver_native(channel_type, items, subject, text, html):
	ch = channel_row(items)
	if not ch:
		# The channel was removed from its rule: nothing left to send with.
		return
	cfg = notify.channel_config(ch)
	lines = [line for line in text.split("\n\n", 1)[-1].splitlines() if line]
	msg = {
		"kind": "digest",
		"label": notify.KIND_LABEL["digest"],
		"severity": wolf_channels.worst_severity(i.severity for i in items),
		"status": "",
		"title": subject.replace("[Wolf] Digest: ", ""),
		"name": None,
		"alert_rule": None,
		"subject": subject,
		"subject_html": escape_html(subject),
		"text": text,
		"html": html,
		"url": notify.public_base() + "/app/wolf-overview",
		"scope": "",
		"lines": lines,
		"actions": {},
		"attempt_id": frappe.generate_hash(length=16),
	}
	outcomes = notify.send_native(ch, cfg, msg)
	failed = [o for o in outcomes if not o["ok"]]
	summary = failed[0] if failed else (outcomes[0] if outcomes else {"ok": 1})
	if failed and len(failed) < len(outcomes):
		summary = {**summary, "ok": 1, "error": f"{len(failed)} of {len(outcomes)} requests failed: {summary['error']}"}
	record_group(items, channel_type, notify.channel_target(ch, cfg), summary)
	if outcomes and len(failed) == len(outcomes):
		raise DigestFailed(failed[0]["error"])


class DigestFailed(Exception):
	"""Every request of a native digest failed; the error is already redacted."""
