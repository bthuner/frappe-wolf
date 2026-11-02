"""CrowdSec (wolf/crowdsec.py): decisions in, detections and approved blocks out.

Wolf Settings → CrowdSec. Every minute `sync_crowdsec`, on the central site only:
- reads the LAPI's decisions (the whole list on the first run and every hour, the changes
  in between), into `private/wolf/crowdsec_decisions.json`. Security events whose IP is under
  a decision are tagged `payload.crowdsec` at insert, one severity step more (Wolf's own blocks
  excepted);
- sends the security events with an IP, at or above the minimum severity, as LAPI alerts
  (when Send detections is on). They carry no decision; with Let CrowdSec decide on, the
  LAPI's profiles may ban (the default profile bans for 4 h). A cursor in a global default
  moves only after a batch was accepted, as for the SIEM export.

When a Wolf Manager approves a `block_ip`, `apply_block` also sends it as a `ban` decision for
the approved duration (Apply approved blocks), so every bouncer of that LAPI applies it. The
Wolf agent still applies its own nftables/fail2ban block when its responder is on.
"""

import json
import os
from datetime import timedelta

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from frappe_wolf import __version__, satellite
from frappe_wolf.wolf import crowdsec, events, window
from frappe_wolf.wolf.spa import MANAGER_ROLES

CURSOR_KEY = "wolf_crowdsec_cursor"
BATCH = 100
LAG_SECONDS = 60
FULL_SYNC = timedelta(hours=1)
_cache = {"key": None, "decisions": None}


def _settings():
	return frappe.get_cached_doc("Wolf Settings")


def enabled(settings=None):
	settings = settings or _settings()
	return bool(cint(settings.get("crowdsec_enabled")) and settings.get("crowdsec_url"))


def client(settings=None):
	settings = settings or _settings()
	return crowdsec.Client(
		settings.crowdsec_url,
		bouncer_key=settings.get_password("crowdsec_bouncer_key", raise_exception=False),
		machine_id=settings.get("crowdsec_machine_id"),
		password=settings.get_password("crowdsec_password", raise_exception=False),
		version=__version__,
	)


def store_path():
	return frappe.get_site_path("private", "wolf", "crowdsec_decisions.json")


def read_store():
	try:
		with open(store_path()) as f:
			return json.load(f)
	except (OSError, ValueError):
		return {}


def _write_store(data):
	path = store_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	tmp = path + ".tmp"
	with open(tmp, "w") as f:
		json.dump(data, f)
	os.replace(tmp, path)
	_cache["key"] = None


def load_decisions():
	"""The stored decisions, reloaded when the file changes; None when there are none."""
	path = store_path()
	try:
		key = (path, os.stat(path).st_mtime_ns)
	except OSError:
		return None
	if _cache["key"] != key:
		_cache["decisions"] = crowdsec.Decisions.from_json(read_store().get("items"))
		_cache["key"] = key
	return _cache["decisions"]


def enrich(data):
	"""events.insert_event: tag a security event whose payload `ip` is under a CrowdSec decision."""
	try:
		if data.get("event_type") != "security" or not enabled():
			return data
		payload = json.loads(data["payload"]) if data.get("payload") else None
		if not isinstance(payload, dict):
			return data
		from frappe_wolf.wolf import schema

		tagged, severity = crowdsec.tag(payload, data.get("severity"), load_decisions(), now_datetime())
		if tagged is payload:
			return data
		return {**data, "payload": schema.dump_payload(tagged), "severity": severity}
	except Exception:
		frappe.logger("frappe_wolf").exception("CrowdSec lookup failed")
		return data


def _utc(dt):
	return window.as_utc(get_datetime(dt), events.system_tz()) if dt else None


def _cursor():
	raw = frappe.db.get_value("DefaultValue", {"parent": "__global", "defkey": CURSOR_KEY}, "defvalue")
	try:
		return json.loads(raw or "{}")
	except ValueError:
		return {}


def _save_cursor(cursor):
	frappe.db.set_global(CURSOR_KEY, json.dumps(cursor, default=str))


def sync_decisions(api, now=None):
	"""Merge the LAPI's decisions into the store; the whole list hourly or when there is none."""
	now = now or now_datetime()
	store = read_store()
	full = not store.get("full_at") or now - get_datetime(store["full_at"]) >= FULL_SYNC
	stream = api.decisions_stream(startup=full)
	decisions = crowdsec.Decisions() if full else crowdsec.Decisions.from_json(store.get("items"))
	decisions.apply(stream, now).prune(now)
	_write_store({"items": decisions.to_json(), "synced_at": str(now), "full_at": str(now) if full else store.get("full_at")})
	return len(decisions.items)


