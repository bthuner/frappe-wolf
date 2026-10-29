# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class WolfIncident(Document):
	def validate(self):
		if self.status == "Resolved" and not self.resolved_at:
			self.resolved_at = now_datetime()
		elif self.status != "Resolved":
			self.resolved_at = None
		if self.status == "Acknowledged" and not self.acknowledged_by:
			self.acknowledged_by = frappe.session.user

	@frappe.whitelist()
	def acknowledge(self):
		self.status = "Acknowledged"
		self.acknowledged_by = frappe.session.user
		self.save()

	@frappe.whitelist()
	def resolve(self):
		self.status = "Resolved"
		self.save()
