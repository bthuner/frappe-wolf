"""One incident timeline for the Wolf UI: events, operator activity and notification attempts,
normalized to one row shape, ordered deterministically and paged with a cursor (no frappe imports).

Order is (time, family rank, id) on the stored times, which are naive and in the site time
zone. Two rows at the same time are ordered by kind and id only: that order says nothing
about which happened first. Times leave as UTC (window.to_utc).

Rows keep what was recorded and nothing more: a deduplicated event is one row with its first
and last time and reported count, never one row per occurrence; a delivery is what the
channel endpoint answered, not a person having read it.
"""

import base64
import json
from datetime import datetime

from frappe_wolf.wolf import window

FAMILIES = ("evidence", "context", "activity", "delivery")
RANK = {"event": 0, "activity": 1, "delivery": 2}  # tie-break between record families
MAX_LIMIT = 500


def families(value):
	"""Requested families from 'evidence,activity' (or a list); all when empty."""
	if not value:
		return set(FAMILIES)
	items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
	chosen = {f.strip() for f in items if f and f.strip()}
	unknown = chosen - set(FAMILIES)
	if unknown:
		raise ValueError(f"unknown families: {', '.join(sorted(unknown))}")
	return chosen or set(FAMILIES)


# --- cursor -------------------------------------------------------------------------------


def encode_cursor(key):
	at, rank, row_id = key
	raw = json.dumps([at.isoformat(), rank, row_id]).encode()
	return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
	"""(time, rank, id) of the last row of the previous page; None for the first page."""
	if not cursor:
		return None
	try:
		raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
		at, rank, row_id = json.loads(raw)
		return datetime.fromisoformat(at), int(rank), str(row_id)
	except (ValueError, TypeError) as e:
		raise ValueError("invalid cursor") from e


def keyset_sql(cursor_key, family, column, name_column="name"):
	"""(SQL, params) selecting rows of `family` after the cursor key (time, rank, id).

	Ids are "<family>:<name>". After a row of the same family: a later name at the same time.
	After a row of an earlier rank, or a same-rank row whose id sorts first (the unavailable
	deployment): every row at that time. Otherwise: a later time.
	"""
	if not cursor_key:
		return "", {}
	at, rank, row_id = cursor_key
	prefix = f"{family}:"
	mine = RANK[family]
	if rank == mine and row_id.startswith(prefix):
		return (
			f"and ({column} > %(c_at)s or ({column} = %(c_at)s and {name_column} > %(c_name)s))",
			{"c_at": at, "c_name": row_id.removeprefix(prefix)},
		)
	if rank < mine or (rank == mine and row_id < prefix):
		return f"and {column} >= %(c_at)s", {"c_at": at}
	return f"and {column} > %(c_at)s", {"c_at": at}


def after(key, cursor_key):
	return cursor_key is None or key > cursor_key


# --- rows ---------------------------------------------------------------------------------


def _key(at, family, row_id):
	return (at, RANK[family], row_id)


def event_row(ev, incident, tz, deployment_event=None):
	"""A Wolf Event as a timeline row. `ev` has the get_timeline columns plus a parsed `payload`."""
	linked = ev.get("incident") == incident
	is_deployment = ev.get("event_type") == "deployment" or ev.get("name") == deployment_event
	row = {
		"id": f"event:{ev['name']}",
		"source_id": ev["name"],
		"kind": "deployment" if is_deployment else "event",
		"family": "evidence" if linked else "context",
		"relation": "linked" if linked else "context",
		"at": window.to_utc(ev["timestamp"], tz),
		"summary": ev.get("title") or ev["name"],
		"severity": ev.get("severity"),
		"event_type": ev.get("event_type"),
		"category": ev.get("category"),
		"source": ev.get("source"),
		"scope": {k: ev.get(k) for k in ("site", "bench", "host")},
		# what the record holds: first and last time it was seen, and how many repeats it absorbed
		"count": ev.get("count") or 1,
		"first_seen": window.to_utc(ev["timestamp"], tz),
		"last_seen": window.to_utc(ev.get("last_seen"), tz),
		"details": ev.get("payload") or {},
	}
	if ev.get("name") == deployment_event:
		row["relation"] = "preceding_change"  # linked as the deployment before it, not as its cause
	return _key(ev["timestamp"], "event", row["id"]), row


