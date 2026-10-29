"""Slack incoming webhook with Block Kit (pure).

Format: https://api.slack.com/messaging/webhooks
Blocks: https://api.slack.com/reference/block-kit/blocks
Buttons: https://api.slack.com/reference/block-kit/block-elements#button
Colour bar: blocks inside a secondary attachment,
https://api.slack.com/reference/messaging/attachments
"""

from frappe_wolf.wolf.channels import ACTION_LABEL, color, truncate

SECTION_LIMIT = 3000  # mrkdwn text in a section block
BUTTON_TEXT_LIMIT = 75
FALLBACK_LIMIT = 4000


def escape(text):
	"""Slack mrkdwn control characters: https://api.slack.com/reference/surfaces/formatting#escaping"""
	return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _button(action_id, label, url, style=None):
	button = {
		"type": "button",
		"action_id": action_id,
		"text": {"type": "plain_text", "text": truncate(label, BUTTON_TEXT_LIMIT)},
		"url": url,
	}
	if style:
		button["style"] = style
	return button


def build_payload(msg, cfg):
	headline = f"*{escape(msg['label'])}* · {escape(msg['severity'])} · "
	if msg.get("url"):
		headline += f"<{msg['url']}|{escape(msg['title'])}>"
	else:
		headline += f"*{escape(msg['title'])}*"
	details = "\n".join(escape(line) for line in msg.get("lines") or [])
	text = truncate(headline + ("\n" + details if details else ""), SECTION_LIMIT)
	blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
	buttons = []
	if msg.get("url") and msg.get("name"):
		buttons.append(_button("wolf_open", f"Open {msg['name']}", msg["url"]))
	for action, url in (msg.get("actions") or {}).items():
		style = "primary" if action == "ack" else None
		buttons.append(_button(f"wolf_{action}", ACTION_LABEL.get(action, action), url, style))
	if buttons:
		blocks.append({"type": "actions", "elements": buttons})
	attachment = {"color": color(msg), "fallback": truncate(msg["text"], FALLBACK_LIMIT), "blocks": blocks}
	return [
		{
			"method": "POST",
			"url": cfg["chat_webhook_url"],
			"json": {"text": truncate(msg["subject"], FALLBACK_LIMIT), "attachments": [attachment]},
			"action": "post",
		}
	]
