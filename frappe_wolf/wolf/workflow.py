"""Incident workflow on the database: assignment, activity, manual incidents, merge, split,
postmortems. Decisions live in incident_ops (pure); this module reads and writes."""

import frappe
from frappe import _
from frappe.utils import get_fullname

from frappe_wolf.wolf import incident_ops
from frappe_wolf.wolf.incidents import get_timeline, latest_deployment, refresh_event_count
from frappe_wolf.wolf.spa import MANAGER_ROLES  # the Wolf UI offers what these roles may do
MOVED_COMMENT_TYPES = ("Comment", "Info")


def require_manager(*docs):
	frappe.only_for(MANAGER_ROLES)
	for doc in docs:
		doc.check_permission("write")


def note(incident, text):
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Wolf Incident",
			"reference_name": incident,
			"content": text,
			"comment_email": frappe.session.user,
			"comment_by": get_fullname(frappe.session.user),
		}
	).insert(ignore_permissions=True)


def add_comment(incident, text):
	"""A user comment on the incident timeline (plain text, escaped, newlines kept)."""
	text = (text or "").strip()
	if not text:
		frappe.throw(_("Write a comment first."))
	content = "<br>".join(frappe.utils.escape_html(line) for line in text.splitlines())
	doc = frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Wolf Incident",
			"reference_name": incident,
			"content": content,
			"comment_email": frappe.session.user,
			"comment_by": get_fullname(frappe.session.user),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


# ---- assignment ----------------------------------------------------------------------------


def _close_todos(incident, user=None, status="Cancelled"):
	filters = {"reference_type": "Wolf Incident", "reference_name": incident, "status": "Open"}
	if user:
		filters["allocated_to"] = user
	for name in frappe.get_all("ToDo", filters=filters, pluck="name"):
		todo = frappe.get_doc("ToDo", name)
		todo.status = status
		todo.flags.ignore_permissions = True
		todo.save()


def sync_assignment(doc, previous):
	"""Keep Frappe's assignment (ToDo, sidebar, notification) in step with `assigned_to`.

	frappe.desk.form.assign_to.add creates the ToDo, notifies the assignee and writes the
	"Assigned" comment shown on the timeline. Closing a ToDo may clear assigned_to, so the
	field is written back at the end.
	"""
	if frappe.flags.wolf_syncing_assignment:
		return
	frappe.flags.wolf_syncing_assignment = True
	try:
		if previous and previous != doc.assigned_to:
			_close_todos(doc.name, previous)
		if doc.assigned_to:
			from frappe.desk.form import assign_to

			assign_to.add(
				{
					"assign_to": [doc.assigned_to],
					"doctype": doc.doctype,
					"name": doc.name,
					"description": doc.title,
					"notify": 1,
				}
			)
		frappe.db.set_value(doc.doctype, doc.name, "assigned_to", doc.assigned_to, update_modified=False)
		# assign_to.add bumps `modified`; keep the in-memory doc saveable in this request
		doc.modified = frappe.db.get_value(doc.doctype, doc.name, "modified")
	finally:
		frappe.flags.wolf_syncing_assignment = False


# ---- activity ------------------------------------------------------------------------------


def get_activity(inc):
	versions = frappe.get_all(
		"Version",
		filters={"ref_doctype": "Wolf Incident", "docname": inc.name},
		fields=["owner", "creation", "data"],
		order_by="creation asc",
		limit=200,
	)
	comments = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Wolf Incident",
			"reference_name": inc.name,
			"comment_type": ("in", incident_ops.COMMENT_TYPES),
		},
		fields=["name", "comment_type", "content", "owner", "comment_email", "creation"],
		order_by="creation asc",
		limit=300,
	)
	entries = incident_ops.build_activity(inc.as_dict(), versions, comments)
	from frappe.utils.html_utils import clean_html

	for e in entries:
		e["user_name"] = get_fullname(e["user"]) if e.get("user") else None
		if e["kind"] == "comment":
			e["content"] = clean_html(e["content"])
		elif e["kind"] == "note":
			e["content"] = frappe.utils.strip_html(e["content"])
		e["text"] = incident_ops.describe_activity(e, e["user_name"])
		e["timestamp"] = str(e["timestamp"]) if e.get("timestamp") else ""
	return entries


