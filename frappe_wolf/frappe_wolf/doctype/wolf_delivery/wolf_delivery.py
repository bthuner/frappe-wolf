# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WolfDelivery(Document):
	"""One notification attempt (see frappe_wolf.wolf.notify.record)."""
