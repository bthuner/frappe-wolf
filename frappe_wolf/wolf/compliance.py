"""Regulatory reporting deadlines on a Wolf Incident (no frappe imports).

Two regimes, each switched on by a flag on the incident:

- NIS2 Art. 23 (significant incident), counted from when the entity became aware of it:
  early warning within 24 h, incident notification within 72 h, final report no later than one
  month after the incident notification (Art. 23(4)(d)). Until the notification is sent, the
  final report is counted from the notification's own deadline and marked `estimated`.
- GDPR Art. 33 (personal data breach): notification to the supervisory authority (CNIL in
  France) within 72 h of becoming aware of it.

Wolf tracks dates; it does not decide whether an incident is significant or a breach, and it
does not send anything to an authority. The `regulatory_deadline` events (`due` when a
deadline is close, `overdue` once passed) let the alert rules reach the people who do.
"""

import calendar
from datetime import datetime, timedelta

WARN_BEFORE = timedelta(hours=6)

# id, regime, label, done field
OBLIGATIONS = [
	("nis2_early_warning", "NIS2", "Early warning to the CSIRT / authority (24 h)", "nis2_early_warning_at"),
	("nis2_notification", "NIS2", "Incident notification (72 h)", "nis2_notification_at"),
	("nis2_final_report", "NIS2", "Final report (1 month after the notification)", "nis2_final_report_at"),
	("gdpr_notification", "GDPR", "Personal data breach notification to the authority (72 h)", "gdpr_notified_at"),
]
FIELDS = ("nis2_significant", "nis2_aware_at", "nis2_early_warning_at", "nis2_notification_at", "nis2_final_report_at",
          "gdpr_breach", "gdpr_aware_at", "gdpr_notified_at", "regulatory_reference")  # fmt: skip


def add_months(dt, months=1):
	"""Same day `months` later, clamped to the month's last day (31 Jan + 1 month = 28/29 Feb)."""
	month = dt.month - 1 + months
	year = dt.year + month // 12
	month = month % 12 + 1
	day = min(dt.day, calendar.monthrange(year, month)[1])
	return dt.replace(year=year, month=month, day=day)


def _dt(value):
	if value is None or value == "":
		return None
	if isinstance(value, datetime):
		return value
	return datetime.fromisoformat(str(value).replace("Z", ""))


def _status(due, done, now):
	if done:
		return "done_late" if done > due else "done"
	if now > due:
		return "overdue"
	if due - now <= WARN_BEFORE:
		return "due_soon"
	return "open"


def deadlines(incident, now):
	"""Rows `{id, regime, label, due_at, done_at, status, estimated}` for the flags set.

	`status`: open, due_soon (within 6 h), overdue, done, done_late. Datetimes stay naive, in
	the site's time zone, like the incident's own fields.
	"""
	rows = []

	def row(ob_id, due, estimated=False):
		_, regime, label, field = next(o for o in OBLIGATIONS if o[0] == ob_id)
		done = _dt(incident.get(field))
		rows.append({"id": ob_id, "regime": regime, "label": label, "due_at": due, "done_at": done,
		             "status": _status(due, done, now), "estimated": estimated})  # fmt: skip

	nis2_aware = _dt(incident.get("nis2_aware_at"))
	if incident.get("nis2_significant") and nis2_aware:
		row("nis2_early_warning", nis2_aware + timedelta(hours=24))
		notification_due = nis2_aware + timedelta(hours=72)
		row("nis2_notification", notification_due)
		sent = _dt(incident.get("nis2_notification_at"))
		row("nis2_final_report", add_months(sent or notification_due), estimated=sent is None)
	gdpr_aware = _dt(incident.get("gdpr_aware_at"))
	if incident.get("gdpr_breach") and gdpr_aware:
		row("gdpr_notification", gdpr_aware + timedelta(hours=72))
	return rows


def alerts(incident, now):
	"""(row, stage) for each deadline that needs a reminder now: stage `due` or `overdue`."""
	out = []
	for r in deadlines(incident, now):
		if r["status"] == "due_soon":
			out.append((r, "due"))
		elif r["status"] == "overdue":
			out.append((r, "overdue"))
	return out


def _fmt(dt):
	return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


STATUS_TEXT = {"open": "open", "due_soon": "due within 6 h", "overdue": "OVERDUE", "done": "sent on time", "done_late": "sent late"}


def report_markdown(incident, rows, timeline=(), postmortem=None, time_zone=""):
	"""A plain report to start the authority's form from: facts, deadlines, timeline, measures.

	`timeline` is a list of `(at, text)`; `postmortem` a dict with summary, impact, root_cause,
	lessons_learned and action_items. Text Editor fields arrive as HTML: the caller strips it. Nothing in it is sent anywhere.
	"""
	scope = ", ".join(f"{k} {incident.get(k)}" for k in ("site", "bench", "host") if incident.get(k)) or "—"
	lines = [
		f"# Incident report: {incident.get('title') or incident.get('name')}",
		"",
		f"- Wolf incident: {incident.get('name')}",
		f"- Scope: {scope}",
		f"- Severity: {incident.get('severity')}; status: {incident.get('status')}",
		f"- Started: {_fmt(_dt(incident.get('started_at')))}; resolved: {_fmt(_dt(incident.get('resolved_at')))}",
		f"- Times in {time_zone or 'the site time zone'}",
	]
	if incident.get("regulatory_reference"):
		lines.append(f"- Authority reference: {incident['regulatory_reference']}")
	if incident.get("nis2_significant"):
		lines.append(f"- NIS2 significant incident, aware since {_fmt(_dt(incident.get('nis2_aware_at')))}")
	if incident.get("gdpr_breach"):
		lines.append(f"- GDPR personal data breach, aware since {_fmt(_dt(incident.get('gdpr_aware_at')))}")
	lines += ["", "## Deadlines", ""]
	if rows:
		lines += ["| Obligation | Due | Sent | Status |", "|---|---|---|---|"]
		for r in rows:
			due = _fmt(r["due_at"]) + (" (estimated)" if r["estimated"] else "")
			lines.append(f"| {r['label']} | {due} | {_fmt(r['done_at'])} | {STATUS_TEXT[r['status']]} |")
	else:
		lines.append("No regulatory flag is set on this incident.")
	if incident.get("summary"):
		lines += ["", "## Summary", "", str(incident["summary"])]
	if postmortem:
		for key, title in (("summary", "Postmortem summary"), ("impact", "Impact"), ("root_cause", "Root cause"),
		                   ("lessons_learned", "Lessons learned")):  # fmt: skip
			if postmortem.get(key):
				lines += ["", f"## {title}", "", str(postmortem[key])]
		items = postmortem.get("action_items") or []
		if items:
			lines += ["", "## Measures taken or planned", ""]
			lines += [f"- {i.get('description')} ({i.get('status') or 'Open'}"
			          + (f", owner {i['assigned_to']}" if i.get("assigned_to") else "") + ")" for i in items]  # fmt: skip
	if timeline:
		lines += ["", "## Timeline", ""]
		lines += [f"- {_fmt(_dt(at))} {text}" for at, text in timeline]
	lines += ["", "_Prepared from Frappe Wolf. Check every fact before sending it to an authority._"]
	return "\n".join(lines) + "\n"