# ---- manual incidents ----------------------------------------------------------------------


def open_from_event(event):
	ev = frappe.get_doc("Wolf Event", event)
	frappe.only_for(MANAGER_ROLES)
	ev.check_permission("read")
	if ev.incident:
		frappe.throw(_("This event already belongs to incident {0}.").format(ev.incident))
	deploy = latest_deployment(ev.host, ev.bench, ev.site, before=ev.timestamp) if ev.event_type != "deployment" else None
	doc = frappe.get_doc(
		{
			"doctype": "Wolf Incident",
			"title": (ev.title or ev.name)[:255],
			"status": "Open",
			"severity": ev.severity or "warning",
			"started_at": ev.timestamp,
			"last_event_at": ev.last_seen or ev.timestamp,
			"host": ev.host,
			"bench": ev.bench,
			"site": ev.site,
			"summary": _("Opened by hand from event {0}.").format(ev.name),
			"deployment_event": deploy.name if deploy else None,
			"deployment_at": deploy.timestamp if deploy else None,
		}
	)
	doc.insert()
	frappe.db.set_value("Wolf Event", ev.name, "incident", doc.name, update_modified=False)
	refresh_event_count(doc.name)
	note(doc.name, f"Opened by hand from event {ev.name}.")
	return doc.name


# ---- merge / split -------------------------------------------------------------------------


def merge(source, target):
	"""Move the events and comments of `source` into `target`; source becomes Resolved, merged."""
	src = frappe.get_doc("Wolf Incident", source)
	tgt = frappe.get_doc("Wolf Incident", target)
	require_manager(src, tgt)
	error = incident_ops.check_merge(src.as_dict(), tgt.as_dict())
	if error:
		frappe.throw(_(error))

	moved = frappe.db.count("Wolf Event", {"incident": src.name})
	frappe.db.sql(
		"update `tabWolf Event` set incident = %(target)s where incident = %(source)s",
		{"source": src.name, "target": tgt.name},
	)
	comments = frappe.db.count(
		"Comment",
		{"reference_doctype": "Wolf Incident", "reference_name": src.name, "comment_type": ("in", MOVED_COMMENT_TYPES)},
	)
	frappe.db.sql(
		"""update `tabComment` set reference_name = %(target)s
		where reference_doctype = 'Wolf Incident' and reference_name = %(source)s
			and comment_type in %(types)s""",
		{"source": src.name, "target": tgt.name, "types": MOVED_COMMENT_TYPES},
	)
	updates = incident_ops.merged_target_fields(src.as_dict(), tgt.as_dict())
	if updates:
		frappe.db.set_value("Wolf Incident", tgt.name, updates, update_modified=False)
	refresh_event_count(tgt.name)

	_close_todos(src.name, status="Closed")
	src.reload()
	if src.status != "Resolved":
		src.status = "Resolved"
		src.resolved_by = frappe.session.user
	src.merged_into = tgt.name
	src.summary = f"Merged into {tgt.name}."
	src.save()
	refresh_event_count(src.name)

	note(tgt.name, f"Merged {src.name} ({src.title}) into this incident: {moved} event(s), {comments} comment(s).")
	note(src.name, f"Merged into {tgt.name}.")
	return {"target": tgt.name, "events": moved, "comments": comments}


