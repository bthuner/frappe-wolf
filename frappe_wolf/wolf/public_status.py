"""What a public status page may show (pure, unit tested).

Only these fields of an incident ever leave the server: a public title, severity, a public
status label and start/end times. Never hosts, benches, rule names, summaries or payloads.
An incident without a Public Title is shown with a generic title for its severity, because
internal titles often name hosts, mounts or jobs.
"""

import re

from frappe_wolf.wolf.incident_ops import SEVERITY_RANK, to_datetime

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
GENERIC_TITLE = {
	"critical": "Service disruption",
	"warning": "Degraded performance",
	"info": "Minor issue",
}
PUBLIC_STATUS = {"Open": "Investigating", "Acknowledged": "Identified", "Resolved": "Resolved"}
OVERALL = {
	None: ("operational", "All systems operational"),
	"info": ("operational", "All systems operational"),
	"warning": ("degraded", "Degraded performance"),
	"critical": ("outage", "Service disruption"),
}
MAX_HISTORY_DAYS = 90


def normalize_slug(value):
	"""'My Site.example.com' -> 'my-site-example-com'. Returns '' when nothing usable is left."""
	slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
	slug = re.sub(r"-{2,}", "-", slug)[:64].strip("-")
	return slug if SLUG_RE.match(slug) else ""


def history_days(value, default=7):
	try:
		days = int(value)
	except (TypeError, ValueError):
		days = default
	return max(0, min(days, MAX_HISTORY_DAYS))


def visible(row, min_severity="warning"):
	return SEVERITY_RANK.get(row.get("severity"), 1) >= SEVERITY_RANK.get(min_severity or "warning", 1)


def _fmt(ts):
	ts = to_datetime(ts)
	return ts.strftime("%Y-%m-%d %H:%M") if ts else ""


def public_incident(row):
	sev = row.get("severity") if row.get("severity") in GENERIC_TITLE else "warning"
	title = (row.get("public_title") or "").strip() or GENERIC_TITLE[sev]
	return {
		"title": title[:140],
		"severity": sev,
		"status": PUBLIC_STATUS.get(row.get("status"), "Investigating"),
		"started_at": _fmt(row.get("started_at")),
		"resolved_at": _fmt(row.get("resolved_at")) if row.get("status") == "Resolved" else "",
	}


def build_view(page, open_rows, history_rows, tz_label=""):
	"""Everything the template renders. page: title, description, min_severity."""
	min_sev = page.get("min_severity") or "warning"
	current = [public_incident(r) for r in open_rows if visible(r, min_sev)]
	worst = None
	for item in current:
		if worst is None or SEVERITY_RANK[item["severity"]] > SEVERITY_RANK[worst]:
			worst = item["severity"]
	state, label = OVERALL[worst]
	days = {}
	for r in history_rows:
		if not visible(r, min_sev):
			continue
		item = public_incident(r)
		days.setdefault(item["started_at"][:10], []).append(item)
	return {
		"status_title": (page.get("title") or "Status")[:140],
		"intro": (page.get("description") or "")[:1000],
		"overall_state": state,
		"overall_label": label,
		"current": current,
		"history": [{"day": d, "incidents": days[d]} for d in sorted(days, reverse=True)],
		"history_days": history_days(page.get("show_history_days")),
		"tz_label": tz_label or "",
	}
