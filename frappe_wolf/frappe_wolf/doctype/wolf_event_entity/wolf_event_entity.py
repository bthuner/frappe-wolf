# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WolfEventEntity(Document):
	pass


def on_doctype_update():
	frappe.db.add_index("Wolf Event Entity", ["kind", "value", "at"])
	frappe.db.add_index("Wolf Event Entity", ["event"])
