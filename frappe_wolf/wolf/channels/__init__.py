"""Native notification channels (pure, no frappe imports, unit tested).

Each channel module exposes ``build_payload(msg, cfg)`` which turns a rendered
notification (``msg``, see ``notify.message``) and the channel configuration
(``cfg``: plain dict, secrets already decrypted) into a list of HTTP requests:

    {"method": "POST", "url": "...", "headers": {...}, "json": {...} | "data": "...", "action": "..."}

``frappe_wolf.wolf.notify`` sends the requests and records each attempt; nothing
here touches the network, the database or the logs.
"""

import hashlib
import json
import re
from urllib.parse import quote, urlsplit

LEGACY = ("Email", "Webhook", "Raven")
NATIVE = ("Slack", "Mattermost", "Matrix", "ntfy", "Gotify", "SMS", "PagerDuty")
TYPES = LEGACY + NATIVE

# Channels that track the incident state on their side (trigger / acknowledge / resolve).
# They hear about acknowledgements and every resolution, and are never digested.
STATEFUL = ("PagerDuty",)

# Wolf Alert Channel fields per native type. Secrets are Password fields: decrypted
# only to build the request, redacted from every error and log.
FIELDS = {
	"Slack": ("chat_webhook_url",),
	"Mattermost": ("chat_webhook_url",),
	"Matrix": ("matrix_homeserver", "matrix_room_id", "matrix_access_token"),
	"ntfy": ("ntfy_server", "ntfy_topic", "ntfy_token"),
	"Gotify": ("gotify_server", "gotify_token"),
	"SMS": (
		"sms_numbers",
		"sms_url",
		"sms_method",
		"sms_content_type",
		"sms_body",
		"sms_auth_header",
		"sms_secret",
		"sms_max_length",
	),
	"PagerDuty": ("pagerduty_url", "pagerduty_routing_key"),
}
SECRETS = {
	"Slack": ("chat_webhook_url",),
	"Mattermost": ("chat_webhook_url",),
	"Matrix": ("matrix_access_token",),
	"ntfy": ("ntfy_token",),
	"Gotify": ("gotify_token",),
	"SMS": ("sms_secret",),
	"PagerDuty": ("pagerduty_routing_key",),
}

# Required settings per native type, with the labels shown when one is missing.
REQUIRED = {
	"Slack": {"chat_webhook_url": "Webhook URL"},
	"Mattermost": {"chat_webhook_url": "Webhook URL"},
	"Matrix": {"matrix_homeserver": "Homeserver URL", "matrix_room_id": "Room ID", "matrix_access_token": "Access Token"},
	"ntfy": {"ntfy_topic": "Topic"},
	"Gotify": {"gotify_server": "Gotify Server URL", "gotify_token": "Application Token"},
	"SMS": {"sms_numbers": "Phone Numbers", "sms_url": "Gateway URL"},
	"PagerDuty": {"pagerduty_routing_key": "Routing Key"},
}
# Settings that must be http(s) URLs when set.
URL_FIELDS = ("chat_webhook_url", "matrix_homeserver", "ntfy_server", "gotify_server", "sms_url", "pagerduty_url")

COLORS = {"critical": "#D72B3F", "warning": "#E8A317", "info": "#2B8CD7"}
KIND_COLORS = {"resolved": "#2EB67D", "acknowledged": "#7A7A7A", "test": "#7A7A7A"}
ACTION_LABEL = {"ack": "Acknowledge", "resolve": "Resolve"}
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def builder(channel_type):
	"""build_payload function of a native channel type, or None."""
	from frappe_wolf.wolf.channels import gotify, matrix, mattermost, ntfy, pagerduty, slack, sms

	return {
		"Slack": slack.build_payload,
		"Mattermost": mattermost.build_payload,
		"Matrix": matrix.build_payload,
		"ntfy": ntfy.build_payload,
		"Gotify": gotify.build_payload,
		"SMS": sms.build_payload,
		"PagerDuty": pagerduty.build_payload,
	}.get(channel_type)


def missing(channel_type, cfg):
	"""Labels of required settings left empty."""
	return [label for field, label in REQUIRED.get(channel_type, {}).items() if not str(cfg.get(field) or "").strip()]


def invalid_urls(cfg):
	"""Fields holding something other than an http(s) URL."""
	bad = []
	for field in URL_FIELDS:
		value = str(cfg.get(field) or "").strip()
		if value and not value.startswith("*") and not re.match(r"https?://[^/\s]+", value):
			bad.append(field)
	return bad


def color(msg):
	return KIND_COLORS.get(msg.get("kind")) or COLORS.get(msg.get("severity"), COLORS["info"])


def truncate(text, limit, ellipsis="…"):
	"""Cut text to at most limit characters, on a word boundary when one is close."""
	text = text or ""
	if limit <= 0:
		return ""
	if len(text) <= limit:
		return text
	room = limit - len(ellipsis)
	if room <= 0:
		return ellipsis[:limit]
	cut = text[:room]
	space = len(cut) if text[room] == " " else cut.rfind(" ")
	if space >= room * 0.6:
		cut = cut[:space]
	return cut.rstrip(" ,.;:·-") + ellipsis


