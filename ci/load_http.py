"""Agent pushes over HTTP, as agents send them: timed POSTs of
frappe_wolf.api.ingest.push with X-Wolf-Agent-Id / X-Wolf-Agent-Secret, from several agents at once.

Agents are created in-process (register() over HTTP is limited to 20 an hour per address); only
the timed pushes go over HTTP. Run on the bench, from sites/, after ci/load_test.py filled the
same throwaway site:

    ../env/bin/python /path/to/ci/load_http.py <site> --url http://127.0.0.1:8000 \
        [--agents 100] [--metrics 15] [--pushes 600] [--concurrency 8]

`--url` is the bench's web server (gunicorn or nginx); the Host header names the site. Prints
p50 / p95 / max of the round trip of accepted pushes (HTTP 200), as the agent sees it, and every
status or connection error. The agents report as hosts load-http-NNN, which the next
load_test.py run deletes: time the reads before this script, not after it.
"""

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import frappe
from load_test import metric_names, summary  # same metrics and percentiles as the server-side timings

PREFIX = "load-http-"


def args_():
	p = argparse.ArgumentParser()
	p.add_argument("site")
	p.add_argument("--url", default="http://127.0.0.1:8000")
	p.add_argument("--agents", type=int, default=100)
	p.add_argument("--metrics", type=int, default=15)
	p.add_argument("--pushes", type=int, default=600)
	p.add_argument("--concurrency", type=int, default=8)
	a = p.parse_args()
	for name in ("agents", "metrics", "pushes", "concurrency"):
		if getattr(a, name) < 1:
			p.error(f"--{name} must be at least 1")
	return a


def post(url, site, method, body, headers=None):
	req = urllib.request.Request(
		f"{url}/api/method/{method}",
		data=json.dumps(body).encode(),
		method="POST",
		headers={"Content-Type": "application/json", "Host": site, **(headers or {})},
	)
	t0 = time.perf_counter()
	try:
		with urllib.request.urlopen(req, timeout=30) as r:
			payload = json.loads(r.read() or b"{}")
			return time.perf_counter() - t0, r.status, payload
	except urllib.error.HTTPError as e:
		return time.perf_counter() - t0, e.code, {}
	except (urllib.error.URLError, OSError) as e:  # timeout, reset, refused: a result, not a crash
		return time.perf_counter() - t0, type(getattr(e, "reason", e)).__name__, {}


def main():
	a = args_()
	frappe.init(a.site)
	frappe.connect()
	# agents are created here, as register() would: its HTTP endpoint allows 20 registrations an
	# hour per address, and this measures pushes, not registration
	from frappe.utils import now_datetime

	from frappe_wolf.wolf import agent_secrets

	agents = []
	for i in range(a.agents):
		host = f"{PREFIX}{i:03d}"
		secret = agent_secrets.new_secret()
		name = frappe.db.get_value("Wolf Agent", {"hostname": host})
		doc = frappe.get_doc("Wolf Agent", name) if name else frappe.new_doc("Wolf Agent")
		doc.update({"hostname": host, "kind": "agent", "status": "Active", "agent_version": "load",
		            "secret_hash": agent_secrets.hash_secret(secret), "secret_issued_at": now_datetime()})  # fmt: skip
		doc.flags.ignore_permissions = True
		doc.save()
		agents.append((host, doc.name, secret))
	# enough pushes per agent minute for the run: this measures latency, not the rate limit. The
	# previous limit is put back afterwards.
	previous = frappe.db.get_single_value("Wolf Settings", "max_pushes_per_minute")
	frappe.db.set_single_value("Wolf Settings", "max_pushes_per_minute", 10_000)
	frappe.db.commit()
	frappe.destroy()
	print(f"{len(agents)} agents ready on {a.site}; pushing through {a.url}", flush=True)
	try:
		out = run(a, agents)
	finally:
		frappe.init(a.site)
		frappe.connect()
		frappe.db.set_single_value("Wolf Settings", "max_pushes_per_minute", previous)
		frappe.db.commit()
		frappe.destroy()
	print("HTTP RESULTS " + json.dumps(out), flush=True)


def run(a, agents):
	metrics = metric_names(a.metrics)
	lock, ok, statuses = threading.Lock(), [], {}

	def push(i):
		host, agent_id, secret = agents[i % len(agents)]
		stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
		body = {"sent_at": stamp, "agent_version": "load", "hostname": host, "events": [],
		        "metrics": [{"metric_name": m, "value": (i * 7 + j) % 97, "timestamp": stamp, "host": host}
		                    for j, m in enumerate(metrics)]}  # fmt: skip
		if i % 10 == 0:
			body["events"].append({"type": "error", "category": "http_5xx", "severity": "warning", "host": host,
			                       "title": f"load 500 on {host}", "timestamp": stamp})  # fmt: skip
		t, status, _ = post(a.url, a.site, "frappe_wolf.api.ingest.push", body,
		                    {"X-Wolf-Agent-Id": agent_id, "X-Wolf-Agent-Secret": secret})  # fmt: skip
		with lock:
			statuses[str(status)] = statuses.get(str(status), 0) + 1
			if status == 200:
				ok.append(t)

	t0 = time.perf_counter()
	with ThreadPoolExecutor(a.concurrency) as pool:
		list(pool.map(push, range(a.pushes)))
	wall = time.perf_counter() - t0
	# latency of accepted pushes only: a fast 401 or 429 is not a fast push
	out = {"pushes": a.pushes, "agents": len(agents), "concurrency": a.concurrency, "statuses": statuses,
	       "accepted_pushes_per_s": round(len(ok) / wall, 1)}  # fmt: skip
	if ok:
		out.update({k: v for k, v in summary(ok).items() if k != "n"})
	return out


if __name__ == "__main__":
	main()
