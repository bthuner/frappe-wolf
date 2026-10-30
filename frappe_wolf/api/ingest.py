import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from frappe_wolf import satellite
from frappe_wolf.api.agent import _request_json, authenticate_agent, push_interval
from frappe_wolf.wolf import events, health, rate, schema, silences
from frappe_wolf.wolf.incidents import attach_to_open_incident


INGEST_QUEUE = health.INGEST_QUEUE
STORE_METHOD = "frappe_wolf.api.ingest.store_batch"
STORE_ATTEMPTS = 3


@frappe.whitelist(allow_guest=True, methods=["POST"])
def push():
	"""Receive a batch of metrics and events from an agent (docs/protocol.md §2)."""
	agent = authenticate_agent()
	settings = frappe.get_cached_doc("Wolf Settings")
	rate.check_push_rate(agent, cint(settings.max_pushes_per_minute))
	return accept(agent, _request_json())


def accept(agent, data, now=None):
	"""Validate and normalize one push, then store it (queued, or inline in sync mode).

	The agent's last_seen / clock skew and Wolf's health are always handled here, so the
	heartbeat does not depend on the ingest queue.
	"""
	settings = frappe.get_cached_doc("Wolf Settings")
	metrics_in = data.get("metrics") or []
	events_in = data.get("events") or []
	if not isinstance(metrics_in, list) or not isinstance(events_in, list):
		frappe.throw(_("metrics and events must be lists"))

	if len(metrics_in) > (cint(settings.max_metrics_per_push) or 5000) or len(events_in) > (
		cint(settings.max_events_per_push) or 1000
	):
		frappe.throw(_("Batch too large, split it"), frappe.ValidationError)

	tz, now = events.system_tz(), now or now_datetime()
	max_skew = cint(settings.max_clock_skew_seconds)
	skew = schema.clock_skew_seconds(data.get("sent_at"), tz, now)
	clamped = 0
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

	events_ok = []
	for raw in events_in:
		try:
			event = schema.normalize_event(raw, tz, now, default_host=hostname)
		except schema.PayloadError as e:
			rejected.append(str(e))
			continue
		event["timestamp"], was_clamped = schema.clamp_future(event["timestamp"], now, max_skew)
		event["last_seen"] = event["timestamp"]
		clamped += was_clamped
		events_ok.append(event)

	queued = False
	if metrics or events_ok:
		if (settings.get("ingest_mode") or "async") == "async":
			queued = enqueue_batch(agent, metrics, events_ok)
		if not queued:
			store_batch(agent, metrics, events_ok)

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
		"metrics": len(metrics),
		"events": len(events_ok),
		"rejected": rejected[:20],
		"push_interval": push_interval(),
		"health": wolf_health,
		"clock_skew_seconds": skew,
		"clamped": clamped,
		"queued": queued,
	}


def enqueue_batch(agent, metrics, events_in, attempt=1, correlate=None):
	"""Queue the inserts for a worker. False if the queue is unavailable (store inline then)."""
	if correlate is None:
		correlate = not satellite.is_satellite()  # a satellite opens no incidents
	try:
		frappe.enqueue(
			STORE_METHOD,
			queue=INGEST_QUEUE,
			agent=agent,
			metrics=metrics,
			events_in=events_in,
			correlate=correlate,
			attempt=attempt,
		)
		return True
	except Exception:
		frappe.logger("frappe_wolf").warning("ingest queue unavailable, storing inline", exc_info=True)
		return False


def store_batch(agent, metrics, events_in, correlate=None, attempt=None):
	"""Insert normalized metrics and events, attaching events to open incidents.

	Runs in a worker for queued pushes (`attempt` set), inline in sync mode. The agent has
	dropped a queued push from its spool already, so a failure is not left to the agent: the
	batch is queued again (deadlocks, lock waits), and recorded in the Error Log once
	STORE_ATTEMPTS have failed.
	"""
	if correlate is None:
		correlate = not satellite.is_satellite()  # a satellite opens no incidents
	try:
		events.insert_metrics(metrics, agent=agent)
		for event in events_in:
			name = events.insert_event(event, agent=agent)
			if correlate:
				attach_to_open_incident(name, event)
		frappe.db.commit()
	except Exception:
		if attempt is None:
			raise
		frappe.db.rollback()
		if attempt < STORE_ATTEMPTS and enqueue_batch(agent, metrics, events_in, attempt + 1, correlate):
			return None
		frappe.log_error(
			title="[Wolf] queued push not stored",
			message=f"{len(metrics)} metrics and {len(events_in)} events from {agent} were dropped "
			f"after {attempt} attempts.\n\n{frappe.get_traceback()}",
		)
		frappe.db.commit()
		return None
	return {"metrics": len(metrics), "events": len(events_in)}


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
