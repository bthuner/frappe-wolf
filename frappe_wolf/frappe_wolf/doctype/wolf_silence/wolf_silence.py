# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime


class WolfSilence(Document):
	"""Maintenance window: matching incidents still open, but nobody is notified."""

	def validate(self):
		if get_datetime(self.ends_at) <= get_datetime(self.starts_at):
			frappe.throw(_("Ends At must be after Starts At"))
		for field in ("host", "bench", "site", "reason"):
			if self.get(field):
				self.set(field, self.get(field).strip())