def _event_rows(cursor, until):
	since, after = cursor.get("event_creation") or str(now_datetime()), cursor.get("event_name") or ""
	return frappe.db.sql(
		"""select name, creation, event_type, category, severity, title, source, `timestamp`, `count`, host, site, payload
		from `tabWolf Event`
		where event_type = 'security' and creation <= %(until)s
			and (creation > %(since)s or (creation = %(since)s and name > %(after)s))
		order by creation asc, name asc limit %(limit)s""",
		{"since": since, "after": after, "until": until, "limit": BATCH},
		as_dict=True,
	)


def push_detections(api, settings, now=None):
	"""Send the new security events with an IP as alerts; returns how many went."""
	now = now or now_datetime()
	cursor = _cursor()
	if not cursor.get("event_creation"):
		# the first run starts now: the past is not replayed into CrowdSec
		_save_cursor({"event_creation": str(now), "event_name": ""})
		return 0
	min_severity = settings.get("crowdsec_min_severity") or "warning"
	remediation = bool(cint(settings.get("crowdsec_detection_remediation")))
	until = add_to_date(now, seconds=-LAG_SECONDS)
	sent = 0
	while True:
		rows = _event_rows(cursor, until)
		if not rows:
			break
		alerts = []
		for r in rows:
			try:
				r["payload"] = json.loads(r.payload) if r.payload else {}
			except ValueError:
				r["payload"] = {}
			if crowdsec.wanted(r, min_severity):
				alerts.append(crowdsec.detection_alert({**r, "timestamp": _utc(r.timestamp)}, remediation))
		api.push_alerts(alerts)
		sent += len(alerts)
		cursor = {"event_creation": str(rows[-1].creation), "event_name": rows[-1].name}
		_save_cursor(cursor)
		frappe.db.commit()
		if len(rows) < BATCH:
			break
	return sent


def sync_crowdsec():
	"""Scheduler (every minute)."""
	settings = _settings()
	if not enabled(settings) or satellite.is_satellite():
		return None
	api = client(settings)
	status = {"at": str(now_datetime())}
	try:
		if settings.get_password("crowdsec_bouncer_key", raise_exception=False):
			status["decisions"] = sync_decisions(api)
		if cint(settings.get("crowdsec_send_detections")) and settings.get("crowdsec_machine_id"):
			status["alerts_sent"] = push_detections(api, settings)
		status["error"] = None
	except crowdsec.CrowdSecError as e:
		status["error"] = str(e)[:500]
		frappe.logger("frappe_wolf").warning(f"CrowdSec: {e}")
	frappe.cache.set_value("wolf_crowdsec_status", status)
	return status


def apply_block(row):
	"""After a block_ip approval: the same block as a CrowdSec decision. Returns a note for the
	incident comment, or None when CrowdSec is not used for blocks."""
	settings = _settings()
	if not enabled(settings) or not cint(settings.get("crowdsec_apply_blocks")) or not settings.get("crowdsec_machine_id"):
		return None
	reason = f"{row.name} approved by {row.approved_by} for incident {row.incident}"
	try:
		ids = client(settings).push_alerts([crowdsec.block_alert(row.target, row.duration_hours, reason, _utc(row.approved_at))])
	except crowdsec.CrowdSecError as e:
		return f"CrowdSec ban not sent: {e}"
	from frappe_wolf.api import journal

	journal.capture("response_action", "Wolf Response Action", row.name, {"crowdsec_alert": ids, "ban_hours": row.duration_hours},
	                f"{row.name} sent to CrowdSec as a {row.duration_hours} h ban")  # fmt: skip
	return f"sent to CrowdSec as a {row.duration_hours} h ban (alert {', '.join(map(str, ids)) or '?'})"


@frappe.whitelist()
def get_status():
	frappe.only_for(MANAGER_ROLES)
	store = read_store()
	return {"enabled": enabled(), "decisions": len(store.get("items") or {}), "synced_at": store.get("synced_at"),
	        **(frappe.cache.get_value("wolf_crowdsec_status") or {})}  # fmt: skip


@frappe.whitelist(methods=["POST"])
def sync_now():
	frappe.only_for(MANAGER_ROLES)
	return sync_crowdsec()
