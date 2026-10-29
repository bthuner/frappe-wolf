# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
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
