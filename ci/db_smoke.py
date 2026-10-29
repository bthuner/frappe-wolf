"""Exercise every Wolf SQL path against a site's database (MariaDB or Postgres).

Run from the bench's sites/ directory:  ../env/bin/python /path/to/ci/db_smoke.py <site>
Inserts demo rows, runs ingest, rules, correlation, timeline, charts, purge and the incident
workflow (assignment, comments, merge/split, postmortem, metrics report, public status page),
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
WF_SITE = "wf.localhost"


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

	from frappe_wolf.api import incident as inc_api

	wf = {}

	def wf_event(title, minutes_ago, severity="warning"):
		return events.insert_event(schema.normalize_event(
			{"type": "error", "host": HOST, "bench": "b1", "site": WF_SITE, "severity": severity, "title": title},
			tz, now - timedelta(minutes=minutes_ago)))

	def workflow():
		e = [wf_event("wf a", 30), wf_event("wf b", 20, "critical"), wf_event("wf c", 10), wf_event("wf d", 5)]
		a, b = inc_api.open_from_event(e[0]), inc_api.open_from_event(e[1])
		for name in e[2:]:
			frappe.db.set_value("Wolf Event", name, "incident", b)
		incidents.refresh_event_count(b)
		doc = frappe.get_doc("Wolf Incident", b)
		doc.assign_to_me()
		doc.reload()
		assert doc.assigned_to == "Administrator", doc.assigned_to
		assert frappe.db.exists("ToDo", {"reference_type": "Wolf Incident", "reference_name": b, "status": "Open"}), "no ToDo"
		doc.acknowledge()
		inc_api.add_comment(b, "smoke <b>comment</b>")
		inc_api.add_comment(a, "comment that moves with the merge")
		activity = ui.get_incident_timeline(b)["activity"]
		comments = [x for x in activity if x["kind"] == "comment"]
		assert comments and "<b>" not in comments[0]["content"], comments
		assert any(x["kind"] == "status" and x["to"] == "Acknowledged" for x in activity), activity
		wf.update(events=e, a=a, b=b)
		return f"{a}, {b}: assigned, acknowledged, {len(activity)} activity entries"

	def merge_split():
		a, b, e = wf["a"], wf["b"], wf["events"]
		res = inc_api.merge(a, b)
		src = frappe.db.get_value("Wolf Incident", a, ["status", "merged_into"], as_dict=True)
		assert (src.status, src.merged_into) == ("Resolved", b), src
		assert frappe.db.get_value("Wolf Event", e[0], "incident") == b
		assert frappe.db.get_value("Wolf Incident", b, "event_count") == 4
		assert frappe.db.count("Comment", {"reference_doctype": "Wolf Incident", "reference_name": b,
		                                   "comment_type": "Comment"}) == 2, "comment not moved"
		try:
			inc_api.merge(a, b)
			raise AssertionError("merged twice")
		except frappe.ValidationError:
			frappe.clear_messages()
		c = inc_api.split(b, json.dumps([e[0], e[1]]), "smoke split")
		new = frappe.db.get_value("Wolf Incident", c, ["split_from", "alert_rule", "severity", "event_count"], as_dict=True)
		assert (new.split_from, new.alert_rule, new.severity, new.event_count) == (b, None, "critical", 2), new
		assert frappe.db.get_value("Wolf Incident", b, "event_count") == 2
		try:
			inc_api.split(b, json.dumps([e[2], e[3]]))
			raise AssertionError("split emptied the incident")
		except frappe.ValidationError:
			frappe.clear_messages()
		# a rule incident merged by hand: further breaches feed the target, no new incident
		cron = frappe.db.get_value("Wolf Incident", {"alert_rule": "smoke cron", "status": ("in", ("Open", "Acknowledged"))}, "name")
		assert cron, "smoke cron incident not open"
		inc_api.merge(cron, b)
		again = rules.evaluate(frappe.get_doc("Wolf Alert Rule", "smoke cron"))
		assert again["updated"] == [b] and not again["fired"], again
		wf["c"] = c
		return f"merged {res['events']} event(s) + {res['comments']} comment(s) into {b}, split {c}, rule follows merge"

	def postmortem():
		c = wf["c"]
		frappe.get_doc("Wolf Incident", c).resolve()
		pm = inc_api.create_postmortem(c)
		assert inc_api.create_postmortem(c) == pm, "second postmortem created"
		doc = frappe.get_doc("Wolf Postmortem", pm)
		assert "wf a" in doc.timeline_snapshot and doc.duration_minutes is not None, doc.timeline_snapshot
		assert frappe.db.get_value("Wolf Incident", c, "postmortem") == pm
		return f"{pm} for {c}, {doc.duration_minutes} min"

	def metrics():
		from frappe_wolf.frappe_wolf.report.wolf_incident_metrics import wolf_incident_metrics as report
		from frappe_wolf.wolf import stats

		today = now.date()
		out = {}
		for group_by in stats.GROUP_FIELDS:
			cols, data, _msg, _chart = report.execute(
				{"from_date": str(today - timedelta(days=1)), "to_date": str(today), "group_by": group_by})
			assert cols and data, group_by
			out[group_by] = len(data)
		site = report.execute({"from_date": str(today - timedelta(days=1)), "to_date": str(today), "group_by": "site",
		                       "site": WF_SITE})[1]
		row = next(r for r in site if r["group"] == WF_SITE)
		# the two merged incidents are left out: b and the split c remain
		assert row["incidents"] == 2 and row["acknowledged"] == 1 and row["resolved"] == 1, row
		return f"groups {out}, {WF_SITE}: mttr={row['mttr']} mtta={row['mtta']}"

	def status_page():
		from frappe.utils import set_request
		from frappe.website.serve import get_response

		from frappe_wolf.wolf.status_page import get_public_status

		slug = "smoke-status"
		page = frappe.get_doc("Wolf Status Page", slug) if frappe.db.exists("Wolf Status Page", slug) else frappe.new_doc("Wolf Status Page")
		page.update({"title": "Smoke status", "slug": slug, "site": WF_SITE, "enabled": 1, "min_severity": "info",
		             "show_history_days": 7})
		page.save()
		frappe.db.set_value("Wolf Incident", wf["b"], "public_title", "Checkout <slow>")
		view = get_public_status(slug)
		assert [i["title"] for i in view["current"]] == ["Checkout <slow>"], view["current"]
		assert view["history"], "split incident missing from history"
		assert HOST not in repr(view) and "b1" not in repr(view), view
		frappe.db.commit()
		frappe.set_user("Guest")
		try:
			set_request(method="GET", path=f"/wolf-status/{slug}")
			resp = get_response()
			html = resp.get_data(as_text=True)
			assert resp.status_code == 200, resp.status_code
			assert "Checkout &lt;slow&gt;" in html and "Checkout <slow>" not in html, "title not escaped"
			assert "<h1>Smoke status</h1>" in html, "page title missing"
			assert HOST not in html, "host leaked into the page"
			set_request(method="GET", path="/wolf-status/no-such-page")
			missing = get_response().status_code
			assert missing == 404, missing
		finally:
			frappe.set_user("Administrator")
		frappe.db.set_value("Wolf Status Page", slug, "enabled", 0)
		assert get_public_status(slug) is None, "disabled page still served"
		return f"/wolf-status/{slug}: 200 for guests, escaped, 404 when unknown or disabled"

	for name, fn in [("ingest", ingest), ("evaluate", evaluate), ("correlate", correlate), ("timeline", timeline), ("skew", skew),
	                 ("overview+charts", overview), ("purge", purge), ("auto-resolve", resolve),
	                 ("assign+comment+activity", workflow), ("merge+split", merge_split), ("postmortem", postmortem),
	                 ("metrics report", metrics), ("status page", status_page)]:
		check(name, fn)

	for name in ("smoke disk", "smoke 502", "smoke last", "smoke cron"):
		frappe.db.set_value("Wolf Alert Rule", name, "enabled", 0)
	frappe.db.commit()
	print("all checks passed")


if __name__ == "__main__":
	main()
