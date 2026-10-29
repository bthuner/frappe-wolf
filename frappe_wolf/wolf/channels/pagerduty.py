"""PagerDuty Events API v2 and compatible receivers (pure).

https://developer.pagerduty.com/docs/events-api-v2/trigger-events/
https://developer.pagerduty.com/docs/events-api-v2/acknowledge-events/
https://developer.pagerduty.com/docs/events-api-v2/resolve-events/
The same body is accepted by Events-v2 compatible endpoints (Grafana OnCall, Opsgenie
and others expose one), hence the configurable URL.

One Wolf incident is one alert: ``dedup_key`` is the incident name, so a re-trigger
(reopen, escalation) updates the same alert and acknowledge / resolve close it.
"""

from frappe_wolf.wolf.channels import truncate

DEFAULT_URL = "https://events.pagerduty.com/v2/enqueue"
SUMMARY_LIMIT = 1024
SEVERITY = {"critical": "critical", "warning": "warning", "info": "info"}
EVENT_ACTION = {
	"fired": "trigger",
	"reopened": "trigger",
	"escalated": "trigger",
	"test": "trigger",
	"acknowledged": "acknowledge",
	"resolved": "resolve",
}


def event_action(kind):
	return EVENT_ACTION.get(kind)


def build_event(msg, cfg, action):
	body = {"routing_key": cfg["pagerduty_routing_key"], "event_action": action, "dedup_key": msg["name"]}
	if action != "trigger":
		return body
	details = {k: msg.get(k) for k in ("scope", "summary", "started_at", "site", "bench", "host", "url") if msg.get(k)}
	details["kind"] = msg.get("kind")
	body["payload"] = {
		"summary": truncate(f"{msg['title']} ({msg.get('scope') or 'all'})", SUMMARY_LIMIT),
		"source": msg.get("host") or msg.get("site") or msg.get("bench") or "frappe-wolf",
		"severity": SEVERITY.get(msg.get("severity"), "error"),
		"component": msg.get("site") or msg.get("bench") or None,
		"group": msg.get("bench") or None,
		"class": msg.get("alert_rule") or None,
		"custom_details": details,
	}
	body["payload"] = {k: v for k, v in body["payload"].items() if v is not None}
	body["client"] = "Frappe Wolf"
	if msg.get("url"):
		body["client_url"] = msg["url"]
		body["links"] = [{"href": msg["url"], "text": f"Wolf incident {msg['name']}"}]
	return body


def build_payload(msg, cfg):
	url = (cfg.get("pagerduty_url") or "").strip() or DEFAULT_URL
	action = event_action(msg.get("kind"))
	if not action:
		return []
	actions = [action]
	if msg.get("kind") == "test":
		actions.append("resolve")  # a test must not leave an alert open on call
	return [{"method": "POST", "url": url, "json": build_event(msg, cfg, a), "action": a} for a in actions]
