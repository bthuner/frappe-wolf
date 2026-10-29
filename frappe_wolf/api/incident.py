"""Incident workflow actions for Desk: comments, manual incidents, merge, split, postmortem.

Writes need Wolf Manager (or System Manager) plus write permission on the incident; adding a
comment needs read permission only, like Frappe's own comment box.
"""

import frappe

from frappe_wolf.wolf import workflow


@frappe.whitelist(methods=["POST"])
def add_comment(incident, content):
	frappe.get_doc("Wolf Incident", incident).check_permission("read")
	return workflow.add_comment(incident, content)


@frappe.whitelist(methods=["POST"])
def open_from_event(event):
	return workflow.open_from_event(event)


@frappe.whitelist(methods=["POST"])
def merge(source, target):
	return workflow.merge(source, target)


@frappe.whitelist(methods=["POST"])
def split(incident, events, title=None):
	return workflow.split(incident, events, title)


@frappe.whitelist(methods=["POST"])
def create_postmortem(incident):
	return workflow.create_postmortem(incident)