def split(incident, events, title=None):
	"""Move some linked events of `incident` to a new incident (no rule: rules never auto-resolve it)."""
	src = frappe.get_doc("Wolf Incident", incident)
	require_manager(src)
	if src.merged_into:
		frappe.throw(_("This incident was merged into {0}.").format(src.merged_into))
	linked = frappe.get_all(
		"Wolf Event",
		filters={"incident": src.name},
		fields=["name", "title", "severity", "timestamp", "last_seen"],
		limit=5000,
	)
	error, names = incident_ops.check_split([r.name for r in linked], events)
	if error:
		frappe.throw(_(error))
	rows = [r for r in linked if r.name in set(names)]
	fields = incident_ops.split_fields(rows)
	resolved = src.status == "Resolved"
	doc = frappe.get_doc(
		{
			"doctype": "Wolf Incident",
			"title": (title or "").strip()[:255] or incident_ops.split_title(src.title, rows),
			"status": "Resolved" if resolved else "Open",
			"severity": fields["severity"],
			"started_at": fields["started_at"],
			"last_event_at": fields["last_event_at"],
			"resolved_at": src.resolved_at if resolved else None,
			"resolved_by": frappe.session.user if resolved else None,
			"host": src.host,
			"bench": src.bench,
			"site": src.site,
			"split_from": src.name,
			"summary": f"Split from {src.name}.",
		}
	)
	doc.insert()
	frappe.db.sql(
		"update `tabWolf Event` set incident = %(new)s where incident = %(source)s and name in %(names)s",
		{"new": doc.name, "source": src.name, "names": tuple(names)},
	)
	refresh_event_count(doc.name)
	refresh_event_count(src.name)
	note(doc.name, f"Split from {src.name}: {len(names)} event(s).")
	note(src.name, f"Split {len(names)} event(s) into {doc.name}.")
	return doc.name


def merge_target_for(rule, key):
	"""The open incident a (rule, group key) incident was merged into, if any.

	The rules engine sends further breaches there instead of opening a fresh duplicate.
	"""
	merged = frappe.db.get_value(
		"Wolf Incident",
		{"alert_rule": rule, "group_key": key, "merged_into": ("is", "set")},
		"merged_into",
		order_by="started_at desc",
	)
	if not merged:
		return None
	final = incident_ops.follow_merges(
		merged, lambda n: frappe.db.get_value("Wolf Incident", n, "merged_into")
	)
	if final and frappe.db.get_value("Wolf Incident", final, "status") in ("Open", "Acknowledged"):
		return final
	return None


# ---- postmortem ----------------------------------------------------------------------------


def create_postmortem(incident):
	inc = frappe.get_doc("Wolf Incident", incident)
	frappe.only_for(MANAGER_ROLES)
	inc.check_permission("read")
	existing = inc.get("postmortem") or frappe.db.get_value("Wolf Postmortem", {"incident": inc.name}, "name")
	if existing and frappe.db.exists("Wolf Postmortem", existing):
		return existing
	if inc.merged_into:
		frappe.throw(_("This incident was merged into {0}: write the postmortem there.").format(inc.merged_into))
	if inc.status != "Resolved":
		frappe.throw(_("Resolve the incident before writing its postmortem."))

	events = get_timeline(inc)
	for e in events:
		e["linked"] = e.get("incident") == inc.name
	activity = incident_ops.build_activity(
		inc.as_dict(),
		frappe.get_all(
			"Version",
			filters={"ref_doctype": "Wolf Incident", "docname": inc.name},
			fields=["owner", "creation", "data"],
			order_by="creation asc",
			limit=200,
		),
		frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Wolf Incident", "reference_name": inc.name, "comment_type": "Info"},
			fields=["name", "comment_type", "content", "owner", "comment_email", "creation"],
			order_by="creation asc",
			limit=300,
		),
	)
	for a in activity:
		if a["kind"] == "note":
			a["content"] = frappe.utils.strip_html(a["content"])
	snapshot = incident_ops.timeline_snapshot_html(events, activity, user_label=get_fullname)
	values = incident_ops.postmortem_defaults(inc.as_dict(), snapshot)
	fetched = {f: inc.get(f) for f in ("severity", "started_at", "resolved_at", "site", "bench", "host")}
	doc = frappe.get_doc({"doctype": "Wolf Postmortem", "incident": inc.name, "status": "Draft", **fetched, **values})
	doc.insert()
	frappe.db.set_value("Wolf Incident", inc.name, "postmortem", doc.name, update_modified=False)
	note(inc.name, f"Postmortem {doc.name} started.")
	return doc.name
