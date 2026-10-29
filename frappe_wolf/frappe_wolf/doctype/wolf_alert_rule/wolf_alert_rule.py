# Copyright (c) 2026, Frappe Wolf contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now_datetime

from frappe_wolf.wolf import conditions


class WolfAlertRule(Document):
	def validate(self):
		if cint(self.window_minutes) < 1:
			frappe.throw(_("Window must be at least one minute"))
		if self.operator not in conditions.OPERATORS:
			frappe.throw(_("Unknown operator {0}").format(self.operator))
		conditions.group_columns(self.group_by)
		if self.signal_type == "metric":
			if self.aggregation not in conditions.AGGREGATIONS:
				frappe.throw(_("Unknown aggregation {0}").format(self.aggregation))
		elif not self.event_type:
			frappe.throw(_("Event rules need an event type"))
		if bool(self.active_from) != bool(self.active_to):
			frappe.throw(_("Set both Active From and Active To, or neither"))
		for field in ("fire_after_evaluations", "resolve_after_evaluations"):
			if cint(self.get(field)) < 1:
				self.set(field, 1)
		if self.use_resolve_threshold:
			error = conditions.resolve_threshold_error(self.operator, self.threshold, self.resolve_threshold)
			if error:
				frappe.throw(_(error))
		if cint(self.escalate_after_minutes) < 0:
			frappe.throw(_("Escalate After Minutes cannot be negative"))
		self.validate_channels()
		# The evaluator owns the hysteresis counters: a form opened minutes ago must
		# not write back stale ones.
		if not self.is_new():
			self.evaluation_state = frappe.db.get_value(self.doctype, self.name, "evaluation_state")

	def validate_channels(self):
		from frappe_wolf.wolf import channels as wolf_channels

		for row in list(self.get("channels") or []) + list(self.get("escalation_channels") or []):
			if row.channel_type not in wolf_channels.NATIVE:
				continue
			# Password fields read as asterisks once saved: present is all that matters here.
			cfg = {f: row.get(f) for f in wolf_channels.FIELDS[row.channel_type]}
			missing = wolf_channels.missing(row.channel_type, cfg)
			if missing and row.enabled:
				frappe.throw(
					_("Row {0} ({1}): set {2}").format(row.idx, row.channel_type, ", ".join(_(m) for m in missing))
				)
			bad = wolf_channels.invalid_urls(cfg)
			if bad:
				labels = ", ".join(_(row.meta.get_label(f)) for f in bad)
				frappe.throw(_("Row {0} ({1}): {2} must start with http:// or https://").format(
					row.idx, row.channel_type, labels
				))

	@frappe.whitelist()
	def preview(self):
		"""Dry run: current value per group, without opening incidents."""
		from frappe_wolf.wolf import rules

		frappe.only_for(("System Manager", "Wolf Manager"))
		now = now_datetime()
		since = frappe.utils.add_to_date(now, minutes=-max(cint(self.window_minutes), 1))
		rows = rules.query_metric(self, since) if self.signal_type == "metric" else rules.query_events(self, since)
		return {
			"active": conditions.in_active_window(now.time(), self.active_from, self.active_to),
			"groups": [
				{
					"group": conditions.describe_group(self.group_by, row),
					"value": conditions.format_value(row.get("value")),
					"samples": row.get("samples"),
					"breached": conditions.is_breached(row, self.operator, self.threshold, cint(self.min_samples) or 1),
				}
				for row in rows
			],
		}

	@frappe.whitelist()
	def send_test_notification(self):
		from frappe_wolf.wolf import notify

		frappe.only_for(("System Manager", "Wolf Manager"))
		incident = frappe.get_doc(
			{
				"doctype": "Wolf Incident",
				"title": f"Test notification for {self.rule_name}",
				"severity": self.severity,
				"status": "Resolved",
				"started_at": now_datetime(),
				"resolved_at": now_datetime(),
				"summary": "This is a test sent from the alert rule form.",
			}
		).insert()
		rows = list(self.get("channels") or []) + list(self.get("escalation_channels") or [])
		results = notify.send(incident.name, "test", channels=rows)
		return {"incident": incident.name, "results": results}
