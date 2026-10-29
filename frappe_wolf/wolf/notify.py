"""Alert delivery: email, webhook, Raven and the native channels (wolf/channels/).

Every attempt is recorded as a Wolf Delivery on the incident (channel, kind, ok or
error, HTTP status). Channel secrets are Password fields: they are decrypted only to
build a request and are redacted from every recorded error and logged traceback.
"""

import frappe
import requests
from frappe.utils import escape_html, get_datetime, get_url, now_datetime

from frappe_wolf.wolf import channels as wolf_channels
from frappe_wolf.wolf import conditions

KIND_LABEL = {
	"fired": "FIRING",
	"reopened": "RE-OPENED",
	"resolved": "RESOLVED",
	"escalated": "ESCALATED",
	"acknowledged": "ACKNOWLEDGED",
	"test": "TEST",
	"digest": "DIGEST",
}
FIRING = ("fired", "reopened")
SILENCEABLE = ("fired", "reopened", "resolved")
ACTIONABLE = ("fired", "reopened", "escalated")
HTTP_TIMEOUT = 10


def enqueue(incident, kind, only_stateful=False):
	"""Decide now (silences, bookkeeping), deliver in a background job.

	``only_stateful``: only PagerDuty-style channels hear about it (an acknowledgement,
	or a resolution the rule does not announce). Returns False when nothing is sent.
	"""
	from frappe_wolf.wolf import silences

	if kind in SILENCEABLE or kind == "acknowledged":
		inc = frappe.db.get_value(
			"Wolf Incident", incident, ["name", "alert_rule", "host", "bench", "site", "silenced"], as_dict=True
		)
		if kind == "resolved" and inc and inc.silenced:
			# Nobody heard about it firing, so nobody needs to hear it resolved.
			silences.release(incident, "Resolved during the maintenance window, no notification sent.")
			return False
		if kind == "acknowledged" and (not inc or inc.silenced):
			return False
		silence = silences.active_for(inc) if inc else None
		if silence:
			if kind == "resolved":
				silences.hold(incident, silence, kind)
				# The on-call tool heard the trigger: close the alert there, hold the rest.
				only_stateful = True
			elif kind != "acknowledged":
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
		only_stateful=only_stateful,
		enqueue_after_commit=True,
	)
	return True


def on_status_change(incident, alert_rule, status):
	"""A person acknowledged or resolved an incident.

	Stateful (PagerDuty-style) channels always hear it, so the alert closes on call too.
	A resolution reaches the other channels only when the rule notifies on resolve.
	"""
	rule = frappe.db.get_value("Wolf Alert Rule", alert_rule, ["name", "notify_on_resolve"], as_dict=True)
	if not rule:
		return False
	stateful = has_stateful(alert_rule)
	if status == "Acknowledged":
		return enqueue(incident, "acknowledged") if stateful else False
	if status == "Resolved":
		announce = rule.notify_on_resolve is None or bool(rule.notify_on_resolve)
		if announce or stateful:
			return enqueue(incident, "resolved", only_stateful=not announce)
	return False


def has_stateful(alert_rule):
	return bool(
		frappe.db.exists(
			"Wolf Alert Channel",
			{
				"parenttype": "Wolf Alert Rule",
				"parent": alert_rule,
				"enabled": 1,
				"channel_type": ("in", wolf_channels.STATEFUL),
			},
		)
	)


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


def _lines(inc, kind):
	label = KIND_LABEL.get(kind, kind.upper())
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
	if kind == "acknowledged" and inc.get("acknowledged_by"):
		lines.append(f"Acknowledged by {inc.acknowledged_by}")
	if kind == "resolved" and inc.get("resolved_by"):
		lines.append(f"Resolved by {inc.resolved_by}")
	return lines


def render(inc, kind, actor=None):
	label = KIND_LABEL.get(kind, kind.upper())
	subject = f"[Wolf] {label} {inc.severity.upper()}: {inc.title}"
	url = incident_url(inc.name)
	lines = _lines(inc, kind)
	actions = action_urls(inc, kind, actor)
	html = "<br>".join(escape_html(line) for line in lines) + f'<br><a href="{escape_html(url)}">{escape_html(inc.name)}</a>'
	if actions:
		html += "<br>" + " · ".join(
			f'<a href="{escape_html(u)}">{"Acknowledge" if a == "ack" else "Resolve"}</a>' for a, u in actions.items()
		)
	lines.append(url)
	lines += [f"{'Acknowledge' if a == 'ack' else 'Resolve'}: {u}" for a, u in actions.items()]
	return subject, "\n".join(lines), html


