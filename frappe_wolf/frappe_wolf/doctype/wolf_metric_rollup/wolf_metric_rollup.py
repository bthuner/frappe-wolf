# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WolfMetricRollup(Document):
	pass


def on_doctype_update():
	# Reads filter one resolution and a bucket range, per metric (rules) or per host (charts).
	frappe.db.add_index("Wolf Metric Rollup", ["resolution", "metric_name", "bucket_start"])
	frappe.db.add_index("Wolf Metric Rollup", ["resolution", "host", "bucket_start"])
