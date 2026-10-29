"""Incident workflow rules: merge, split, activity feed, postmortem snapshot.

Pure python (no frappe imports) so it is unit tested in tests/test_incident_ops.py.
Rows are plain dicts (or frappe._dict, which is a dict).
"""

import json
from datetime import datetime
from html import escape

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
# Version entries and the incident's own fields describe the same ack/resolve; an entry
# closer than this to acknowledged_at/resolved_at counts as covering it.
SAME_ACTION_SECONDS = 120
COMMENT_TYPES = ("Comment", "Info", "Assigned", "Assignment Completed")
TRACKED_FIELDS = ("status", "severity", "assigned_to", "merged_into")


def to_datetime(value):
	"""datetime, 'YYYY-MM-DD HH:MM:SS[.ffffff]' or None -> datetime or None."""
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value
	text = str(value).strip().replace("T", " ")
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	return None


def max_severity(*values):
	known = [v for v in values if v in SEVERITY_RANK]
	return max(known, key=SEVERITY_RANK.get) if known else None


def _earliest(*values):
	dts = [d for d in (to_datetime(v) for v in values) if d]
	return min(dts) if dts else None


def _latest(*values):
	dts = [d for d in (to_datetime(v) for v in values) if d]
	return max(dts) if dts else None


# ---- merge ---------------------------------------------------------------------------------


def check_merge(source, target):
	"""Returns an error message, or None when `source` may be merged into `target`."""
	if not source or not target:
		return "Both incidents must exist."
	if source.get("name") == target.get("name"):
		return "An incident cannot be merged into itself."
	if source.get("merged_into"):
		return f"{source['name']} was already merged into {source['merged_into']}."
	if target.get("merged_into"):
		return f"{target['name']} was merged into {target['merged_into']}: merge into that incident instead."
	return None


def merged_target_fields(source, target):
	"""Field updates for the target so it covers the source's whole window and severity."""
	out = {
		"started_at": _earliest(source.get("started_at"), target.get("started_at")),
		"last_event_at": _latest(source.get("last_event_at"), target.get("last_event_at")),
		"severity": max_severity(source.get("severity"), target.get("severity")) or target.get("severity"),
	}
	if not target.get("deployment_event") and source.get("deployment_event"):
		out["deployment_event"] = source["deployment_event"]
		out["deployment_at"] = source.get("deployment_at")
	return {k: v for k, v in out.items() if v is not None and v != target.get(k)}


def follow_merges(name, merged_into_of, max_depth=10):
	"""Final incident of a merge chain. `merged_into_of(name)` returns the next hop or None.

	Returns None on a cycle or a chain longer than max_depth.
	"""
	seen = set()
	while name and len(seen) <= max_depth:
		if name in seen:
			return None
		seen.add(name)
		nxt = merged_into_of(name)
		if not nxt:
			return name
		name = nxt
	return None


# ---- split ---------------------------------------------------------------------------------


def parse_names(value):
	"""A JSON list, a comma/newline separated string or a list -> unique names, order kept."""
	if value is None:
		return []
	if isinstance(value, str):
		text = value.strip()
		if text.startswith("["):
			try:
				value = json.loads(text)
			except ValueError:
				value = []
		else:
			value = text.replace(",", "\n").splitlines()
	out = []
	for v in value:
		v = str(v).strip()
		if v and v not in out:
			out.append(v)
	return out


def check_split(linked, selected):
	"""Returns (error, names). Selected events must be linked to the incident, and some must stay."""
	names = parse_names(selected)
	if not names:
		return "Select at least one event to move to the new incident.", []
	linked = set(linked)
	foreign = [n for n in names if n not in linked]
	if foreign:
		return f"Not linked to this incident: {', '.join(foreign[:5])}.", []
	if len(names) >= len(linked):
		return "Keep at least one event in this incident (to move everything, edit the incident instead).", []
	return None, names


def split_fields(events):
	"""started_at / last_event_at / severity of a new incident made of these event rows."""
	return {
		"started_at": _earliest(*(e.get("timestamp") for e in events)),
		"last_event_at": _latest(*(e.get("last_seen") or e.get("timestamp") for e in events)),
		"severity": max_severity(*(e.get("severity") for e in events)) or "warning",
	}


