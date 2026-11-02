"""Wazuh custom integration endpoint (wolf/wazuh.py maps; agent/wazuh/custom-frappe-wolf posts).

`alert` takes one Wazuh alert, posted as it is by the integration daemon, with the credentials
of a Wolf Agent registered with kind `integration` (a source that pushes only when it has
something to say, so it is never reported silent). The alert becomes a `hids_alert` event;
the same alert repeated within DEDUPE_MINUTES bumps the event's count instead of adding rows.
"""

import frappe
from frappe.utils import now_datetime

from frappe_wolf import satellite
from frappe_wolf.api.agent import _request_json, authenticate_agent
from frappe_wolf.wolf import events, rate, schema, wazuh
from frappe_wolf.wolf.incidents import attach_to_open_incident

DEDUPE_MINUTES = 10
RATE_PER_MINUTE = 600  # one call per alert: a burst must not be refused like a misbehaving agent


@frappe.whitelist(allow_guest=True, methods=["POST"])
def alert():
	agent = authenticate_agent()
	rate.check_push_rate(agent, RATE_PER_MINUTE)
	data = _request_json()
	skipped = wazuh.skip_reason(data)
	now = now_datetime()
	frappe.db.set_value("Wolf Agent", agent, {"last_seen": now, "last_ip": frappe.local.request_ip, "silence_reported": 0},
	                    update_modified=False)  # fmt: skip
	if skipped:
		frappe.db.commit()
		return {"stored": False, "reason": skipped}
	raw = wazuh.event_for(data, schema.fingerprint)
	raw["host"] = raw["host"] or frappe.db.get_value("Wolf Agent", agent, "hostname")
	try:
		event = schema.normalize_event(raw, events.system_tz(), now)
	except schema.PayloadError as e:
		frappe.throw(str(e))
	event["timestamp"], _clamped = schema.clamp_future(event["timestamp"], now, 300)
	event["last_seen"] = event["timestamp"]
	name = events.insert_event(event, agent=agent, dedupe_minutes=DEDUPE_MINUTES)
	if not satellite.is_satellite():
		attach_to_open_incident(name, event)
	frappe.db.commit()
	return {"stored": True, "event": name}
