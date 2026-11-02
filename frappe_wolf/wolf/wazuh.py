"""A Wazuh alert → a Wolf `hids_alert` event, for the integration route (no frappe imports).

The Wolf agent on the manager's host reads `alerts.json` itself (agent/wolf_agent/parsers/
wazuh.py). When no Wolf agent runs there, Wazuh's own integration daemon calls
`agent/wazuh/custom-frappe-wolf` for each alert at or above the `<level>` set in ossec.conf.
That script posts the alert as it is to `api/wazuh.alert`, which maps it here. The mapping is the
agent parser's, one alert at a time; repeats are merged on the site by fingerprint.
"""

import ipaddress
from datetime import datetime, timezone

LOOP_GROUP = "frappe_wolf"  # Wazuh's alerts on Wolf's own SIEM export: never brought back
EXCLUDED_GROUPS = {LOOP_GROUP, "sca"}  # configuration assessment is posture, not intrusion


def severity(level):
	return "critical" if level >= 12 else "warning" if level >= 7 else "info"


def host_of(alert):
	agent = alert.get("agent") or {}
	if agent.get("id") == "000":
		origin = (alert.get("predecoder") or {}).get("hostname")
		if origin:
			return str(origin)
	return agent.get("name")


def source_ip(alert):
	value = (alert.get("data") or {}).get("srcip")
	try:
		return str(ipaddress.ip_address(str(value).strip())) if value else None
	except ValueError:
		return None


def timestamp(text):
	for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
		try:
			return datetime.strptime(str(text), fmt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
		except ValueError:
			continue
	return None


def skip_reason(alert):
	"""Why an alert is not taken (None when it is)."""
	rule = alert.get("rule") if isinstance(alert, dict) else None
	if not isinstance(rule, dict) or rule.get("level") is None:
		return "not a Wazuh alert"
	try:
		int(rule["level"])
	except (TypeError, ValueError):
		return "rule level is not a number"
	excluded = EXCLUDED_GROUPS.intersection(rule.get("groups") or [])
	if excluded:
		return f"group {sorted(excluded)[0]} is not brought into Wolf"
	return None


def event_for(alert, fingerprint):
	"""The raw Wolf event (as the agent would send it) for one Wazuh alert, or None.

	`fingerprint(*parts)` is schema.fingerprint, passed in to keep this module frappe-free."""
	if skip_reason(alert):
		return None
	rule = alert["rule"]
	level = int(rule["level"])
	ip, host = source_ip(alert), host_of(alert)
	data = alert.get("data") or {}
	agent = alert.get("agent") or {}
	title = f"Wazuh: {rule.get('description') or 'rule ' + str(rule.get('id'))}"
	if ip:
		title += f" from {ip}"
	if host:
		title += f" on {host}"
	payload = {
		"ip": ip, "user": data.get("srcuser") or data.get("dstuser"), "count": 1, "rule_id": rule.get("id"), "level": level,
		"description": rule.get("description"), "groups": (rule.get("groups") or [])[:10],
		"wazuh_agent": {k: agent.get(k) for k in ("id", "name", "ip") if agent.get(k)}, "location": alert.get("location"),
		"decoder": (alert.get("decoder") or {}).get("name"),
		"sample_lines": [str(alert["full_log"])[:1000]] if alert.get("full_log") else None,
		"wazuh_alert_id": alert.get("id"), "via": "wazuh integration",
	}  # fmt: skip
	techniques = [str(t) for t in ((rule.get("mitre") or {}).get("id") or [])][:10]
	if techniques:
		payload["attack_techniques"] = techniques
	return {
		"type": "security", "category": "hids_alert", "severity": severity(level), "source": "wazuh",
		"timestamp": timestamp(alert.get("timestamp")), "host": host, "title": title[:255],
		"fingerprint": fingerprint(host, "wazuh", rule.get("id"), ip or host),
		"payload": {k: v for k, v in payload.items() if v not in (None, [], {})},
	}  # fmt: skip