def split_title(source_title, events):
	ordered = sorted(events, key=lambda e: to_datetime(e.get("timestamp")) or datetime.min)
	first = ordered[0].get("title") if ordered else None
	return (first or f"Split from {source_title}")[:255]


# ---- activity feed -------------------------------------------------------------------------


def _version_changes(data):
	if isinstance(data, str):
		try:
			data = json.loads(data or "{}")
		except ValueError:
			return []
	return [c for c in (data or {}).get("changed") or [] if isinstance(c, (list, tuple)) and len(c) == 3]


def build_activity(incident, versions, comments):
	"""Status changes, assignments and comments of an incident as timeline entries.

	incident: dict with name, started_at, alert_rule, owner, acknowledged_at/by, resolved_at/by.
	versions: Version rows (owner, creation, data). comments: Comment rows (name, comment_type,
	content, owner, comment_email, creation). Entries are dicts with kind ("opened", "status",
	"field", "comment", "note"), timestamp, user and kind-specific keys, sorted by timestamp.
	Comment content is passed through untouched: the caller sanitizes it.
	"""
	out = []
	started = to_datetime(incident.get("started_at"))
	if started:
		out.append(
			{
				"kind": "opened",
				"timestamp": started,
				"user": None if incident.get("alert_rule") else incident.get("owner"),
				"rule": incident.get("alert_rule"),
			}
		)
	status_seen = {"Acknowledged": [], "Resolved": []}
	for v in versions or []:
		ts = to_datetime(v.get("creation"))
		for field, old, new in _version_changes(v.get("data")):
			if field not in TRACKED_FIELDS or old == new:
				continue
			kind = "status" if field == "status" else "field"
			out.append({"kind": kind, "field": field, "from": old, "to": new, "timestamp": ts, "user": v.get("owner")})
			if field == "status" and new in status_seen and ts:
				status_seen[new].append(ts)

	def covered(status, at):
		at = to_datetime(at)
		return at is None or any(abs((t - at).total_seconds()) <= SAME_ACTION_SECONDS for t in status_seen[status])

	# Changes written without a Version (the rules engine's auto-resolve, older incidents).
	for status, at_field, by_field in (
		("Acknowledged", "acknowledged_at", "acknowledged_by"),
		("Resolved", "resolved_at", "resolved_by"),
	):
		at = to_datetime(incident.get(at_field))
		if at and not covered(status, at):
			out.append(
				{
					"kind": "status",
					"field": "status",
					"from": None,
					"to": status,
					"timestamp": at,
					"user": incident.get(by_field) or None,
					"auto": not incident.get(by_field),
				}
			)

	for c in comments or []:
		ctype = c.get("comment_type")
		if ctype not in COMMENT_TYPES:
			continue
		out.append(
			{
				"kind": "comment" if ctype == "Comment" else "note",
				"comment_type": ctype,
				"name": c.get("name"),
				"content": c.get("content") or "",
				"timestamp": to_datetime(c.get("creation")),
				"user": c.get("comment_email") or c.get("owner"),
			}
		)
	out.sort(key=lambda e: e["timestamp"] or datetime.min)
	return out


def describe_activity(entry, user_label=None):
	"""One line of plain text for an activity entry (postmortem snapshot, tests)."""
	who = user_label or entry.get("user") or ("Wolf" if entry.get("auto") or entry.get("kind") == "opened" else "")
	kind = entry.get("kind")
	if kind == "opened":
		return f"Incident opened by rule {entry['rule']}" if entry.get("rule") else f"Incident opened by {who}"
	if kind == "status":
		verb = {"Acknowledged": "acknowledged", "Resolved": "resolved", "Open": "re-opened"}.get(entry.get("to"))
		verb = verb or f"set status to {entry.get('to')}"
		if entry.get("auto"):
			return f"Automatically {verb} (condition cleared)" if entry.get("to") == "Resolved" else f"{verb.capitalize()}"
		return f"{who} {verb}".strip()
	if kind == "field":
		label = entry.get("field", "").replace("_", " ")
		if not entry.get("to"):
			return f"{who} cleared {label}".strip()
		return f"{who} set {label} to {entry.get('to')}".strip()
	if kind == "comment":
		return f"{who} commented".strip()
	return entry.get("content") or ""


