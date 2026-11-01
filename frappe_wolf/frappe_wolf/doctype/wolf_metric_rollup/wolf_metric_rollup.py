# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WolfMetricRollup(Document):
	pass


def on_doctype_update():
	# Rows are stored in name order, and names start with resolution, metric and bucket
	# (storage.rollup_name): a time range of one metric is read from neighbouring pages.
	# Reads filter one resolution and a bucket range, per metric (rules) or per host (charts).
	frappe.db.add_index("Wolf Metric Rollup", ["resolution", "metric_name", "bucket_start"])
	frappe.db.add_index("Wolf Metric Rollup", ["resolution", "host", "bucket_start"])
	# The rollup job and the purge read and delete one resolution's bucket range. The inventory
	# (api/scope.py) reads which host, bench and site sent anything in a range: with them in the
	# index, it reads the index alone instead of fetching every row of the range.
	frappe.db.add_index("Wolf Metric Rollup", ["resolution", "bucket_start", "host", "bench", "site"])
	# replaced by the index above, of which it is the start
	_drop_index("Wolf Metric Rollup", "resolution_bucket_start_index")
	# no read filters bucket_start without a resolution. The field lost search_index, so the schema
	# sync drops this index already; dropped here too, whatever the database.
	_drop_index("Wolf Metric Rollup", "bucket_start")


def _drop_index(doctype, index_name):
	table = f"tab{doctype}"
	if not frappe.db.has_index(table, index_name):
		return
	if frappe.db.db_type == "postgres":
		frappe.db.sql_ddl(f'DROP INDEX IF EXISTS "{index_name}"')
	else:
		frappe.db.sql_ddl(f"ALTER TABLE `{table}` DROP INDEX `{index_name}`")
