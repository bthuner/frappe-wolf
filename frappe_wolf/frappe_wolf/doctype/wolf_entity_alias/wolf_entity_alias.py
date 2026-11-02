# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WolfEntityAlias(Document):
	def validate(self):
		from frappe_wolf.wolf import entities

		ip = entities.normal_ip(self.alias)
		self.alias = ip or entities.normal_name(self.alias)
		self.canonical = entities.normal_name(self.canonical)
		if self.kind == "site" and ip:
			frappe.throw(frappe._("A site alias is a domain, not an address."))

	def on_change(self):
		frappe.cache.delete_value("wolf_entity_aliases")

	def on_trash(self):
		frappe.cache.delete_value("wolf_entity_aliases")



def on_doctype_update():
	frappe.db.add_unique("Wolf Entity Alias", ["kind", "alias"], constraint_name="unique_kind_alias")
