"""Gotify message (pure).

POST /message with the application token: https://gotify.net/api-docs#/message/createMessage
Extras (click URL, content type): https://gotify.net/docs/msgextras
Priorities as the Android client reads them (https://gotify.net/docs/faq):
0 no notification, 1-3 silent, 4-7 sound, 8-10 sound and pop-up.
"""

from frappe_wolf.wolf.channels import join_url, truncate

PRIORITY = {"critical": 8, "warning": 5, "info": 3}


def priority(severity, kind):
	if kind in ("resolved", "acknowledged"):
		return 2
	if kind == "test":
		return 4
	base = PRIORITY.get(severity, 3)
	return min(base + 2, 10) if kind == "escalated" else base


def build_payload(msg, cfg):
	body = {
		"title": truncate(msg["subject"], 250),
		"message": msg["text"],
		"priority": priority(msg.get("severity"), msg.get("kind")),
		"extras": {"client::display": {"contentType": "text/plain"}},
	}
	if msg.get("url"):
		body["extras"]["client::notification"] = {"click": {"url": msg["url"]}}
	return [
		{
			"method": "POST",
			"url": join_url(cfg["gotify_server"], "message"),
			"headers": {"X-Gotify-Key": cfg["gotify_token"]},
			"json": body,
			"action": "message",
		}
	]
