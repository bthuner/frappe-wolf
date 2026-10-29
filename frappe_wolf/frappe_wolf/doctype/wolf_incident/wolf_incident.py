# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class WolfIncident(Document):
	def _validate_links(self):
		# Retention may have purged the deployment event of an old incident; keep the incident
		# saveable (ack/resolve) and keep deployment_at as the record of when it happened.
		if self.deployment_event and not frappe.db.exists("Wolf Event", self.deployment_event):
			self.deployment_event = None
		super()._validate_links()

	def validate(self):
		if self.is_new():
			# Incidents created by hand in Desk (no rule) start now.
			self.started_at = self.started_at or now_datetime()
			self.last_event_at = self.last_event_at or self.started_at
		if self.merged_into:
			if self.merged_into == self.name:
				frappe.throw(_("An incident cannot be merged into itself."))
			if self.status != "Resolved":
				frappe.throw(_("A merged incident stays resolved: work on {0} instead.").format(self.merged_into))
		if self.status == "Resolved" and not self.resolved_at:
			self.resolved_at = now_datetime()
		elif self.status != "Resolved":
			self.resolved_at = None
			self.resolved_by = None
		if self.status == "Acknowledged":
			if not self.acknowledged_by:
				self.acknowledged_by = frappe.session.user
			if not self.acknowledged_at:
				self.acknowledged_at = now_datetime()
		if self.public_title:
			self.public_title = self.public_title.strip()[:140]

	def on_update(self):
		before = self.get_doc_before_save()
		previous = before.assigned_to if before else None
		if previous != self.assigned_to:
			from frappe_wolf.wolf.workflow import sync_assignment

			sync_assignment(self, previous)

	def on_update(self):
		"""Acknowledged / resolved by a person (Desk, form, signed link): tell the channels.

		Auto-resolution by the rule engine writes the status directly and notifies from
		rules.py, so it does not come through here.
		"""
		before = self.get_doc_before_save()
		if not before or before.status == self.status or not self.alert_rule:
			return
		if self.status in ("Acknowledged", "Resolved"):
			from frappe_wolf.wolf import notify

			notify.on_status_change(self.name, self.alert_rule, self.status)

	def on_trash(self):
		frappe.db.delete("Wolf Delivery", {"incident": self.name})

	@frappe.whitelist()
	def acknowledge(self):
		self.status = "Acknowledged"
		self.acknowledged_by = frappe.session.user
		self.acknowledged_at = now_datetime()
		self.save()

	@frappe.whitelist()
	def resolve(self):
		self.status = "Resolved"
		self.resolved_by = frappe.session.user
		self.save()

	@frappe.whitelist()
	def assign_to_me(self):
		self.assigned_to = frappe.session.user
		self.save()