def join_url(base, path):
	return (base or "").strip().rstrip("/") + "/" + path.lstrip("/")


def worst_severity(severities):
	return max((s for s in severities if s), key=lambda s: SEVERITY_ORDER.get(s, 0), default="info")


def identity(channel_type, cfg):
	"""Stable, secret-free key for a native channel: merges digests across rules.

	The hash covers the secrets too (two Slack webhooks are two destinations), but a
	SHA-256 of a high-entropy token reveals nothing about it.
	"""
	fields = FIELDS.get(channel_type, ())
	canonical = json.dumps([channel_type] + [str(cfg.get(f) or "").strip() for f in fields])
	return "h:" + hashlib.sha256(canonical.encode()).hexdigest()[:40]


def _host(url):
	try:
		return urlsplit((url or "").strip()).hostname or ""
	except ValueError:
		return ""


def mask_number(number):
	digits = re.sub(r"\D", "", number or "")
	return ("…" + digits[-4:]) if len(digits) > 4 else "…"


def target_label(channel_type, cfg):
	"""Where a channel delivers, without secrets (shown on the incident)."""
	if channel_type in ("Slack", "Mattermost"):
		return _host(cfg.get("chat_webhook_url")) or channel_type
	if channel_type == "Matrix":
		return f"{cfg.get('matrix_room_id') or '?'} on {_host(cfg.get('matrix_homeserver')) or '?'}"
	if channel_type == "ntfy":
		return f"{_host(cfg.get('ntfy_server') or 'https://ntfy.sh')}/{cfg.get('ntfy_topic') or '?'}"
	if channel_type == "Gotify":
		return _host(cfg.get("gotify_server")) or "Gotify"
	if channel_type == "SMS":
		numbers = recipients(cfg.get("sms_numbers"))
		return f"{_host(cfg.get('sms_url')) or 'SMS'} → " + ", ".join(mask_number(n) for n in numbers)
	if channel_type == "PagerDuty":
		return _host(cfg.get("pagerduty_url") or "https://events.pagerduty.com") or "PagerDuty"
	return channel_type or ""


def recipients(value):
	return [r.strip() for r in (value or "").replace(",", "\n").splitlines() if r.strip()]


_URL = re.compile(r"(https?://[^/\s'\"<>]+)[^\s'\"<>]*")
_URLLIB3_PATH = re.compile(r"(url: )\S+")


def redact(text, secrets=(), urls=()):
	"""Remove secrets and URL paths/queries from an error before it is stored or logged.

	Webhook URLs (Slack, Mattermost), ntfy topics and SMS gateway queries carry
	credentials in the path, so only scheme and host survive.
	"""
	text = str(text or "")
	values = set()
	for value in list(secrets) + list(urls):
		value = str(value or "").strip()
		if len(value) < 4:
			continue
		values.update({value, quote(value, safe=""), quote(value)})
		# A receiver may echo only the path of a webhook URL back in its error body.
		parts = urlsplit(value) if "://" in value else None
		if parts and parts.path and len(parts.path) > 1:
			values.add(parts.path + (f"?{parts.query}" if parts.query else ""))
			values.add(parts.path)
	for value in sorted(values, key=len, reverse=True):
		text = text.replace(value, "***")
	text = _URL.sub(r"\1/***", text)
	return _URLLIB3_PATH.sub(r"\1***", text)


def _unique(rows, key):
	seen, out = set(), []
	for row in rows:
		k = key(row) if key else id(row)
		if k in seen:
			continue
		seen.add(k)
		out.append(row)
	return out


def select(kind, channels, escalation_channels=(), only_stateful=False, escalated=False, key=None):
	"""Channels (rows) that hear about this kind of notification.

	- fired / reopened: the rule's channels.
	- escalated: the escalation channels.
	- acknowledged: stateful channels only (PagerDuty-style), from the channels and,
	  once escalated, the escalation channels.
	- resolved: the channels (only the stateful ones when ``only_stateful``, i.e. the
	  rule does not notify on resolve), plus the stateful escalation channels once escalated.

	``key(row)`` identifies a destination so a PagerDuty service listed in both tables
	hears an acknowledgement or resolution once.
	"""
	channels = [c for c in channels or [] if c.get("enabled")]
	escalation = [c for c in escalation_channels or [] if c.get("enabled")]
	stateful = [c for c in channels if c.get("channel_type") in STATEFUL]
	stateful_escalation = [c for c in escalation if c.get("channel_type") in STATEFUL] if escalated else []
	if kind == "escalated":
		return escalation
	if kind == "acknowledged":
		return _unique(stateful + stateful_escalation, key)
	if kind == "resolved":
		return _unique((stateful if only_stateful else channels) + stateful_escalation, key)
	return channels
