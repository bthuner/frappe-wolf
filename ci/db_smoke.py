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
ROLL_HOST = "smoke-roll"


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
	from frappe_wolf.wolf import conditions, events, incidents, rules, schema

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

	# --- storage: rollups, resolution selection, async ingest, retention -------------------
	from frappe_wolf.api import ingest as ingest_api
	from frappe_wolf.wolf import rollups, storage
	from frappe_wolf.wolf.series import bucket_series
	from frappe_wolf.wolf.storage import HOUR, MINUTE, RAW

	base = storage.floor(now, HOUR) - timedelta(hours=5)  # 3 closed hours of samples from here
	roll_end = base + timedelta(hours=3)
	roll_where = ["host = %(rhost)s", "metric_name = 'smoke_roll'"]
	roll_params = {"rhost": ROLL_HOST}

	def raw_truth(start, end):
		"""Per label: every aggregation computed from the raw samples."""
		rows = frappe.db.sql(
			"""select label_key, `value`, `timestamp` from `tabWolf Metric Sample`
			where host = %(rhost)s and metric_name = 'smoke_roll' and `timestamp` >= %(s)s and `timestamp` < %(e)s""",
			{**roll_params, "s": start, "e": end},
			as_dict=True,
		)
		out = {}
		for r in rows:
			out.setdefault(r.label_key, []).append((r.timestamp, float(r.value)))
		return {
			label: {"avg": sum(v for _, v in vals) / len(vals), "min": min(v for _, v in vals),
			        "max": max(v for _, v in vals), "sum": sum(v for _, v in vals), "count": len(vals),
			        "last": max(vals)[1]}
			for label, vals in out.items()
		}

	def compare(rows, truth, agg, what):
		got = {r.label_key: float(r.value) for r in rows}
		assert set(got) == set(truth), f"{what}: groups {sorted(got)} != {sorted(truth)}"
		for label, expected in truth.items():
			assert abs(got[label] - expected[agg]) <= 1e-6 * max(1.0, abs(expected[agg])), \
				f"{what} {agg} {label}: {got[label]} != {expected[agg]}"

	def compare_rollups(what):
		truth = raw_truth(base, roll_end)
		for res in (MINUTE, HOUR):
			for agg in conditions.AGGREGATIONS:
				rows = rollups.aggregate(roll_where, roll_params, ["label_key"], [], agg, res, base, roll_end)
				compare(rows, truth, agg, f"{what} {storage.LABELS[res]}")
			stored = frappe.db.sql(
				"""select sum(value_count) from `tabWolf Metric Rollup`
				where resolution = %(res)s and host = %(rhost)s and metric_name = 'smoke_roll'""",
				{**roll_params, "res": res},
			)[0][0]
			assert int(stored) == sum(t["count"] for t in truth.values()), f"{what}: {res}s rollups hold {stored} samples"
		return truth

	def rollup():
		samples = []
		for i in range(3 * 120):  # every 30 s for 3 hours
			for label, offset in (("/", 0), ("/data", 50)):
				samples.append(schema.normalize_metric(
					{"metric_name": "smoke_roll", "value": (i * 7) % 23 + offset + 0.25, "host": ROLL_HOST,
					 "labels": {"mount": label}}, tz, base + timedelta(seconds=30 * i + 7)))
		events.insert_metrics(samples)
		frappe.db.commit()
		rollups.run()
		marks = rollups.watermarks()
		assert marks[MINUTE] and marks[MINUTE] >= storage.floor(now, MINUTE) - timedelta(minutes=3), marks
		assert marks[HOUR] and marks[HOUR] >= roll_end, marks
		truth = compare_rollups("first run")

		rollups.run()  # nothing new: no-op
		# force a full recompute: must replace, not add
		frappe.db.sql("update `tabWolf Rollup State` set done_until = %(b)s", {"b": base})
		frappe.db.commit()
		rollups.run()
		compare_rollups("recomputed")

		# a late sample (spool replay) moves the watermarks back and is rolled in
		late = schema.normalize_metric({"metric_name": "smoke_roll", "value": 1000, "host": ROLL_HOST,
		                                "labels": {"mount": "/"}}, tz, base + timedelta(minutes=30, seconds=3))
		events.insert_metrics([late])
		frappe.db.commit()
		moved = rollups.watermarks()
		assert moved[MINUTE] == base + timedelta(minutes=30) and moved[HOUR] == base, moved
		rollups.run()
		truth = compare_rollups("late sample")
		assert truth["mount=/"]["max"] == 1000
		return f"{sum(t['count'] for t in truth.values())} samples, 1m and 1h equal raw for {conditions.AGGREGATIONS}"

	def resolution():
		assert rollups.resolution_for(5 * 60) == RAW
		assert rollups.resolution_for(24 * 3600) == MINUTE
		assert rollups.resolution_for(7 * 86400) == HOUR
		truth = raw_truth(now - timedelta(days=8), now + timedelta(minutes=1))
		out = []
		for minutes, agg in ((1440, "sum"), (1440, "last"), (10080, "min"), (10080, "count"), (10080, "avg")):
			doc = rule(f"smoke roll {minutes} {agg}", signal_type="metric", metric_name="smoke_roll", aggregation=agg,
			           operator=">", threshold=-1, window_minutes=minutes, group_by="host_label", filter_host=ROLL_HOST)
			frappe.db.set_value("Wolf Alert Rule", doc.name, "enabled", 0)
			compare(rules.query_metric(doc, now - timedelta(minutes=minutes)), truth, agg, f"rule {minutes} min")
			out.append(f"{minutes}min/{agg}")
		# charts: same points as bucketing the raw samples
		raw_rows = frappe.db.sql(
			"""select metric_name, label_key, `timestamp`, `value` from `tabWolf Metric Sample`
			where host = %(rhost)s and metric_name = 'smoke_roll'""", roll_params, as_dict=True)
		for range_key, label in (("24h", "1m"), ("7d", "1h")):
			chart = ui.get_metric_series(ROLL_HOST, metrics=json.dumps(["smoke_roll"]), range=range_key)
			assert chart["resolution"] == label, chart["resolution"]
			expected = bucket_series(raw_rows, chart["bucket_seconds"])
			for key, points in expected.items():
				got = dict(map(tuple, chart["series"][key]))
				for ts, value in points:
					assert abs(got[ts] - value) <= 0.011, f"chart {range_key} {key} {ts}: {got.get(ts)} != {value}"
			out.append(f"chart {range_key}={label}/{chart['bucket_seconds']}s")
		return ", ".join(out)

	def ingest_modes():
		agent = frappe.db.get_value("Wolf Agent", {"hostname": "smoke-ingest"})
		if not agent:
			doc = frappe.get_doc({"doctype": "Wolf Agent", "hostname": "smoke-ingest", "status": "Active", "kind": "agent"})
			doc.insert(ignore_permissions=True)
			agent = doc.name
		frappe.local.request_ip = "127.0.0.1"
		stamp = (now - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")

		def payload(tag):
			return {"hostname": "smoke-ingest", "agent_version": "smoke",
			        "metrics": [{"metric_name": "smoke_ingest", "value": 1, "labels": {"mode": tag}}],
			        "events": [{"type": "error", "category": "http_502", "severity": "critical", "host": HOST,
			                    "bench": "b1", "site": "smoke.localhost", "title": f"smoke {tag} 502",
			                    "fingerprint": f"smoke-{tag}-{stamp}"}]}

		def set_mode(mode):
			settings = frappe.get_single("Wolf Settings")
			settings.ingest_mode = mode
			settings.flags.ignore_permissions = True
			settings.save()
			frappe.db.commit()

		def stored(tag):
			return (frappe.db.count("Wolf Metric Sample", {"metric_name": "smoke_ingest", "label_key": f"mode={tag}"}),
			        frappe.db.get_value("Wolf Event", {"fingerprint": f"smoke-{tag}-{stamp}"}, "incident"))

		set_mode("sync")
		res = ingest_api.accept(agent, payload("sync"))
		assert res["queued"] is False and res["metrics"] == 1 and res["events"] == 1, res
		count, incident = stored("sync")
		assert count == 1 and incident, f"sync push not stored/correlated: {count}, {incident}"

		set_mode("async")
		before = frappe.db.get_value("Wolf Agent", agent, "last_seen")
		res = ingest_api.accept(agent, payload("async"))
		assert res["queued"] is True and res["metrics"] == 1 and res["events"] == 1 and "health" in res, res
		assert stored("async") == (0, None), "async push stored inline"
		assert frappe.db.get_value("Wolf Agent", agent, "last_seen") >= before, "heartbeat not updated"
		from frappe.utils.background_jobs import get_queue
		jobs = [j for j in get_queue(ingest_api.INGEST_QUEUE).get_jobs()
		        if (j.kwargs or {}).get("method") == ingest_api.STORE_METHOD
		        and (j.kwargs.get("kwargs") or {}).get("agent") == agent]
		assert jobs, "no ingest job queued"
		for job in jobs:  # no worker in CI: run the queued payload (it went through Redis)
			kwargs = job.kwargs["kwargs"]
			assert kwargs["correlate"] is True
			ingest_api.store_batch(**kwargs)
			job.delete()
		count, incident = stored("async")
		assert count == 1 and incident, f"queued push not stored/correlated: {count}, {incident}"
		return f"sync stored inline, async queued {len(jobs)} job(s) then stored, both attached to {incident}"

	def purge_metrics():
		raw_hours, minute_days, hour_days = rollups.retention()
		old_raw = schema.normalize_metric({"metric_name": "smoke_purge", "value": 1, "host": ROLL_HOST}, tz,
		                                  now - timedelta(hours=raw_hours + 24))
		events.insert_metrics([old_raw])
		key = ("smoke_purge", ROLL_HOST, None, None, None)
		part = storage.new_partial(1.0, now)
		old_minute = storage.floor(now - timedelta(days=minute_days + 5), MINUTE)
		old_hour = storage.floor(now - timedelta(days=hour_days + 5), HOUR)
		rollups.replace_buckets(MINUTE, old_minute, old_minute + timedelta(minutes=1), {(*key, old_minute): part})
		rollups.replace_buckets(HOUR, old_hour, old_hour + timedelta(hours=1), {(*key, old_hour): part})
		# a raw sample older than the raw retention but not rolled up yet must survive
		marks = rollups.watermarks()
		held = now - timedelta(hours=raw_hours + 2)
		frappe.db.set_value("Wolf Rollup State", "1m", "done_until", held - timedelta(hours=1))
		unrolled = schema.normalize_metric({"metric_name": "smoke_purge_keep", "value": 1, "host": ROLL_HOST}, tz, held)
		events.insert_metrics([unrolled])
		frappe.db.commit()
		cutoffs = rollups.purge()
		left = {m: frappe.db.count("Wolf Metric Sample", {"metric_name": m}) for m in ("smoke_purge", "smoke_purge_keep")}
		rolled = frappe.db.count("Wolf Metric Rollup", {"metric_name": "smoke_purge"})
		frappe.db.set_value("Wolf Rollup State", "1m", "done_until", marks[MINUTE])
		frappe.db.commit()
		assert left == {"smoke_purge": 0, "smoke_purge_keep": 1}, left
		assert rolled == 0, f"{rolled} expired rollups left"
		assert frappe.db.count("Wolf Metric Rollup", {"host": ROLL_HOST, "metric_name": "smoke_roll"}), "recent rollups purged"
		return f"expired raw/1m/1h purged, unrolled raw kept (cutoffs {', '.join(f'{k}<{v}' for k, v in cutoffs.items())})"

	for name, fn in [("ingest", ingest), ("evaluate", evaluate), ("correlate", correlate), ("timeline", timeline), ("skew", skew),
	                 ("overview+charts", overview), ("purge", purge), ("auto-resolve", resolve),
	                 ("rollup", rollup), ("resolution", resolution), ("ingest modes", ingest_modes),
	                 ("purge metrics", purge_metrics)]:
		check(name, fn)

	for name in ("smoke disk", "smoke 502", "smoke last", "smoke cron"):
		frappe.db.set_value("Wolf Alert Rule", name, "enabled", 0)
	frappe.db.commit()
	print("all checks passed")


if __name__ == "__main__":
	main()
