"""Mattermost incoming webhook with a message attachment (pure).

Format: https://developers.mattermost.com/integrate/webhooks/incoming/
Attachments: https://developers.mattermost.com/integrate/reference/message-attachments/
Interactive buttons need an integration URL on the Wolf side, so the acknowledge /
resolve actions are Markdown links in the attachment text.
"""

from frappe_wolf.wolf.channels import ACTION_LABEL, color, truncate

TEXT_LIMIT = 7000  # well under the 16383 character post limit


def build_payload(msg, cfg):
	text = "\n".join(msg.get("lines") or [])
	links = [f"[{ACTION_LABEL.get(a, a)}]({u})" for a, u in (msg.get("actions") or {}).items()]
	if links:
		text += "\n\n" + " · ".join(links)
	attachment = {
		"fallback": msg["subject"],
		"color": color(msg),
		"pretext": f"**{msg['label']}** · {msg['severity']}",
		"title": msg["title"],
		"text": truncate(text, TEXT_LIMIT),
		"fields": [
			{"short": True, "title": "Severity", "value": msg["severity"]},
			{"short": True, "title": "Scope", "value": msg.get("scope") or "all"},
		],
		"footer": f"Frappe Wolf · {msg['name']}" if msg.get("name") else "Frappe Wolf",
	}
	if msg.get("url"):
		attachment["title_link"] = msg["url"]
	return [{"method": "POST", "url": cfg["chat_webhook_url"], "json": {"attachments": [attachment]}, "action": "post"}]
