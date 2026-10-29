"""Satellite mode: capture this site's local signals and forward them to a central Wolf site.

Signals go to a durable outbox (Wolf Outbox) inside the triggering transaction, and the
`forward_outbox` scheduler job pushes them through the regular agent ingest API.
"""

import json
import platform

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from frappe_wolf import __version__
from frappe_wolf.wolf import events, forwarding

MANAGER_ROLES = ("System Manager", "Wolf Manager")


def is_satellite():
	return (frappe.get_cached_doc("Wolf Settings").get("mode") or "central") == "satellite"


def enqueue(data, dedupe_minutes=None):
	"""Store a normalized event (schema.normalize_event) for forwarding; returns the outbox name.

	With `dedupe_minutes`, a still-pending row with the same fingerprint is bumped instead.
	"""
	if dedupe_minutes and data.get("fingerprint"):
		existing = frappe.db.get_value(
			"Wolf Outbox",
			{
				"fingerprint": data["fingerprint"],
				"site": data.get("site"),
				"last_seen": (">=", add_to_date(data["timestamp"], minutes=-dedupe_minutes)),
			},
			["name", "count"],
			as_dict=True,
			order_by="last_seen desc",
		)
		if existing:
			frappe.db.set_value(
				"Wolf Outbox",
				existing.name,
				{"count": (existing.count or 1) + data.get("count", 1), "last_seen": data["timestamp"]},
				update_modified=False,
			)
			return existing.name

	doc = frappe.get_doc(
		{
			"doctype": "Wolf Outbox",
			"title": data["title"],
			"event_type": data["event_type"],
			"site": data.get("site"),
			"fingerprint": data.get("fingerprint"),
			"timestamp": data["timestamp"],
			"last_seen": data.get("last_seen") or data["timestamp"],
			"count": data.get("count") or 1,
			"event": json.dumps(forwarding.protocol_event(data, events.system_tz()), default=str),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def flush(force=False):
	"""Push pending outbox rows to the central site. Runs every minute; an empty push is the heartbeat."""
	if not is_satellite():
		return
	settings = frappe.get_single("Wolf Settings")
	secret = settings.get_password("satellite_secret", raise_exception=False)
	if not settings.central_url or not settings.satellite_id or not secret:
		return
	now = now_datetime()
	if not force and settings.forward_retry_at and get_datetime(settings.forward_retry_at) > now:
		return

	trim_outbox()
	rows = frappe.get_all(
		"Wolf Outbox",
		fields=["name", "event", "count"],
		order_by="creation asc",
		limit=forwarding.BATCH_SIZE * forwarding.MAX_BATCHES_PER_RUN,
	)
	host = events.local_scope()["host"]
	sent, notes = 0, []
	for batch in forwarding.chunks(rows) or [[]]:
		body = forwarding.push_body(
			[forwarding.with_count(json.loads(r.event), r.count) for r in batch], host, __version__
		)
		outcome, detail = _post(settings, secret, body)
		if outcome in ("retry", "auth"):
			failures = cint(settings.forward_failures) + 1
			_record(
				status="credentials rejected" if outcome == "auth" else "retrying",
				error=detail,
				forward_failures=failures,
				forward_retry_at=forwarding.next_attempt(now, failures, outcome),
			)
			return {"sent": sent, "status": outcome, "error": detail}
		# accepted, or refused as a whole (retrying cannot help): either way it leaves the outbox
		if batch:
			frappe.db.delete("Wolf Outbox", {"name": ("in", [r.name for r in batch])})
		if outcome == "rejected":
			frappe.logger("frappe_wolf").error(f"central site rejected a batch of {len(batch)} events: {detail}")
			notes.append(f"dropped {len(batch)} events: {detail}")
		else:
			sent += len(batch)
			if detail:
				notes.append(detail)
		frappe.db.commit()

	_record(
		status="ok" if not notes else "ok with rejections",
		error="\n".join(notes)[:1000] or None,
		last_forward_at=now,
		forward_failures=0,
		forward_retry_at=None,
	)
	return {"sent": sent, "status": "ok", "error": "\n".join(notes) or None}


def _post(settings, secret, body):
	"""POST one push body; returns (outcome, detail) with outcome from forwarding.classify_status."""
	try:
		resp = requests.post(
			settings.central_url.rstrip("/") + forwarding.PUSH_METHOD,
			json=body,
			headers={"X-Wolf-Agent-Id": settings.satellite_id, "X-Wolf-Agent-Secret": secret},
			timeout=15,
			verify=bool(cint(settings.verify_tls)),
		)
	except requests.RequestException as e:
		return "retry", str(e)[:500]
	outcome = forwarding.classify_status(resp.status_code)
	if outcome != "ok":
		return outcome, f"HTTP {resp.status_code}: {forwarding.error_detail(resp.text)}"
	try:
		message = resp.json().get("message") or {}
	except (ValueError, AttributeError):
		# e.g. a proxy answering 200 with an HTML page
		return "retry", f"invalid JSON response: {resp.text[:200]!r}"
	rejected = message.get("rejected") if isinstance(message, dict) else None
	return "ok", ("rejected: " + "; ".join(map(str, rejected)))[:500] if rejected else None


def _record(status, error=None, **values):
	frappe.db.set_value(
		"Wolf Settings",
		"Wolf Settings",
		{"last_forward_status": status, "last_forward_error": error, **values},
		update_modified=False,
	)
	frappe.db.commit()


def trim_outbox():
	"""Keep the outbox bounded while the central site is unreachable: drop the oldest rows."""
	extra = forwarding.overflow(frappe.db.count("Wolf Outbox"))
	if not extra:
		return
	names = frappe.get_all("Wolf Outbox", order_by="creation asc", limit=extra, pluck="name")
	frappe.db.delete("Wolf Outbox", {"name": ("in", names)})
	frappe.logger("frappe_wolf").warning(f"Wolf Outbox over {forwarding.MAX_OUTBOX} rows, dropped {len(names)}")


@frappe.whitelist(methods=["POST"])
def register(url, token, verify_tls=1):
	"""Register this site as a satellite of the central Wolf site at `url` and switch to satellite mode.

	bench --site b.example.com execute frappe_wolf.satellite.register \
		--kwargs '{"url": "https://wolf.example.com", "token": "<registration token>"}'
	"""
	frappe.only_for(MANAGER_ROLES)
	try:
		url = forwarding.normalize_central_url(url)
	except ValueError as e:
		frappe.throw(str(e))
	if not token:
		frappe.throw(_("The central site's registration token is required"))

	scope = events.local_scope()
	body = forwarding.register_body(
		str(token).strip(), scope, __version__, platform.platform(), frappe.utils.get_bench_path()
	)
	try:
		resp = requests.post(url + forwarding.REGISTER_METHOD, json=body, timeout=20, verify=bool(cint(verify_tls)))
	except requests.RequestException as e:
		frappe.throw(_("Could not reach {0}: {1}").format(url, e))
	if forwarding.classify_status(resp.status_code) != "ok":
		frappe.throw(
			_("The central site refused the registration (HTTP {0}): {1}").format(
				resp.status_code, forwarding.error_detail(resp.text)
			)
		)
	try:
		message = resp.json().get("message") or {}
	except (ValueError, AttributeError):
		message = {}
	if not message.get("agent_id") or not message.get("agent_secret"):
		frappe.throw(_("Unexpected answer from {0}: is Frappe Wolf installed there?").format(url))

	settings = frappe.get_single("Wolf Settings")
	settings.update(
		{
			"mode": "satellite",
			"central_url": url,
			"verify_tls": cint(verify_tls),
			"satellite_id": message["agent_id"],
			"satellite_secret": message["agent_secret"],
			"last_forward_status": "registered",
			"last_forward_error": None,
			"forward_failures": 0,
			"forward_retry_at": None,
		}
	)
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()
	return {"mode": "satellite", "central_url": url, "satellite_id": message["agent_id"]}


@frappe.whitelist(methods=["POST"])
def forward_now():
	"""Desk button: push the outbox right away, ignoring the retry delay."""
	frappe.only_for(MANAGER_ROLES)
	if not is_satellite():
		frappe.throw(_("This site is not in satellite mode"))
	result = flush(force=True)
	if result is None:
		frappe.throw(_("Register with the central site first"))
	return {**result, **status()}


def status():
	"""Mode summary for the Overview page."""
	settings = frappe.get_single("Wolf Settings")
	if (settings.get("mode") or "central") != "satellite":
		return {"mode": "central"}
	return {
		"mode": "satellite",
		"central_url": settings.central_url,
		"satellite_id": settings.satellite_id,
		"pending": frappe.db.count("Wolf Outbox"),
		"last_forward_at": settings.last_forward_at,
		"last_forward_status": settings.last_forward_status,
		"last_forward_error": settings.last_forward_error,
		"forward_retry_at": settings.forward_retry_at,
	}
