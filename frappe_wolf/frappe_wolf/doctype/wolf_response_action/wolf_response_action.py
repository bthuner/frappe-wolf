# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WolfResponseAction(Document):
	"""Audit row of an approved response action (api/response.py creates and updates it)."""
