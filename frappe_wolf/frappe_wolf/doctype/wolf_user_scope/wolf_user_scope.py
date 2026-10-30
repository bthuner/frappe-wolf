# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from frappe_wolf.wolf import access


class WolfUserScope(Document):
	def validate(self):
		self.value = (self.value or "").strip()
		if not self.value:
			frappe.throw(_("Value is required"))
		duplicate = frappe.db.exists(
			"Wolf User Scope",
			{"user": self.user, "scope_type": self.scope_type, "value": self.value, "name": ("!=", self.name or "")},
		)
		if duplicate:
			frappe.throw(_("{0} already has {1} {2}").format(self.user, self.scope_type, self.value))

	def on_update(self):
		access.clear_cache()

	def on_trash(self):
		access.clear_cache()