def deployment_unavailable_row(incident, deployment_at, tz):
	"""The incident remembers a deployment time, but the event itself was deleted (retention)."""
	row = {
		"id": f"deployment:{incident}",
		"source_id": None,
		"kind": "deployment",
		"family": "context",
		"relation": "preceding_change",
		"at": window.to_utc(deployment_at, tz),
		"summary": "Deployment (details no longer available)",
		"unavailable": True,
		"details": {},
	}
	return _key(deployment_at, "event", row["id"]), row


def activity_id(entry, incident):
	kind = entry.get("kind")
	if kind in ("comment", "note") and entry.get("name"):
		return f"activity:comment:{entry['name']}"
	if entry.get("version"):
		return f"activity:version:{entry['version']}:{entry.get('field')}"
	at = entry.get("timestamp")
	stamp = at.isoformat() if isinstance(at, datetime) else str(at or "")
	if kind == "opened":
		return f"activity:opened:{incident}"
	# written without a Version (rules engine, older incidents): stable from what it records
	return f"activity:{kind}:{entry.get('to') or entry.get('field') or ''}:{stamp}"


def activity_row(entry, incident, tz):
	"""An operator activity entry (wolf/workflow.get_activity) as a timeline row."""
	row_id = activity_id(entry, incident)
	row = {
		"id": row_id,
		"source_id": entry.get("name") or entry.get("version"),
		"kind": "activity",
		"family": "activity",
		"activity": entry.get("kind"),
		"at": window.to_utc(entry["timestamp"], tz),
		"summary": entry.get("text") or "",
		"user": entry.get("user"),
		"user_name": entry.get("user_name"),
		"auto": bool(entry.get("auto")),
		# comments and notes as plain text: the UI renders text, never HTML
		"text": entry.get("plain") if entry.get("kind") in ("comment", "note") else None,
	}
	return _key(entry["timestamp"], "activity", row_id), row


def delivery_row(d, tz):
	"""A notification attempt (Wolf Delivery). `ok` means the endpoint accepted it, nothing more."""
	row_id = f"delivery:{d['name']}"
	channel = d.get("channel_type") or d.get("kind") or "channel"
	result = "accepted" if d.get("ok") else "failed"
	row = {
		"id": row_id,
		"source_id": d["name"],
		"kind": "delivery",
		"family": "delivery",
		"at": window.to_utc(d["at"], tz),
		"summary": f"{channel} {d.get('action') or 'notification'} {result}"
		+ (f" ({d['http_status']})" if d.get("http_status") else ""),
		"channel": channel,
		"action": d.get("action"),
		"target": d.get("target"),
		"digest": bool(d.get("digest")),
		"result": result,
		"http_status": d.get("http_status") or None,
		"error": d.get("error") or None,
	}
	return _key(d["at"], "delivery", row_id), row


# --- paging -------------------------------------------------------------------------------


def page(keyed_rows, limit, cursor_key=None, more=False):
	"""The first `limit` rows after the cursor, in order, plus the paging block.

	`keyed_rows` holds, per source, either all its rows or its first limit + 1 rows after the
	cursor (`more` says whether any bounded source hit that bound). Rows past a source's bound
	are later than all it returned, so the first `limit` rows of the merge are the right ones.
	"""
	rows = sorted((kr for kr in keyed_rows if after(kr[0], cursor_key)), key=lambda kr: kr[0])
	chosen = rows[:limit]
	has_more = len(rows) > limit or more
	return [r for _, r in chosen], {
		"has_more": has_more,
		"next_cursor": encode_cursor(chosen[-1][0]) if has_more and chosen else None,
		"limit": limit,
	}
