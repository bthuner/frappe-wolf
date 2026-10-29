"""ntfy publish with headers (pure).

https://docs.ntfy.sh/publish/  (Title, Priority, Tags, Click, Actions headers)
Action buttons, short format: https://docs.ntfy.sh/publish/#action-buttons
Access tokens: https://docs.ntfy.sh/publish/#access-tokens
Non-ASCII header values are sent RFC 2047 encoded, which ntfy decodes
(https://docs.ntfy.sh/publish/#message-title).
"""

import base64

from frappe_wolf.wolf.channels import ACTION_LABEL, join_url, truncate

DEFAULT_SERVER = "https://ntfy.sh"
BODY_LIMIT = 4000  # ntfy turns larger messages into attachments
PRIORITY = {"critical": 5, "warning": 4, "info": 3}
TAGS = {
	"critical": "rotating_light",
	"warning": "warning",
	"info": "information_source",
	"resolved": "white_check_mark",
	"acknowledged": "eyes",
	"test": "test_tube",
}


def priority(severity, kind):
	if kind in ("resolved", "acknowledged"):
		return 2
	if kind == "test":
		return 3
	base = PRIORITY.get(severity, 3)
	return min(base + 1, 5) if kind == "escalated" else base


def header_value(value):
	"""Plain if latin-1 safe ASCII, else an RFC 2047 encoded word."""
	value = " ".join(str(value or "").split())
	if value.isascii():
		return value
	return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


def build_payload(msg, cfg):
	kind = msg.get("kind")
	headers = {
		"Title": header_value(truncate(msg["subject"], 250)),
		"Priority": str(priority(msg.get("severity"), kind)),
		"Tags": ",".join(t for t in (TAGS.get(kind) or TAGS.get(msg.get("severity")), msg.get("severity")) if t),
	}
	if msg.get("url"):
		headers["Click"] = msg["url"]
	actions = []
	if msg.get("url"):
		actions.append(f"view, Open, {msg['url']}")
	for action, url in (msg.get("actions") or {}).items():
		actions.append(f"view, {ACTION_LABEL.get(action, action)}, {url}, clear=true")
	if actions:
		headers["Actions"] = "; ".join(actions[:3])  # ntfy allows three actions
	if cfg.get("ntfy_token"):
		headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"
	server = cfg.get("ntfy_server") or DEFAULT_SERVER
	return [
		{
			"method": "POST",
			"url": join_url(server, (cfg.get("ntfy_topic") or "").strip()),
			"headers": headers,
			"data": truncate(msg["text"], BODY_LIMIT).encode(),
			"action": "publish",
		}
	]
