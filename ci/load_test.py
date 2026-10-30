"""Synthetic load for Wolf storage: N hosts x M metrics x D days, then time push and the overview.

Run from the bench's sites/ directory, ON A THROWAWAY SITE (it rewrites the rollup watermarks
and fills the metric tables):

    ../env/bin/python /path/to/ci/load_test.py <site> [--hosts 100] [--metrics 15] [--days 7]

Data layout, as a site running for D days would have it with the default retention:
  * raw samples every --interval seconds for the last --raw-hours (default 48),
  * 1-minute rollups before that (2 samples per minute), 1-hour rollups for the whole period
    before the raw window, written directly (synthetic);
  * the raw window is then rolled up by the real rollup job (timed).
Then it times, server side (no HTTP, no agent authentication):
  * push: ingest.accept() of one host's batch (M metrics, an event every 10th push), in async
    mode (validate + enqueue) and in sync mode (validate + insert + correlate);
  * the queued jobs (store_batch), as a worker would run them;
  * the overview: get_overview and get_metric_series for every range, and rule queries.
Prints p50 / p95 / max per operation. Not run in CI; ci/integration.sh runs it after
db_smoke.py when WOLF_LOAD_TEST=1 (flags in WOLF_LOAD_ARGS).
"""

import argparse
import json
import random
import statistics
import sys
import time
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

PREFIX = "load-"
CHART_METRICS = ["cpu_usage", "memory_used_percent", "disk_used_percent", "load_per_core"]


def parse_args():
	p = argparse.ArgumentParser()
	p.add_argument("site")
	p.add_argument("--hosts", type=int, default=100)
	p.add_argument("--metrics", type=int, default=15)
	p.add_argument("--days", type=float, default=7)
	p.add_argument("--interval", type=int, default=30, help="seconds between raw samples")
	p.add_argument("--raw-hours", type=int, default=48)
	p.add_argument("--pushes", type=int, default=200, help="timed pushes per ingest mode")
	p.add_argument("--repeat", type=int, default=10, help="timed runs per read operation")
	return p.parse_args()


def metric_names(m):
	return (CHART_METRICS + [f"load_metric_{i}" for i in range(m)])[:m]


def timed(fn):
	t0 = time.perf_counter()
	out = fn()
	return time.perf_counter() - t0, out


def summary(samples):
	ms = sorted(s * 1000 for s in samples)
	p95 = ms[min(len(ms) - 1, int(round(0.95 * (len(ms) - 1))))]
	return {"n": len(ms), "p50_ms": round(statistics.median(ms), 1), "p95_ms": round(p95, 1), "max_ms": round(ms[-1], 1)}


