"""Exercise every Wolf SQL path against a site's database (MariaDB or Postgres).

Run from the bench's sites/ directory:  ../env/bin/python /path/to/ci/db_smoke.py <site>
Inserts demo rows, runs ingest, rules, correlation, timeline, charts and purge,
and prints one line per check ("ok" or "FAIL"). Exits non-zero on the first failure.
CI runs it from ci/integration.sh.
"""

import json
import sys
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

SITE = sys.argv[1] if len(sys.argv) > 1 else "wolf.localhost"
HOST = "smoke-host"


def check(name, fn):
	try:
		out = fn()
		frappe.db.commit()
		print(f"ok    {name}: {out}", flush=True)
	except Exception:
		frappe.db.rollback()
		print(f"FAIL  {name}\n{frappe.get_traceback()}", flush=True)
		print(f"smoke test FAILED at check '{name}'", flush=True)
		sys.exit(1)


def main():
	frappe.init(SITE)
	frappe.connect()
	frappe.set_user("Administrator")
	from frappe_wolf import tasks
	from frappe_wolf.api import ui
	from frappe_wolf.wolf import events, incidents, rules, schema

	print(f"site {SITE} on {frappe.conf.db_type or 'mariadb'}")
	tz, now = events.system_tz(), now_datetime()

	def ingest():
		metrics = [
			schema.normalize_metric(
				{"metric_name": "disk_used_percent", "value": 95, "host": HOST,
				 "timestamp": None, "labels": {"mount": "/"}}, tz, now - timedelta(minutes=m))
			for m in range(4)
		] + [
			schema.normalize_metric({"metric_name": "http_502", "value": 7, "host": HOST, "bench": "b1",
			                         "site": "smoke.localhost"}, tz, now - timedelta(minutes=m))
			for m in range(3)
		]
		n = events.insert_metrics(metrics)
		ev = schema.normalize_event({"type": "deployment", "host": HOST, "bench": "b1", "site": "smoke.localhost",
		                             "severity": "info", "title": "smoke deploy"}, tz, now - timedelta(minutes=5))
		events.insert_event(ev)
		for _ in range(2):
			e = schema.normalize_event({"type": "cron_failure", "category": "scheduled_job", "host": HOST,
			                            "bench": "b1", "site": "smoke.localhost", "severity": "critical",
			                            "title": "smoke cron"}, tz, now)
			incidents.attach_to_open_incident(events.insert_event(e), e)
		dedup = events.emit("error", "smoke dup", fingerprint="smoke-fp", dedupe_minutes=10, host=HOST, site="smoke.localhost")
		again = events.emit("error", "smoke dup", fingerprint="smoke-fp", dedupe_minutes=10, host=HOST, site="smoke.localhost")
		assert dedup == again, "dedupe did not merge"
		return f"{n} metrics, events + dedupe"

	def rule(name, **kw):
		doc = frappe.get_doc("Wolf Alert Rule", name) if frappe.db.exists("Wolf Alert Rule", name) else frappe.new_doc("Wolf Alert Rule")
		doc.update({"rule_name": name, "enabled": 1, "channels": [], "active_from": None, "active_to": None, **kw})
		doc.save()
		return doc

	def evaluate():
		out = {}
		for doc in (
			rule("smoke disk", signal_type="metric", metric_name="disk_used_percent", aggregation="min",
			     operator=">", threshold=90, window_minutes=5, min_samples=3, group_by="host_label", filter_host=HOST),
			rule("smoke 502", signal_type="metric", metric_name="http_502", aggregation="sum", operator=">=",
			     threshold=10, window_minutes=5, group_by="site", require_recent_deployment=1, filter_host=HOST),
			rule("smoke last", signal_type="metric", metric_name="disk_used_percent", aggregation="last",
			     operator=">", threshold=90, window_minutes=5, group_by="none", filter_host=HOST),
			rule("smoke cron", signal_type="event", event_type="cron_failure", min_severity="warning",
			     operator=">=", threshold=1, window_minutes=10, group_by="bench", filter_host=HOST),
		):
			res = rules.evaluate(doc)
			assert res["fired"] or res["updated"], f"{doc.name} did not fire: {res}"
			out[doc.name] = res["fired"] or res["updated"]
		return out

	def correlate():
		e = schema.normalize_event({"type": "error", "host": HOST, "bench": "b1", "site": "smoke.localhost",
		                            "severity": "critical", "title": "smoke 500"}, tz, now)
		inc = incidents.attach_to_open_incident(events.insert_event(e), e)
		site_incident = frappe.db.get_value("Wolf Incident", {"site": "smoke.localhost", "status": "Open"}, "name",
		                                    order_by="started_at desc")
		assert inc, "not attached"
		return f"attached to {inc} (site incident {site_incident})"

	def timeline():
		inc = frappe.db.get_value("Wolf Incident", {"alert_rule": "smoke 502"}, "name", order_by="creation desc")
		rows = ui.get_incident_timeline(inc)["events"]
		assert any(r.event_type == "deployment" for r in rows), "deployment missing from timeline"
		return f"{inc}: {len(rows)} events, deployment linked={bool(frappe.db.get_value('Wolf Incident', inc, 'deployment_event'))}"

	def skew():
		future = schema.normalize_metric({"metric_name": "smoke_skew", "value": 99, "host": HOST}, tz,
		                                 now + timedelta(hours=11))
		events.insert_metrics([future])
		doc = rule("smoke skew", signal_type="metric", metric_name="smoke_skew", aggregation="max",
		           operator=">", threshold=0, window_minutes=5, group_by="host", filter_host=HOST)
		res = rules.evaluate(doc)
		assert not (res["fired"] or res["updated"]), f"future-dated sample fired a rule: {res}"
		clamped, was = schema.clamp_future(future["timestamp"], now, 300)
		assert was and clamped == now
		frappe.db.set_value("Wolf Alert Rule", "smoke skew", "enabled", 0)
		return "future samples ignored by rules, clamped on ingest"

	def overview():
		data = ui.get_overview(range="1h", host=HOST)
		series = ui.get_metric_series(HOST, metrics=json.dumps(["disk_used_percent", "http_502"]), range="1h")["series"]
		return f"{len(data['open_incidents'])} open, {len(data['events'])} events, series={sorted(series)}"

	def presets_and_signal_rules():
		from frappe_wolf.wolf.presets import PRESETS, install_presets

		install_presets()
		missing = [p["rule_name"] for p in PRESETS if not frappe.db.exists("Wolf Alert Rule", p["rule_name"])]
		assert not missing, f"presets not installed: {missing}"
		samples = [
			schema.normalize_metric({"metric_name": "tls_days_left", "value": 2.5, "host": HOST, "bench": "b1",
			                         "site": "smoke.localhost", "labels": {"domain": "smoke.example.net"}}, tz, now),
			schema.normalize_metric({"metric_name": "backup_age_hours", "value": 30, "host": HOST, "bench": "b1",
			                         "site": "smoke.localhost"}, tz, now),
		]
		events.insert_metrics(samples)
		fired = {}
		for preset in ("TLS certificate expiring within 3 days", "No backup for 26 h"):
			spec = next(p for p in PRESETS if p["rule_name"] == preset)
			doc = rule(f"smoke {preset}", **{k: v for k, v in spec.items() if k != "rule_name"}, filter_host=HOST)
			res = rules.evaluate(doc)
			assert res["fired"] or res["updated"], f"{preset} did not fire: {res}"
			fired[preset] = res["fired"] or res["updated"]
			frappe.db.set_value("Wolf Alert Rule", doc.name, "enabled", 0)
		return f"{len(PRESETS)} presets installed; fired {fired}"

	def email_queue_signals():
		from frappe_wolf.wolf import email_queue

		names = []
		for status, age in (("Not Sent", 120), ("Error", 1), ("Sent", 120)):
			doc = frappe.get_doc({"doctype": "Email Queue", "sender": "smoke@example.com", "message": "smoke",
			                      "status": status, "name": frappe.generate_hash(length=12)})
			doc.db_insert()
			stamp = now - timedelta(minutes=age)
			frappe.db.sql("update `tabEmail Queue` set creation = %s, modified = %s where name = %s", (stamp, stamp, doc.name))
			names.append(doc.name)
		found = email_queue.check(now)
		assert found["backlog"] >= 1 and found["errors"] >= 1, found
		cats = set(frappe.get_all("Wolf Event", filters={"category": ("in", ["email_queue_backlog", "email_queue_error"])},
		                          pluck="category"))
		assert cats == {"email_queue_backlog", "email_queue_error"}, cats
		again = email_queue.check(now)  # bumps, does not duplicate
		count = frappe.db.count("Wolf Event", {"category": "email_queue_backlog"})
		frappe.db.delete("Email Queue", {"name": ("in", names)})
		return f"{found} then {again}; {count} backlog event(s)"

	def backup_failure_and_geoip():
		import os
		import tempfile

		frappe.log_error(title="Dropbox Backup Failed", message="Traceback: smoke backup failure")
		assert frappe.db.exists("Wolf Event", {"category": "backup_failed"}), "backup failure not captured"

		fd, path = tempfile.mkstemp(suffix=".csv")
		with os.fdopen(fd, "w") as f:
			f.write("start_ip,end_ip,country\n81.2.69.0,81.2.69.255,FR\n89.160.20.0,89.160.23.255,DE\n")
		settings = frappe.get_single("Wolf Settings")
		settings.geoip_database = path
		settings.capture_admin_countries = 1
		settings.capture_admin_logins = 1
		settings.save()
		user = "Administrator"
		for ip in ("81.2.69.10", "81.2.69.11", "89.160.20.7"):
			frappe.get_doc({"doctype": "Activity Log", "operation": "Login", "status": "Success", "user": user,
			                "subject": "smoke login", "ip_address": ip}).insert(ignore_permissions=True)
		events_found = frappe.get_all("Wolf Event", filters={"category": "admin_login_new_country"},
		                              fields=["title", "payload"])
		settings.geoip_database = None
		settings.save()
		os.unlink(path)
		assert len(events_found) == 1, events_found
		return events_found[0].title

	def purge():
		old = events.insert_event(schema.normalize_event(
			{"type": "error", "host": HOST, "severity": "warning", "title": "smoke old unlinked"},
			tz, now - timedelta(days=400)))
		tasks.purge_old_data()
		assert not frappe.db.exists("Wolf Event", old), "old unattached event survived purge"
		kept = frappe.db.count("Wolf Event", {"incident": ("is", "set"), "host": HOST})
		return f"old unattached event purged, {kept} incident-linked events kept"

	def resolve():
		frappe.db.delete("Wolf Metric Sample", {"host": HOST})
		res = rules.evaluate(frappe.get_doc("Wolf Alert Rule", "smoke disk"))
		assert res["resolved"], res
		return f"resolved {res['resolved']}"

	for name, fn in [("ingest", ingest), ("evaluate", evaluate), ("correlate", correlate), ("timeline", timeline), ("skew", skew),
	                 ("overview+charts", overview), ("presets+signal rules", presets_and_signal_rules),
	                 ("email queue", email_queue_signals), ("backup failure+geoip", backup_failure_and_geoip),
	                 ("purge", purge), ("auto-resolve", resolve)]:
		check(name, fn)

	for name in ("smoke disk", "smoke 502", "smoke last", "smoke cron"):
		frappe.db.set_value("Wolf Alert Rule", name, "enabled", 0)
	frappe.db.commit()
	print("all checks passed")


if __name__ == "__main__":
	main()