def message(inc, kind, rendered=None):
	"""The notification as a plain dict, input of the native channel builders."""
	subject, text, html = rendered or render(inc, kind)
	return {
		"kind": kind,
		"label": KIND_LABEL.get(kind, kind.upper()),
		"severity": inc.severity,
		"status": inc.status,
		"title": inc.title,
		"name": inc.name,
		"alert_rule": inc.alert_rule,
		"subject": subject,
		"subject_html": escape_html(subject),
		"text": text,
		"html": html,
		"url": incident_url(inc.name),
		"scope": scope_label(inc),
		"summary": inc.summary,
		"started_at": _ts(inc.started_at),
		"site": inc.site,
		"bench": inc.bench,
		"host": inc.host,
		"lines": _lines(inc, kind)[1:],
		"actions": action_urls(inc, kind),
	}


def _ts(value):
	"""Datetime to the second, in the site's time zone (no microseconds in alerts)."""
	return get_datetime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def recipients(value):
	return [r.strip() for r in (value or "").replace(",", "\n").splitlines() if r.strip()]


# --- channel configuration ---------------------------------------------------------


def _secret(ch, field):
	if hasattr(ch, "get_password"):
		try:
			return ch.get_password(field, raise_exception=False) or ""
		except Exception:
			return ""
	return ch.get(field) or ""


def channel_config(ch):
	"""Plain dict of a native channel's settings, secrets decrypted."""
	ctype = ch.get("channel_type")
	secrets = wolf_channels.SECRETS.get(ctype, ())
	return {f: (_secret(ch, f) if f in secrets else ch.get(f)) for f in wolf_channels.FIELDS.get(ctype, ())}


def _safe_config(ch):
	try:
		return channel_config(ch)
	except Exception:
		return {}


def channel_identity(ch, cfg=None):
	ctype = ch.get("channel_type")
	if ctype in wolf_channels.NATIVE:
		return wolf_channels.identity(ctype, cfg if cfg is not None else channel_config(ch))
	return f"{ctype}:{conditions.channel_destination(ch)}"


def channel_target(ch, cfg=None):
	"""Secret-free description of where a channel delivers."""
	ctype = ch.get("channel_type")
	if ctype in wolf_channels.NATIVE:
		return wolf_channels.target_label(ctype, cfg if cfg is not None else channel_config(ch))
	if ctype == "Webhook":
		return wolf_channels.target_label("Slack", {"chat_webhook_url": ch.get("webhook_url")}) or "webhook"
	if ctype == "Email":
		return ", ".join(recipients(ch.get("recipients")))
	return ch.get("raven_channel") or ""


def _sensitive(ch, cfg):
	"""(secrets, urls) to scrub from errors for this channel."""
	ctype = ch.get("channel_type")
	secrets = [cfg.get(f) for f in wolf_channels.SECRETS.get(ctype, ())]
	# Other URLs lose their path and query in redact(); webhook URLs are secrets whole.
	urls = [ch.get("webhook_url")] if ctype == "Webhook" else []
	return secrets, urls


# --- sending -----------------------------------------------------------------------


def send(incident, kind, channels=None, only_stateful=False):
	"""Deliver one notification. Returns one result per attempt (for the test button)."""
	from frappe_wolf.wolf import digest

	inc = frappe.get_doc("Wolf Incident", incident)
	if channels is None:
		if not inc.alert_rule:
			return []
		rule = frappe.get_doc("Wolf Alert Rule", inc.alert_rule)
		channels = wolf_channels.select(
			kind,
			rule.channels,
			rule.get("escalation_channels"),
			only_stateful=only_stateful,
			escalated=bool(inc.escalated_at),
			key=channel_identity,
		)
	else:
		channels = [c for c in channels if c.enabled]
	rendered = render(inc, kind)
	results = []
	for ch in channels:
		stateful = ch.channel_type in wolf_channels.STATEFUL
		try:
			if not stateful and conditions.should_digest(ch.get("digest"), inc.severity, kind):
				digest.queue(ch, inc, kind)
				continue
		except Exception:
			_log_failure(ch, _safe_config(ch), f"[Wolf] {ch.channel_type} digest queueing failed for {inc.name}")
			continue
		try:
			results += attempt(ch, inc, kind, rendered)
		except Exception:
			_log_failure(ch, _safe_config(ch), f"[Wolf] {ch.channel_type} notification failed for {inc.name}")
	return results


def attempt(ch, inc, kind, rendered):
	"""Send to one channel and record the outcome of each request."""
	ctype = ch.channel_type
	cfg = channel_config(ch) if ctype in wolf_channels.NATIVE else {}
	target = channel_target(ch, cfg)
	if ctype in wolf_channels.NATIVE:
		msg = message(inc, kind, rendered)
		msg["attempt_id"] = frappe.generate_hash(length=16)
		outcomes = send_native(ch, cfg, msg)
	else:
		outcomes = [_legacy(ch, inc, kind, rendered)]
	for out in outcomes:
		out.update(channel_type=ctype, target=target, kind=kind)
		record(inc.name, inc.alert_rule, out)
		if not out["ok"]:
			_log_failure(ch, cfg, f"[Wolf] {ctype} notification failed for {inc.name}", out["error"])
	return outcomes