# ---- postmortem ----------------------------------------------------------------------------


def _fmt(ts):
	ts = to_datetime(ts)
	return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def minutes_between(start, end):
	start, end = to_datetime(start), to_datetime(end)
	if not start or not end or end < start:
		return None
	return round((end - start).total_seconds() / 60.0, 1)


def format_duration(minutes):
	if minutes is None:
		return "unknown"
	total = int(round(minutes))
	days, rest = divmod(total, 1440)
	hours, mins = divmod(rest, 60)
	parts = [f"{days} d"] if days else []
	if hours:
		parts.append(f"{hours} h")
	if mins or not parts:
		parts.append(f"{mins} min")
	return " ".join(parts)


def timeline_snapshot_html(events, activity, user_label=None, limit=200):
	"""Frozen, escaped HTML list of what happened: linked events, deployments, status changes.

	`events` are get_timeline rows with a boolean `linked`; context events of the same scope that
	were not part of the incident are left out, deployments are kept.
	"""
	user_label = user_label or (lambda u: u)
	rows = []
	for e in events or []:
		if not (e.get("linked") or e.get("event_type") == "deployment"):
			continue
		count = f" (x{e['count']})" if (e.get("count") or 0) > 1 else ""
		scope = " / ".join(x for x in (e.get("site"), e.get("bench"), e.get("host")) if x)
		text = f"[{e.get('severity') or ''}] {e.get('event_type') or ''}: {e.get('title') or ''}{count}"
		rows.append((to_datetime(e.get("timestamp")), text + (f" - {scope}" if scope else "")))
	for a in activity or []:
		if a.get("kind") == "comment":
			continue
		rows.append((to_datetime(a.get("timestamp")), describe_activity(a, user_label(a.get("user")) if a.get("user") else None)))
	rows.sort(key=lambda r: r[0] or datetime.min)
	truncated = len(rows) > limit
	rows = rows[:limit]
	items = "".join(f"<li><strong>{escape(_fmt(ts))}</strong> {escape(text)}</li>" for ts, text in rows)
	more = f"<p><em>{len(rows)} first entries shown.</em></p>" if truncated else ""
	return f"<ul>{items}</ul>{more}" if items else "<p>No events.</p>"


def postmortem_defaults(incident, snapshot_html):
	"""Prefilled fields of a new Wolf Postmortem for a resolved incident."""
	duration = minutes_between(incident.get("started_at"), incident.get("resolved_at"))
	scope = " / ".join(x for x in (incident.get("site"), incident.get("bench"), incident.get("host")) if x) or "all"
	e = escape
	summary = incident.get("summary") or ""
	impact = (
		f"<p><strong>Scope:</strong> {e(scope)}<br>"
		f"<strong>Severity:</strong> {e(incident.get('severity') or '')}<br>"
		f"<strong>Started:</strong> {e(_fmt(incident.get('started_at')))}<br>"
		f"<strong>Resolved:</strong> {e(_fmt(incident.get('resolved_at')))}<br>"
		f"<strong>Duration:</strong> {e(format_duration(duration))}<br>"
		f"<strong>Events:</strong> {e(str(incident.get('event_count') or 0))}</p>"
		"<p>Who and what was affected (users, sites, routes, jobs)?</p>"
	)
	deploy = ""
	if incident.get("deployment_at"):
		deploy = f"<p>A deployment happened shortly before, at {e(_fmt(incident['deployment_at']))}.</p>"
	return {
		"title": f"Postmortem: {incident.get('title') or incident.get('name')}"[:255],
		"summary": f"<p>{e(summary)}</p>" if summary else "<p>What happened, in two or three sentences.</p>",
		"impact": impact,
		"root_cause": deploy + "<p>Why did it happen? Ask why until the answer is something you can fix.</p>",
		"lessons_learned": "<p>What went well, what went badly, where we got lucky.</p>",
		"timeline_snapshot": snapshot_html,
		"duration_minutes": duration,
	}

