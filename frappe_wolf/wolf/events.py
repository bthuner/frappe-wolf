"""Storage of Wolf Events and Metric Samples."""

import os
import socket

import frappe
from frappe.utils import add_to_date, get_system_timezone, now_datetime

from frappe_wolf.wolf import schema


def system_tz():
	return get_system_timezone() or "UTC"


def local_scope():
	"""Host/bench/site of the Frappe process itself, matching what the agent reports."""
	return {
		"host": socket.gethostname(),
		"bench": os.path.basename(os.path.abspath(frappe.utils.get_bench_path())),
		"site": frappe.local.site,
	}


def insert_metrics(rows, agent=None):
	"""Bulk insert normalized metric dicts (see schema.normalize_metric)."""
	if not rows:
		return 0
	now = now_datetime()
	fields = [
		"name", "creation", "modified", "owner", "modified_by", "docstatus",
		"metric_name", "value", "timestamp", "host", "bench", "site", "label_key", "labels",
	]  # fmt: skip
	values = [
		(
			frappe.generate_hash(length=12), now, now, "Administrator", "Administrator", 0,
			r["metric_name"], r["value"], r["timestamp"], r["host"], r["bench"], r["site"],
			r["label_key"], r["labels"],
		)  # fmt: skip
		for r in rows
	]
	frappe.db.bulk_insert("Wolf Metric Sample", fields, values)
	return len(values)


def insert_event(data, agent=None, dedupe_minutes=None):
	"""Insert a normalized event dict; returns the Wolf Event name.

	With `dedupe_minutes`, an event with the same fingerprint and site seen within
	that window is bumped (count/last_seen) instead of creating a new row.
	"""
	if dedupe_minutes and data.get("fingerprint"):
		existing = frappe.db.get_value(
			"Wolf Event",
			{
				"fingerprint": data["fingerprint"],
				"site": data.get("site"),
				"last_seen": (">=", add_to_date(data["timestamp"], minutes=-dedupe_minutes)),
			},
			["name", "count"],
			as_dict=True,
			order_by="last_seen desc",
		)
		if existing:
			frappe.db.set_value(
				"Wolf Event",
				existing.name,
				{"count": (existing.count or 1) + data.get("count", 1), "last_seen": data["timestamp"]},
				update_modified=False,
			)
			return existing.name

	doc = frappe.get_doc({"doctype": "Wolf Event", "agent": agent, **data})
	doc.flags.ignore_permissions = True
	doc.flags.ignore_links = True
	doc.insert()
	return doc.name


def emit(event_type, title, severity="warning", category=None, source="frappe", payload=None, dedupe_minutes=None, **scope):
	"""Record an event raised by the Wolf app itself (local signals, rule engine)."""
	timestamp = scope.pop("timestamp", None)
	raw = {
		**local_scope(),
		**scope,
		"type": event_type,
		"title": title,
		"severity": severity,
		"category": category,
		"source": source,
		"payload": payload,
	}
	now = now_datetime()
	data = schema.normalize_event(raw, system_tz(), now)
	data["timestamp"] = data["last_seen"] = timestamp or now
	return insert_event(data, dedupe_minutes=dedupe_minutes)


def scope_clause(host=None, bench=None, site=None, prefix=""):
	"""SQL clause matching events that belong to the same bench/site/host.

	A site matches directly; a bench matches on the same host (bench names are only
	unique per host); a host matches host-level events (no bench/site).
	"""
	clauses, params = [], {}
	if site:
		clauses.append(f"{prefix}`site` = %(scope_site)s")
		params["scope_site"] = site
	if bench:
		if host:
			clauses.append(f"({prefix}`bench` = %(scope_bench)s and {prefix}`host` = %(scope_host)s)")
		else:
			clauses.append(f"{prefix}`bench` = %(scope_bench)s")
		params["scope_bench"] = bench
	if host:
		clauses.append(
			f"({prefix}`host` = %(scope_host)s and coalesce({prefix}`bench`, '') = '' and coalesce({prefix}`site`, '') = '')"
		)
		params["scope_host"] = host
	if not clauses:
		return "1=1", params
	return "(" + " or ".join(clauses) + ")", params