def _legacy(ch, inc, kind, rendered):
	try:
		status = deliver(ch, inc, kind, *rendered)
		return {"ok": 1, "http_status": status, "error": None, "action": None}
	except Exception as e:
		return _failure(ch, {}, e)


def send_native(ch, cfg, msg):
	ctype = ch.get("channel_type")
	missing = wolf_channels.missing(ctype, cfg)
	if missing:
		return [{"ok": 0, "http_status": None, "action": None, "error": f"Not configured: {', '.join(missing)}"}]
	try:
		reqs = wolf_channels.builder(ctype)(msg, cfg)
	except Exception as e:
		return [_failure(ch, cfg, e)]
	outcomes = []
	for req in reqs:
		try:
			outcomes.append({"ok": 1, "http_status": http_request(req), "error": None, "action": req.get("action")})
		except Exception as e:
			outcomes.append(_failure(ch, cfg, e, req.get("action")))
	return outcomes


def http_request(req):
	kwargs = {"headers": req.get("headers") or {}, "timeout": HTTP_TIMEOUT}
	if "json" in req:
		kwargs["json"] = req["json"]
	elif "data" in req:
		kwargs["data"] = req["data"]
	response = requests.request(req["method"], req["url"], **kwargs)
	response.raise_for_status()
	return response.status_code


def _failure(ch, cfg, exc, action=None):
	response = getattr(exc, "response", None)
	status = getattr(response, "status_code", None)
	if status is not None:
		body = (getattr(response, "text", "") or "").strip()
		error = f"HTTP {status}" + (f": {body[:300]}" if body else "")
	else:
		error = f"{type(exc).__name__}: {exc}"
	secrets, urls = _sensitive(ch, cfg)
	return {"ok": 0, "http_status": status, "action": action, "error": wolf_channels.redact(error, secrets, urls)[:1000]}


def _log_failure(ch, cfg, title, error=None):
	"""Error Log entry with the traceback scrubbed of the channel's secrets."""
	secrets, urls = _sensitive(ch, cfg)
	trace = frappe.get_traceback() if error is None else ""
	text = "\n".join(x for x in (error, trace) if x and x.strip() != "NoneType: None")
	frappe.log_error(title=title, message=wolf_channels.redact(text, secrets, urls))


def record(incident, alert_rule, out, digest=False):
	"""One Wolf Delivery row. ``out`` carries no secret (errors are redacted)."""
	if not incident:
		return
	try:
		frappe.get_doc(
			{
				"doctype": "Wolf Delivery",
				"incident": incident,
				"alert_rule": alert_rule,
				"at": now_datetime(),
				"channel_type": out.get("channel_type"),
				"target": (out.get("target") or "")[:140],
				"kind": out.get("kind"),
				"action": out.get("action"),
				"ok": 1 if out.get("ok") else 0,
				"http_status": out.get("http_status"),
				"error": out.get("error"),
				"digest": 1 if digest else 0,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title=f"[Wolf] Could not record a delivery for {incident}")


def deliver(ch, inc, kind, subject, text, html):
	"""Email, Webhook and Raven. Returns the HTTP status for webhooks."""
	if ch.channel_type == "Email":
		to = recipients(ch.recipients)
		if not to:
			raise ValueError("Not configured: Recipients")
		if not action_urls(inc, kind):
			send_email(to, subject, html, inc.name)
			return None
		# One message per recipient so an acknowledgement can name who clicked.
		for address in to:
			s, _t, h = render(inc, kind, actor=address)
			send_email([address], s, h, inc.name)
	elif ch.channel_type == "Webhook":
		if not ch.webhook_url:
			raise ValueError("Not configured: Webhook URL")
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
		return post_webhook(ch.webhook_url, body)
	elif ch.channel_type == "Raven":
		post_raven(ch.raven_channel, f"<p><strong>{escape_html(subject)}</strong></p><p>{html}</p>")
	return None


def send_email(to, subject, html, incident=None):
	frappe.sendmail(
		recipients=to,
		subject=subject,
		message=html,
		reference_doctype="Wolf Incident" if incident else None,
		reference_name=incident,
	)


def post_webhook(url, body):
	response = requests.post(url, json=body, timeout=HTTP_TIMEOUT)
	response.raise_for_status()
	return response.status_code


def post_raven(channel, html):
	if "raven" not in frappe.get_installed_apps():
		frappe.throw("Raven is not installed on this site")
	frappe.get_doc(
		{"doctype": "Raven Message", "channel_id": channel, "text": html, "message_type": "Text"}
	).insert(ignore_permissions=True)
