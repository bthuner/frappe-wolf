# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from frappe_wolf.wolf.incident_ops import minutes_between


class WolfPostmortem(Document):
	def validate(self):
		other = frappe.db.get_value("Wolf Postmortem", {"incident": self.incident, "name": ("!=", self.name)}, "name")
		if other:
			frappe.throw(_("Incident {0} already has postmortem {1}.").format(self.incident, other))
		self.duration_minutes = minutes_between(self.started_at, self.resolved_at)

	def after_insert(self):
		if not frappe.db.get_value("Wolf Incident", self.incident, "postmortem"):
			frappe.db.set_value("Wolf Incident", self.incident, "postmortem", self.name, update_modified=False)

	def on_trash(self):
		frappe.db.set_value(
			"Wolf Incident", {"postmortem": self.name}, "postmortem", None, update_modified=False
		)
