"""Regulatory reporting deadlines on a Wolf Incident (no frappe imports).

Three regimes, each switched on by a flag on the incident:

- NIS2 Art. 23 (significant incident), counted from when the entity became aware of it:
  early warning within 24 h, incident notification within 72 h, final report no later than one
  month after the incident notification (Art. 23(4)(d)). Until the notification is sent, the
  final report is counted from the notification's own deadline and marked `estimated`.
  An intermediate report is due only when the CSIRT or the authority asks for one
  (Art. 23(4)(c)): its deadline is the one they give. An incident still ongoing when the final
  report is due gets a progress report then, and the final report one month after it is handled
  (Art. 23(4)(e)).
- DORA Art. 19 (major ICT-related incident of a financial entity), with the time limits of
  Delegated Regulation (EU) 2025/301, Art. 5: initial notification within 4 h of classifying the
  incident as major and no later than 24 h after becoming aware of it (4 h from a later
  classification), intermediate report within 72 h of the initial notification, final report
  within one month of the latest intermediate report. The weekend and bank holiday extension of
  Art. 5(4) is not applied: it depends on the entity and its Member State, and an earlier
  deadline is always met.
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
	("nis2_intermediate", "NIS2", "Intermediate report requested by the CSIRT / authority", "nis2_intermediate_at"),
	("dora_initial", "DORA", "Initial notification (4 h after classification, 24 h after awareness)", "dora_initial_at"),
	("dora_intermediate", "DORA", "Intermediate report (72 h after the initial notification)", "dora_intermediate_at"),
	("dora_final", "DORA", "Final report (1 month after the latest intermediate report)", "dora_final_at"),
	("gdpr_notification", "GDPR", "Personal data breach notification to the authority (72 h)", "gdpr_notified_at"),
]
FIELDS = ("nis2_significant", "nis2_aware_at", "nis2_early_warning_at", "nis2_notification_at", "nis2_final_report_at",
          "nis2_intermediate_requested_at", "nis2_intermediate_due_at", "nis2_intermediate_at",
          "dora_major", "dora_aware_at", "dora_classified_at", "dora_initial_at", "dora_intermediate_at", "dora_final_at",
          "gdpr_breach", "gdpr_aware_at", "gdpr_notified_at", "regulatory_reference")  # fmt: skip
FLAGS = ("nis2_significant", "dora_major", "gdpr_breach")


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
		return "done_late" if due and done > due else "done"
	if due is None:
		return "open"  # requested without a deadline: nothing to count down
	if now > due:
		return "overdue"
	if due - now <= WARN_BEFORE:
		return "due_soon"
	return "open"


def deadlines(incident, now):
	"""Rows `{id, regime, label, due_at, done_at, status, estimated}` for the flags set.

	`status`: open, due_soon (within 6 h), overdue, done, done_late. Datetimes stay naive, in
	the site's time zone, like the incident's own fields. `due_at` is None only for a NIS2
	intermediate report requested without a deadline.
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
		if _dt(incident.get("nis2_intermediate_requested_at")):
			row("nis2_intermediate", _dt(incident.get("nis2_intermediate_due_at")))
	dora_aware = _dt(incident.get("dora_aware_at"))
	classified = _dt(incident.get("dora_classified_at"))
	if incident.get("dora_major") and dora_aware and classified:
		row("dora_initial", initial_due(dora_aware, classified))
		initial = _dt(incident.get("dora_initial_at"))
		intermediate_due = (initial or initial_due(dora_aware, classified)) + timedelta(hours=72)
		row("dora_intermediate", intermediate_due, estimated=initial is None)
		intermediate = _dt(incident.get("dora_intermediate_at"))
		row("dora_final", add_months(intermediate or intermediate_due), estimated=intermediate is None)
	gdpr_aware = _dt(incident.get("gdpr_aware_at"))
	if incident.get("gdpr_breach") and gdpr_aware:
		row("gdpr_notification", gdpr_aware + timedelta(hours=72))
	return rows


def initial_due(aware, classified):
	"""DORA initial notification: 4 h after classification, but no later than 24 h after
	awareness; a classification made after those 24 h gives 4 h from itself (2025/301 Art. 5)."""
	four_hours = classified + timedelta(hours=4)
	cap = aware + timedelta(hours=24)
	return four_hours if classified > cap else min(four_hours, cap)


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
	if incident.get("nis2_significant") and _dt(incident.get("nis2_intermediate_requested_at")):
		lines.append(f"- NIS2 intermediate report requested on {_fmt(_dt(incident.get('nis2_intermediate_requested_at')))}")
	if incident.get("dora_major"):
		lines.append(f"- DORA major ICT-related incident, aware since {_fmt(_dt(incident.get('dora_aware_at')))}, "
		             f"classified as major on {_fmt(_dt(incident.get('dora_classified_at')))}")  # fmt: skip
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
	final = next((r for r in rows if r["id"] == "nis2_final_report"), None)
	if final and incident.get("status") != "Resolved" and not final["done_at"]:
		lines += ["", "NIS2 Art. 23(4)(e): if the incident is still ongoing when the final report is due, send a progress "
		          "report then, and the final report within one month of handling the incident."]  # fmt: skip
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
