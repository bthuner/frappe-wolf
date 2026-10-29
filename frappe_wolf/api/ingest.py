import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from frappe_wolf.api.agent import _request_json, authenticate_agent, push_interval
from frappe_wolf.wolf import events, schema, silences
from frappe_wolf.wolf.incidents import attach_to_open_incident


@frappe.whitelist(allow_guest=True, methods=["POST"])
def push():
	"""Receive a batch of metrics and events from an agent (docs/protocol.md §2)."""
	agent = authenticate_agent()
	data = _request_json()
	metrics_in = data.get("metrics") or []
	events_in = data.get("events") or []
	if not isinstance(metrics_in, list) or not isinstance(events_in, list):
		frappe.throw(_("metrics and events must be lists"))

	settings = frappe.get_cached_doc("Wolf Settings")
	if len(metrics_in) > (cint(settings.max_metrics_per_push) or 5000) or len(events_in) > (
		cint(settings.max_events_per_push) or 1000
	):
		frappe.throw(_("Batch too large, split it"), frappe.ValidationError)

	tz, now = events.system_tz(), now_datetime()
	hostname = (data.get("hostname") or "").strip()[:140] or None
	rejected = []

	metrics = []
	for raw in metrics_in:
		try:
			metrics.append(schema.normalize_metric(raw, tz, now, default_host=hostname))
		except schema.PayloadError as e:
			rejected.append(str(e))
	accepted_metrics = events.insert_metrics(metrics, agent=agent)

	accepted_events = 0
	for raw in events_in:
		try:
			event = schema.normalize_event(raw, tz, now, default_host=hostname)
		except schema.PayloadError as e:
			rejected.append(str(e))
			continue
		name = events.insert_event(event, agent=agent)
		attach_to_open_incident(name, event)
		accepted_events += 1

	frappe.db.set_value(
		"Wolf Agent",
		agent,
		{
			"last_seen": now,
			"last_ip": frappe.local.request_ip,
			"agent_version": str(data.get("agent_version") or "")[:140] or None,
			"silence_reported": 0,
		},
		update_modified=False,
	)
	frappe.db.commit()

	return {
		"metrics": accepted_metrics,
		"events": accepted_events,
		"rejected": rejected[:20],
		"push_interval": push_interval(),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def deployment():
	"""Mark a deployment window for a bench or site (docs/protocol.md §3)."""
	agent = authenticate_agent()
	data = _request_json()
	hostname = frappe.db.get_value("Wolf Agent", agent, "hostname")
	target = data.get("site") or data.get("bench") or hostname
	raw = {
		"type": "deployment",
		"category": data.get("source") or "manual",
		"severity": "info",
		"source": data.get("source") or "wolf-agent",
		"title": f"Deployment on {target}" + (f": {data['note']}" if data.get("note") else ""),
		"timestamp": data.get("timestamp"),
		"host": data.get("host") or hostname,
		"bench": data.get("bench"),
		"site": data.get("site"),
		"payload": {k: data.get(k) for k in ("note", "source", "version", "task") if data.get(k)},
	}
	try:
		event = schema.normalize_event(raw, events.system_tz(), now_datetime())
	except schema.PayloadError as e:
		frappe.throw(str(e))
	name = events.insert_event(event, agent=agent)
	result = {"event": name}

	# Optional maintenance window (protocol v1 addition, ignored by older apps).
	if cint(data.get("silence_minutes")) > 0:
		result["silence"] = silences.create(
			data["silence_minutes"],
			host=event["host"],
			bench=event.get("bench"),
			site=event.get("site"),
			reason=event["title"],
			source="deployment",
			deployment_event=name,
		)
		result["silence_until"] = str(frappe.db.get_value("Wolf Silence", result["silence"], "ends_at"))
	frappe.db.commit()
	return result
