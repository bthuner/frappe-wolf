import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from frappe_wolf import satellite
from frappe_wolf.api.agent import _request_json, authenticate_agent, push_interval
from frappe_wolf.wolf import events, health, schema, silences
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
	max_skew = cint(settings.max_clock_skew_seconds)
	skew = schema.clock_skew_seconds(data.get("sent_at"), tz, now)
	clamped = 0
	correlate = not satellite.is_satellite()  # a satellite opens no incidents
	hostname = (data.get("hostname") or "").strip()[:140] or None
	rejected = []

	metrics = []
	for raw in metrics_in:
		try:
			metric = schema.normalize_metric(raw, tz, now, default_host=hostname)
		except schema.PayloadError as e:
			rejected.append(str(e))
			continue
		metric["timestamp"], was_clamped = schema.clamp_future(metric["timestamp"], now, max_skew)
		clamped += was_clamped
		metrics.append(metric)
	accepted_metrics = events.insert_metrics(metrics, agent=agent)

	accepted_events = 0
	for raw in events_in:
		try:
			event = schema.normalize_event(raw, tz, now, default_host=hostname)
		except schema.PayloadError as e:
			rejected.append(str(e))
			continue
		event["timestamp"], was_clamped = schema.clamp_future(event["timestamp"], now, max_skew)
		event["last_seen"] = event["timestamp"]
		clamped += was_clamped
		name = events.insert_event(event, agent=agent)
		if correlate:
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
			# satellites and older agents send no sent_at: keep the last known skew
			**({"clock_skew_seconds": skew} if skew is not None else {}),
		},
		update_modified=False,
	)
	frappe.db.commit()

	# after the commit: a health failure must not cost the telemetry
	wolf_health = health.safe_health()
	health.report_stall(wolf_health)
	frappe.db.commit()

	return {
		"metrics": accepted_metrics,
		"events": accepted_events,
		"rejected": rejected[:20],
		"push_interval": push_interval(),
		"health": wolf_health,
		"clock_skew_seconds": skew,
		"clamped": clamped,
	}


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def status():
	"""Wolf's own health for an agent, without sending telemetry (docs/protocol.md §4)."""
	agent = authenticate_agent()
	return {"agent": agent, "push_interval": push_interval(), "health": health.safe_health()}


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
