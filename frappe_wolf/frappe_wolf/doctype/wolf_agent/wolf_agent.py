# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WolfAgent(Document):
	@frappe.whitelist()
	def rotate_secret(self):
		"""Invalidate the current secret. The agent must re-register."""
		frappe.only_for(("System Manager", "Wolf Manager"))
		self.agent_secret = frappe.generate_hash(length=40)
		self.save()
