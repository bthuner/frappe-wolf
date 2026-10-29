# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WolfMetricSample(Document):
	pass


def on_doctype_update():
	# Rules read one metric over a time range, charts one host, the rollup job a time range.
	frappe.db.add_index("Wolf Metric Sample", ["metric_name", "timestamp"])
	frappe.db.add_index("Wolf Metric Sample", ["host", "timestamp"])
