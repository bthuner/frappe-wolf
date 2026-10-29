"""Data of a public status page (guest access): read with fixed, minimal fields only."""

import frappe
from frappe.utils import add_days, now_datetime

from frappe_wolf.wolf import public_status
from frappe_wolf.wolf.events import system_tz

INCIDENT_FIELDS = ["public_title", "severity", "status", "started_at", "resolved_at"]


def _scope_filters(page):
	"""(filters, or_filters): this site's incidents, plus bench-wide ones when a bench is set."""
	base = {"merged_into": ("is", "not set")}
	if page.bench:
		return base, [
			["site", "=", page.site],
			["bench", "=", page.bench],
		]
	return {**base, "site": page.site}, None


def get_public_status(slug, now=None):
	"""The rendered view for /wolf-status/<slug>, or None when there is no enabled page."""
	slug = public_status.normalize_slug(slug)
	if not slug:
		return None
	page = frappe.db.get_value(
		"Wolf Status Page",
		{"name": slug, "enabled": 1},
		["name", "title", "description", "site", "bench", "show_history_days", "min_severity"],
		as_dict=True,
	)
	if not page or not page.site:
		return None
	now = now or now_datetime()
	filters, or_filters = _scope_filters(page)

	def rows(extra, order_by, limit):
		out = frappe.get_all(
			"Wolf Incident",
			filters={**filters, **extra},
			or_filters=or_filters,
			fields=INCIDENT_FIELDS + ["site"],
			order_by=order_by,
			limit=limit,
		)
		if page.bench:
			# bench-wide means no site: another site's incident on the same bench is not ours
			out = [r for r in out if not r.site or r.site == page.site]
		for r in out:
			r.pop("site", None)
		return out

	open_rows = rows({"status": ("in", ("Open", "Acknowledged"))}, "started_at desc", 50)
	days = public_status.history_days(page.show_history_days)
	history = (
		rows({"status": "Resolved", "started_at": (">=", add_days(now, -days))}, "started_at desc", 200)
		if days
		else []
	)
	return public_status.build_view(page, open_rows, history, tz_label=system_tz())