class Loader:
	def __init__(self, args):
		from frappe_wolf.wolf import rollups, storage

		self.args, self.rollups, self.storage = args, rollups, storage
		self.hosts = [f"{PREFIX}{i:03d}" for i in range(args.hosts)]
		self.metrics = metric_names(args.metrics)
		self.now = now_datetime()
		self.start = storage.floor(self.now - timedelta(days=args.days), storage.HOUR)
		self.raw_start = storage.floor(self.now - timedelta(hours=args.raw_hours), storage.HOUR)
		self.rng = random.Random(42)
		self.pool = [round(self.rng.uniform(5, 95), 2) for _ in range(4093)]
		self.pool_i, self.seq, self.counts, self.t0 = 0, 0, {}, time.perf_counter()

	def value(self, metric):
		return round(self.rng.uniform(5, 95), 2) if not metric.startswith("http_") else self.rng.randint(0, 50)

	def pooled(self):
		"""Cheap pseudo-random values (generating tens of millions of rows is the slow part)."""
		self.pool_i = (self.pool_i + 1) % len(self.pool)
		return self.pool[self.pool_i]

	def name(self):
		self.seq += 1
		return f"ld{self.seq:011d}"

	def bulk(self, doctype, fields, rows, chunk=2000):
		"""Multi-row INSERT (faster than frappe.db.bulk_insert for millions of rows), one commit per call."""
		cols = ", ".join(f"`{f}`" for f in fields)
		row_ph = "(" + ", ".join(["%s"] * len(fields)) + ")"
		for i in range(0, len(rows), chunk):
			part = rows[i : i + chunk]
			frappe.db.sql(
				f"insert into `tab{doctype}` ({cols}) values {', '.join([row_ph] * len(part))}",
				[v for row in part for v in row],
			)
		frappe.db.commit()
		self.counts[doctype] = self.counts.get(doctype, 0) + len(rows)
		elapsed = time.perf_counter() - self.t0
		total = sum(self.counts.values())
		if total // 1_000_000 != (total - len(rows)) // 1_000_000:
			print(f"    {total:,} rows in {elapsed:.0f}s ({total / max(elapsed, 1e-9):,.0f} rows/s)", flush=True)

	def clean(self):
		for table in ("Wolf Metric Sample", "Wolf Metric Rollup", "Wolf Event"):
			frappe.db.sql(f"delete from `tab{table}` where host like %(p)s", {"p": PREFIX + "%"})
		frappe.db.sql("delete from `tabWolf Rollup State`")
		frappe.db.commit()

	def raw(self):
		fields = ["name", "creation", "modified", "owner", "modified_by", "docstatus",
		          "metric_name", "value", "timestamp", "host", "bench", "site", "label_key", "labels"]  # fmt: skip
		batch, ts, step = [], self.raw_start, timedelta(seconds=self.args.interval)
		end = self.now - timedelta(seconds=self.args.interval)
		while ts < end:
			t = ts.strftime("%Y-%m-%d %H:%M:%S")
			for host in self.hosts:
				for metric in self.metrics:
					batch.append((self.name(), t, t, "Administrator", "Administrator", 0,
					              metric, self.pooled(), t, host, None, None, None, None))
			if len(batch) >= 50_000:
				self.bulk("Wolf Metric Sample", fields, batch)
				batch = []
			ts += step
		if batch:
			self.bulk("Wolf Metric Sample", fields, batch)

	def synthetic_rollups(self):
		storage = self.storage
		fields = self.rollups.ROLLUP_FIELDS
		for res, until in ((storage.MINUTE, self.raw_start), (storage.HOUR, self.raw_start)):
			batch, ts, step = [], self.start, timedelta(seconds=res)
			per_bucket = max(1, res // self.args.interval)
			while ts < until:
				t = ts.strftime("%Y-%m-%d %H:%M:%S")
				last = (ts + step - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
				for host in self.hosts:
					for metric in self.metrics:
						lo, hi = sorted((self.pooled(), self.pooled()))
						batch.append((self.name(), t, t, "Administrator", "Administrator", 0,
						              metric, res, t, host, None, None, None,
						              per_bucket, (lo + hi) / 2 * per_bucket, lo, hi, hi, last))
				if len(batch) >= 50_000:
					self.bulk("Wolf Metric Rollup", fields, batch)
					batch = []
				ts += step
			if batch:
				self.bulk("Wolf Metric Rollup", fields, batch)
		for res in (storage.MINUTE, storage.HOUR):
			name = self.rollups._ensure_state(res)
			frappe.db.set_value("Wolf Rollup State", name, "done_until", self.raw_start)
		frappe.db.commit()

	def events(self):
		fields = ["name", "creation", "modified", "owner", "modified_by", "docstatus", "title", "event_type",
		          "category", "severity", "source", "timestamp", "last_seen", "count", "fingerprint", "host"]  # fmt: skip
		batch, ts = [], self.start
		while ts < self.now:
			for host in self.hosts:
				kind = self.rng.choice(("error", "security", "cron_failure"))
				batch.append((self.name(), ts, ts, "Administrator", "Administrator", 0,
				              f"load {kind} on {host}", kind, "load", self.rng.choice(("warning", "critical")), "load",
				              ts, ts, 1, f"load-{self.seq}", host))
			ts += timedelta(hours=1)
		self.bulk("Wolf Event", fields, batch)


def push_payload(loader, host, i):
	from frappe_wolf.wolf import events
	from frappe_wolf.wolf.heartbeat import to_utc_iso

	stamp = to_utc_iso(now_datetime(), events.system_tz())
	data = {
		"hostname": host,
		"agent_version": "load",
		"sent_at": stamp,
		"metrics": [{"metric_name": m, "value": loader.value(m), "timestamp": stamp, "host": host} for m in loader.metrics],
		"events": [],
	}
	if i % 10 == 0:
		data["events"].append({"type": "error", "category": "http_5xx", "severity": "warning", "host": host,
		                       "title": f"load 500 on {host}", "timestamp": stamp})
	return data


def set_mode(mode):
	settings = frappe.get_single("Wolf Settings")
	settings.ingest_mode = mode
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()


def main():
	args = parse_args()
	frappe.init(args.site)
	frappe.connect()
	frappe.set_user("Administrator")
	frappe.local.request_ip = "127.0.0.1"
	from frappe.utils.background_jobs import get_queue

	from frappe_wolf.api import ingest, ui
	from frappe_wolf.wolf import rollups, rules

	loader = Loader(args)
	print(f"site {args.site} on {frappe.conf.db_type or 'mariadb'}: {args.hosts} hosts x {args.metrics} metrics x "
	      f"{args.days} days, raw every {args.interval}s for {args.raw_hours}h", flush=True)
	results = {}

	loader.clean()
	for step in ("synthetic_rollups", "raw", "events"):
		results[f"load {step}"] = {"seconds": round(timed(getattr(loader, step))[0], 1)}
		print(f"  {step}: {results[f'load {step}']['seconds']}s, rows so far {loader.counts}", flush=True)
	results["rows"] = dict(loader.counts)

	# the real rollup job over the raw window
	seconds, _ = timed(lambda: [rollups.roll(res) for res in (rollups.MINUTE, rollups.HOUR)])
	raw_rows = loader.counts.get("Wolf Metric Sample", 0)
	results["rollup raw window"] = {"seconds": round(seconds, 1), "raw_rows_per_s": round(raw_rows / max(seconds, 1e-9))}
	print(f"  rollup of the raw window: {seconds:.1f}s", flush=True)

	agent = frappe.db.get_value("Wolf Agent", {"hostname": f"{PREFIX}agent"})
	if not agent:
		doc = frappe.get_doc({"doctype": "Wolf Agent", "hostname": f"{PREFIX}agent", "status": "Active"})
		doc.insert(ignore_permissions=True)
		agent = doc.name
	frappe.db.commit()

	for mode in ("async", "sync"):
		set_mode(mode)
		times = []
		for i in range(args.pushes):
			host = loader.hosts[i % len(loader.hosts)]
			data = push_payload(loader, host, i)
			times.append(timed(lambda data=data: ingest.accept(agent, data))[0])
		results[f"push ({mode})"] = summary(times)
		print(f"  push {mode}: {results[f'push ({mode})']}", flush=True)
		if mode == "async":
			jobs = [j for j in get_queue(ingest.INGEST_QUEUE).get_jobs()
			        if (j.kwargs or {}).get("method") == ingest.STORE_METHOD]
			worker = []
			for job in jobs:
				worker.append(timed(lambda job=job: ingest.store_batch(**job.kwargs["kwargs"]))[0])
				job.delete()
			if worker:
				results["queued store_batch (worker)"] = summary(worker)
				print(f"  worker store_batch: {results['queued store_batch (worker)']}", flush=True)
	set_mode("async")

	for range_key in ui.RANGES:
		results[f"get_overview {range_key}"] = summary(
			[timed(lambda r=range_key: ui.get_overview(range=r))[0] for _ in range(args.repeat)])
		results[f"get_metric_series {range_key}"] = summary(
			[timed(lambda r=range_key: ui.get_metric_series(loader.hosts[0], range=r))[0] for _ in range(args.repeat)])
		page = [timed(lambda r=range_key: (ui.get_overview(range=r), ui.get_metric_series(loader.hosts[0], range=r)))[0]
		        for _ in range(args.repeat)]
		results[f"overview page {range_key} (overview + chart)"] = summary(page)
		print(f"  overview page {range_key}: {results[f'overview page {range_key} (overview + chart)']}", flush=True)

	for minutes, agg in ((5, "avg"), (1440, "max"), (10080, "last")):
		doc = frappe.get_doc({"doctype": "Wolf Alert Rule", "rule_name": f"load {minutes} {agg}", "signal_type": "metric",
		                      "metric_name": "cpu_usage", "aggregation": agg, "operator": ">", "threshold": 1000,
		                      "window_minutes": minutes, "group_by": "host", "enabled": 0})
		since = now_datetime() - timedelta(minutes=minutes)
		times, rows = [], []
		for _ in range(args.repeat):
			t, rows = timed(lambda doc=doc, since=since: rules.query_metric(doc, since))
			times.append(t)
		results[f"rule query {minutes} min {agg} by host"] = {**summary(times), "groups": len(rows)}
		print(f"  rule {minutes} min {agg}: {results[f'rule query {minutes} min {agg} by host']}", flush=True)

	print("\n| operation | n | p50 ms | p95 ms | max ms |\n|---|---|---|---|---|")
	for name, r in results.items():
		if "p95_ms" in r:
			print(f"| {name} | {r['n']} | {r['p50_ms']} | {r['p95_ms']} | {r['max_ms']} |")
	print("\nRESULTS " + json.dumps(results, default=str))
	frappe.db.commit()


if __name__ == "__main__":
	sys.exit(main())
