# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class WolfJournalEntry(Document):
	"""Append-only: created by api/journal.py, signed by its chaining job, never edited."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Wolf Journal entries cannot be edited."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Wolf Journal entries cannot be deleted."), frappe.PermissionError)
