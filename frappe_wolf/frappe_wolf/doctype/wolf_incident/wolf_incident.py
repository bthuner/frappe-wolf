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
