"""SIEM export (wolf/siem.py): security events and journal entries to a syslog collector.

Every minute `export_siem` sends what is new since its cursor: Wolf Events of the chosen types
at or above the minimum severity, and, when enabled, every signed journal entry. Messages are
RFC 5424 syslog carrying CEF or OCSF JSON, over UDP, TCP or TLS (octet-counted frames on TCP
and TLS, or one message per line for collectors that expect it). UDP cannot tell whether the
collector got anything: the cursor moves anyway. The cursor only moves after a batch was sent, so a collector that is down delays the
export without losing anything (at-least-once: a collector may see a message twice after a
failure mid-batch).

The cursor follows `creation`, which is set at insert, not at commit: events newer than `LAG`
are left for the next run, so a transaction that commits a little later is not jumped over.
The cursor and the last error are a global default (`wolf_siem_cursor`), not a Wolf Settings
field: writing it every minute must not touch the settings form.
"""

import json
import os
import ssl
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from frappe_wolf import __version__
from frappe_wolf.wolf import events, siem
from frappe_wolf.wolf.schema import SEVERITY_RANK
from frappe_wolf.wolf.spa import MANAGER_ROLES

BATCH = 500
TIMEOUT = 10
LAG_SECONDS = 120
CURSOR_KEY = "wolf_siem_cursor"


def _settings():
	return frappe.get_cached_doc("Wolf Settings")


def _cursor():
	try:
		return json.loads(frappe.db.get_global(CURSOR_KEY) or "{}")
	except ValueError:
		return {}


def _save_cursor(cursor):
	frappe.db.set_global(CURSOR_KEY, json.dumps(cursor, default=str))


def _utc(dt, tz):
	if not dt:
		return None
	return dt.replace(tzinfo=ZoneInfo(tz)) if dt.tzinfo is None else dt


def _ca_file(settings):
	path = (settings.get("siem_ca_file") or "").strip()
	if path and not os.path.isabs(path):
		path = frappe.get_site_path(path)  # e.g. private/files/collector-ca.pem
	return path or None


def _sender(settings):
	return siem.Sender(settings.siem_host, cint(settings.siem_port) or 514, settings.siem_transport or "tcp",
	                   verify_tls=bool(cint(settings.siem_verify_tls)), ca_file=_ca_file(settings),
	                   framing=settings.get("siem_framing") or "octet-counting", timeout=TIMEOUT)  # fmt: skip


def _event_rows(cursor, types, min_rank, until):
	since, after = cursor.get("event_creation") or "1970-01-01", cursor.get("event_name") or ""
	rows = frappe.db.sql(
		"""select name, creation, event_type, category, severity, title, source, `timestamp`, `count`, host, bench, site,
			incident, payload from `tabWolf Event`
		where event_type in %(types)s and creation <= %(until)s
			and (creation > %(since)s or (creation = %(since)s and name > %(after)s))
		order by creation asc, name asc limit %(limit)s""",
		{"types": tuple(types), "since": since, "after": after, "until": until, "limit": BATCH},
		as_dict=True,
	)
	keep = []
	for r in rows:
		if SEVERITY_RANK.get(r.severity, 0) >= min_rank:
			try:
				r["payload"] = json.loads(r.payload) if r.payload else {}
			except ValueError:
				r["payload"] = {"raw": r.payload[:siem.MAX_PAYLOAD]}
			keep.append(r)
	return rows, keep


def _journal_rows(cursor):
	return frappe.db.sql(
		"""select seq, `at`, kind, ref_doctype, ref_name, summary, digest, prev_mac, mac from `tabWolf Journal Entry`
		where seq > %(seq)s order by seq asc limit %(limit)s""",
		{"seq": cint(cursor.get("journal_seq")), "limit": BATCH},
		as_dict=True,
	)


def export_siem(lag_seconds=LAG_SECONDS):
	"""Scheduler (every minute). Returns the number of messages sent."""
	settings = _settings()
	if not cint(settings.get("siem_enabled")) or not settings.get("siem_host"):
		return 0
	tz = events.system_tz()
	fmt = settings.get("siem_format") or "ocsf"
	hostname = frappe.local.site
	header = settings.get("siem_header") or "rfc5424"
	types = [t.strip() for t in (settings.get("siem_event_types") or "security").split(",") if t.strip()]
	min_rank = SEVERITY_RANK.get(settings.get("siem_min_severity") or "warning", 1)
	cursor = _cursor()
	sender = _sender(settings)
	until = add_to_date(now_datetime(), seconds=-lag_seconds)
	sent = 0
	try:
		while True:
			scanned, rows = _event_rows(cursor, types, min_rank, until)
			if not scanned:
				break
			messages = []
			for r in rows:
				r["timestamp"] = _utc(r.timestamp, tz)
				messages.append(siem.render("event", r, fmt, hostname, __version__, header))
			sender.send(messages)
			sent += len(messages)
			cursor.update(event_creation=str(scanned[-1].creation), event_name=scanned[-1].name)
			_save_cursor(cursor)
			frappe.db.commit()
			if len(scanned) < BATCH:
				break
		while cint(settings.get("siem_journal")):
			rows = _journal_rows(cursor)
			if not rows:
				break
			messages = [siem.render("journal", {**r, "at": _utc(r.at, tz)}, fmt, hostname, __version__, header) for r in rows]
			sender.send(messages)
			sent += len(messages)
			cursor["journal_seq"] = rows[-1].seq
			_save_cursor(cursor)
			frappe.db.commit()
			if len(rows) < BATCH:
				break
		cursor.update(last_ok=str(now_datetime()), last_error=None)
	except (OSError, ssl.SSLError) as e:
		cursor.update(last_error=f"{now_datetime()}: {e}"[:500])
		frappe.logger("frappe_wolf").warning(f"SIEM export to {settings.siem_host}: {e}")
	_save_cursor(cursor)
	frappe.db.commit()
	return sent


@frappe.whitelist()
def get_status():
	frappe.only_for(MANAGER_ROLES)
	settings = _settings()
	return {"enabled": bool(cint(settings.get("siem_enabled"))), "host": settings.get("siem_host"), **_cursor()}


@frappe.whitelist(methods=["POST"])
def send_test():
	"""One test message to the configured collector, whatever `siem_enabled` says."""
	frappe.only_for(MANAGER_ROLES)
	settings = _settings()
	if not settings.get("siem_host"):
		frappe.throw(frappe._("Set the SIEM host first."))
	event = {"name": "test", "event_type": "security", "category": "wolf_test", "severity": "info",
	         "title": "Frappe Wolf SIEM export test", "timestamp": _utc(now_datetime(), events.system_tz()),
	         "site": frappe.local.site, "payload": {}}  # fmt: skip
	_sender(settings).send([siem.render("event", event, settings.get("siem_format") or "ocsf", frappe.local.site, __version__,
	                                    settings.get("siem_header") or "rfc5424")])  # fmt: skip
	return {"sent": True}
