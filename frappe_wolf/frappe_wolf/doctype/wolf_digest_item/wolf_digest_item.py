# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WolfDigestItem(Document):
	"""A warning notification waiting for the hourly digest of its channel."""
