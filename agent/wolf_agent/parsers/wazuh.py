"""Wazuh manager alerts (`alerts.json`) → `security` / `hids_alert` events (#72, step 2).

Wazuh is optional: the Wolf agent on the Wazuh manager's host reads its alerts log
(`wazuh.alerts_log`, default `/var/ossec/logs/alerts/alerts.json`) when there is one. The Wolf
agent user must be able to read it (the file belongs to the `wazuh` group).

- **Floor:** only alerts at or above `wazuh.min_level` (7 by default) are read, so Wolf does
  not copy Wazuh's noise (configuration assessment, logins that worked). Level 12 and above is
  critical, 7 to 11 warning, below 7 info.
- **Loop guard:** alerts of the `frappe_wolf` group (the rules that read Wolf's own SIEM export,
  docs/siem.md) are always skipped.
- **Excluded groups:** `wazuh.exclude_groups`, `["sca"]` by default. Configuration assessment
  failures are posture, not intrusions: a fresh CIS scan gives dozens of level 7 alerts.
- **Grouping:** one event per rule and subject (the source IP, else the host) per push
  interval, with a count.
- **Host:** the Wazuh agent's name. For logs the manager received by syslog (agent `000`), it
  is the host name in the log line (`predecoder.hostname`), since that is where it happened.
- **Payload:** rule id, level, description, groups, ATT&CK techniques (`rule.mitre.id`), the
  Wazuh agent, location, decoder, source IP and user, and the first log line (the agent's
  scrubber runs on it like on every event).
"""

import ipaddress
import json
from datetime import datetime

from ..util import event, fingerprint, utc_iso

MAX_GROUPS = 500
LOOP_GROUP = "frappe_wolf"


def timestamp(text):
	for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
		try:
			return utc_iso(datetime.strptime(str(text), fmt))
		except ValueError:
			continue
	return None


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


class WazuhParser:
	def __init__(self, host, log_file=None, min_level=7, exclude_groups=("sca",), **_):
		self.host, self.log_file = host, log_file
		self.exclude = {LOOP_GROUP, *(exclude_groups or ())}
		self.min_level = int(min_level if min_level is not None else 7)
		self.groups = {}
		self.overflow = 0
		self.bad_lines = 0

	def feed(self, line):
		if '"rule"' not in line:
			return
		try:
			alert = json.loads(line)
		except ValueError:
			self.bad_lines += 1
			return
		rule = alert.get("rule") if isinstance(alert, dict) else None
		if not isinstance(rule, dict):
			return
		try:
			level = int(rule.get("level") or 0)
		except (TypeError, ValueError):
			return
		groups = rule.get("groups") or []
		if level < self.min_level or self.exclude.intersection(groups):
			return
		ip = source_ip(alert)
		origin = host_of(alert)
		key = (rule.get("id"), ip or origin)
		g = self.groups.get(key)
		if g is None:
			if len(self.groups) >= MAX_GROUPS:
				self.overflow += 1
				return
			agent = alert.get("agent") or {}
			data = alert.get("data") or {}
			g = self.groups[key] = {
				"count": 0, "level": level, "first": timestamp(alert.get("timestamp")), "host": origin, "ip": ip,
				"rule_id": rule.get("id"), "description": rule.get("description"), "groups": groups[:10],
				"techniques": [str(t) for t in ((rule.get("mitre") or {}).get("id") or [])][:10],
				"wazuh_agent": {k: agent.get(k) for k in ("id", "name", "ip") if agent.get(k)},
				"location": alert.get("location"), "decoder": (alert.get("decoder") or {}).get("name"),
				"user": data.get("srcuser") or data.get("dstuser"), "full_log": str(alert.get("full_log") or "")[:1000],
				"wazuh_id": alert.get("id"),
			}  # fmt: skip
		g["count"] += 1
		g["level"] = max(g["level"], level)
		g["last"] = timestamp(alert.get("timestamp"))

	def drain(self, now=None):
		ts = utc_iso(now)
		events = []
		for (rule_id, subject), g in self.groups.items():
			title = f"Wazuh: {g['description'] or f'rule {rule_id}'}"
			if g["ip"]:
				title += f" from {g['ip']}"
			if g["host"]:
				title += f" on {g['host']}"
			if g["count"] > 1:
				title += f", {g['count']} alerts"
			payload = {
				"ip": g["ip"], "user": g["user"], "count": g["count"], "rule_id": rule_id, "level": g["level"],
				"description": g["description"], "groups": g["groups"], "wazuh_agent": g["wazuh_agent"], "location": g["location"],
				"decoder": g["decoder"], "sample_lines": [g["full_log"]] if g["full_log"] else None, "wazuh_alert_id": g["wazuh_id"],
				"first_seen": g["first"], "last_seen": g.get("last"), "log_file": self.log_file,
			}  # fmt: skip
			if g["techniques"]:
				payload["attack_techniques"] = g["techniques"]
			events.append(event("security", "hids_alert", severity(g["level"]), g["first"] or ts, g["host"] or self.host, title[:255],
			                    fingerprint(g["host"] or self.host, "wazuh", rule_id, subject),
			                    {k: v for k, v in payload.items() if v not in (None, [], {})}, source="wazuh"))  # fmt: skip
		if self.overflow:
			events.append(event("security", "hids_alert", "warning", ts, self.host,
			                    f"Wazuh: {self.overflow} more alerts not detailed (over {MAX_GROUPS} rule/subject pairs)",
			                    fingerprint(self.host, "wazuh", "overflow"), {"count": self.overflow, "log_file": self.log_file},
			                    source="wazuh"))  # fmt: skip
		self.groups, self.overflow = {}, 0
		return [], events
