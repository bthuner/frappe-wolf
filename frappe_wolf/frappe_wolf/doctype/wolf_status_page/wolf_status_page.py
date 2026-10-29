# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from frappe_wolf.wolf.public_status import history_days, normalize_slug


class WolfStatusPage(Document):
	def before_insert(self):
		# autoname is field:slug, so the slug must be clean before naming
		self.slug = normalize_slug(self.slug or self.title)

	def validate(self):
		# The slug is the document name: renaming the document changes the public address.
		slug = normalize_slug(self.slug if self.is_new() else self.name)
		if not slug:
			frappe.throw(_("Slug must contain letters or digits."))
		self.slug = slug
		for field in ("site", "bench", "title"):
			if self.get(field):
				self.set(field, self.get(field).strip())
		self.show_history_days = history_days(self.show_history_days)
