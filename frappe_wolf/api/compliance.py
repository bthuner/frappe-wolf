"""NIS2 / DORA / GDPR deadlines on Wolf Incidents (wolf/compliance.py): reminders and a report draft.

- `check_deadlines` (scheduler, every 5 minutes, central site only): for every flagged incident
  whose deadline is within 6 h (`due`) or passed (`overdue`), records one `regulatory_deadline`
  event per obligation and stage, linked to the incident (so it is on its timeline), and a
  comment. The "Regulatory deadline" preset turns those events into an alert on the channels
  you give it.
- `get_report(incident)`: a Markdown draft (facts, deadlines, timeline, postmortem measures)
  to start the authority's form from. Nothing is sent anywhere.
"""

import re

import frappe
from frappe import _
from frappe.utils import now_datetime

from frappe_wolf import satellite
from frappe_wolf.wolf import compliance, events, incidents, schema

TIMELINE_LIMIT = 100


def serialize(rows, tz):
	from frappe_wolf.wolf import window

	return [{**r, "due_at": window.to_utc(r["due_at"], tz), "done_at": window.to_utc(r["done_at"], tz)} for r in rows]


def _flagged():
	return frappe.get_all(
		"Wolf Incident",
		filters={"merged_into": ("is", "not set")},
		or_filters={flag: 1 for flag in compliance.FLAGS},
		fields=["name", "title", "host", "bench", "site", *compliance.FIELDS],
	)


def check_deadlines(now=None):
	if satellite.is_satellite():
		return []
	now = now or now_datetime()
	recorded = []
	for inc in _flagged():
		for row, stage in compliance.alerts(inc, now):
			fp = schema.fingerprint("regulatory_deadline", inc.name, row["id"], stage)
			if frappe.db.exists("Wolf Event", {"fingerprint": fp}):
				continue  # one reminder per obligation and stage
			when = row["due_at"].strftime("%Y-%m-%d %H:%M")
			title = f"{row['regime']}: {row['label']} {'overdue since' if stage == 'overdue' else 'due by'} {when} ({inc.name})"
			name = events.emit(
				"security",
				title[:255],
				severity="critical" if stage == "overdue" else "warning",
				category="regulatory_deadline",
				source="wolf",
				payload={"incident": inc.name, "obligation": row["id"], "regime": row["regime"], "stage": stage,
				         "due_at": row["due_at"], "estimated": row["estimated"]},  # fmt: skip
				fingerprint=fp,
				host=inc.host, bench=inc.bench, site=inc.site,
			)
			frappe.db.set_value("Wolf Event", name, "incident", inc.name, update_modified=False)
			incidents.refresh_event_count(inc.name)
			frappe.get_doc("Wolf Incident", inc.name).add_comment("Comment", title)
			recorded.append(name)
	return recorded


def _text(html):
	return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(html or ""))).strip()


def _timeline(inc):
	rows = []
	for label, field in (("Incident started", "started_at"), ("Acknowledged", "acknowledged_at"), ("Resolved", "resolved_at")):
		if inc.get(field):
			rows.append((inc.get(field), label))
	for ev in frappe.get_all("Wolf Event", filters={"incident": inc.name}, fields=["timestamp", "title"],
	                         order_by="timestamp asc", limit=TIMELINE_LIMIT):  # fmt: skip
		rows.append((ev.timestamp, ev.title))
	for c in frappe.get_all("Comment", filters={"reference_doctype": "Wolf Incident", "reference_name": inc.name,
	                                            "comment_type": "Comment"},
	                        fields=["creation", "content", "comment_email"], order_by="creation asc", limit=TIMELINE_LIMIT):  # fmt: skip
		rows.append((c.creation, f"{c.comment_email}: {_text(c.content)}"))
	return sorted(rows, key=lambda r: str(r[0]))


@frappe.whitelist()
def get_deadlines(incident):
	"""Deadlines for the Desk form, in site time (naive), like the incident's own fields."""
	inc = frappe.get_doc("Wolf Incident", incident)
	if not frappe.has_permission("Wolf Incident", "read", doc=inc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return compliance.deadlines(inc.as_dict(), now_datetime())


@frappe.whitelist()
def get_report(incident):
	inc = frappe.get_doc("Wolf Incident", incident)
	if not frappe.has_permission("Wolf Incident", "read", doc=inc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	postmortem = None
	if inc.postmortem and frappe.db.exists("Wolf Postmortem", inc.postmortem):
		pm = frappe.get_doc("Wolf Postmortem", inc.postmortem)
		postmortem = {k: _text(pm.get(k)) for k in ("summary", "impact", "root_cause", "lessons_learned")}
		postmortem["action_items"] = [i.as_dict() for i in pm.action_items]
	rows = compliance.deadlines(inc.as_dict(), now_datetime())
	return {
		"incident": inc.name,
		"markdown": compliance.report_markdown(inc.as_dict(), rows, _timeline(inc), postmortem, events.system_tz()),
	}
