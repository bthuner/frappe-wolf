#!/usr/bin/env python3
"""Replay the CLAUDE.md demo against the local stack: deployment -> 502 window -> 2AM cron failure.

Run from dev/demo after `docker compose up -d`:  python3 scenario.py
Stdlib only. Talks to the site through its REST API, like any admin would.
"""

import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

URL = "http://localhost:8099"
SITE = "wolf.localhost"
WEBHOOK = "http://catcher:9999/"
RULES = ["502 spike after deployment", "Nightly cron failure (01:00-04:00)", "Scheduler failure"]
# Candidate zones (one per hour of offset): the demo picks one where it is between 01:00 and 03:59.
ZONES = [
	"Pacific/Pago_Pago", "Pacific/Honolulu", "America/Anchorage", "America/Los_Angeles", "America/Denver",
	"America/Chicago", "America/New_York", "America/Halifax", "America/Sao_Paulo", "Atlantic/South_Georgia",
	"Atlantic/Azores", "Europe/London", "Europe/Paris", "Europe/Athens", "Europe/Moscow", "Asia/Dubai",
	"Asia/Karachi", "Asia/Dhaka", "Asia/Bangkok", "Asia/Shanghai", "Asia/Tokyo", "Australia/Brisbane",
	"Pacific/Noumea", "Pacific/Auckland", "Pacific/Tongatapu", "Pacific/Kiritimati",
]

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def step(msg):
	print(f"\n\033[1m==> {msg}\033[0m", flush=True)


def api(method, path, data=None):
	body = json.dumps(data).encode() if data is not None else None
	path = urllib.parse.quote(path, safe="/?&=%")
	req = urllib.request.Request(URL + path, data=body, method=method, headers={"Content-Type": "application/json"})
	with opener.open(req, timeout=30) as resp:
		return json.loads(resp.read() or b"{}")


def compose(*args):
	subprocess.run(["docker", "compose", *args], check=True)


def night_zone():
	now = datetime.now(timezone.utc)
	for zone in ZONES:
		if 1 <= now.astimezone(ZoneInfo(zone)).hour < 4:
			return zone
	sys.exit("no zone found")


def hammer(seconds, path="/api/method/frappe.auth.get_logged_user"):
	codes = {}
	end = time.time() + seconds
	while time.time() < end:
		try:
			with urllib.request.urlopen(URL + path, timeout=5) as r:
				codes[r.status] = codes.get(r.status, 0) + 1
		except urllib.error.HTTPError as e:
			codes[e.code] = codes.get(e.code, 0) + 1
		except OSError:
			codes["conn"] = codes.get("conn", 0) + 1
		time.sleep(0.3)
	return codes


def main():
	step("Log in as Administrator")
	api("POST", "/api/method/login", {"usr": "Administrator", "pwd": "admin"})

	current = api("GET", "/api/resource/System Settings/System Settings")["data"].get("time_zone")
	zone = current if current and 1 <= datetime.now(ZoneInfo(current)).hour < 4 else night_zone()
	step(f"Move the site clock to the night: System Settings time zone = {zone} "
	     f"(local time {datetime.now(ZoneInfo(zone)):%H:%M})")
	api("PUT", "/api/resource/System Settings/System Settings", {"time_zone": zone, "language": "en"})

	step("Route the demo rules to the webhook sink")
	for rule in RULES:
		path = "/api/resource/Wolf Alert Rule/" + rule
		api("PUT", path, {"channels": [{"channel_type": "Webhook", "webhook_url": WEBHOOK, "enabled": 1}]})
		print("   ", rule)

	step("Deployment: wolf-agent mark-deployment")
	compose("exec", "-T", "agent", "env/bin/wolf-agent", "mark-deployment",
	        "--bench", "frappe-bench", "--site", SITE, "--note", "bench update (demo)")

	step("Deployment goes wrong: backend down, traffic keeps coming (60s)")
	compose("stop", "backend")
	print("    responses:", hammer(60))

	step("Backend back + nginx reload")
	compose("start", "backend")
	compose("restart", "frontend")
	time.sleep(10)
	print("    responses:", hammer(10))

	step("2AM: a scheduled job fails (Server Script, every minute)")
	script = {
		"doctype": "Server Script",
		"name": "Nightly ledger sync",
		"script_type": "Scheduler Event",
		"event_frequency": "Cron",
		"cron_format": "* * * * *",
		"script": "frappe.throw('Nightly ledger sync failed: column tabGL Entry.wolf_demo missing (migration pending)')",
	}
	try:
		api("POST", "/api/resource/Server Script", script)
	except urllib.error.HTTPError as e:
		if e.code != 409:
			raise
		api("PUT", "/api/resource/Server Script/Nightly ledger sync", {"disabled": 0})

	step("Let the scheduler fail once and the rules evaluate (~150s)")
	time.sleep(150)
	api("PUT", "/api/resource/Server Script/Nightly ledger sync", {"disabled": 1})

	step("Incidents")
	incidents = api(
		"GET",
		"/api/resource/Wolf Incident?" + urllib.parse.urlencode({
			"fields": json.dumps(["name", "title", "status", "severity", "event_count", "deployment_event"]),
			"order_by": "creation asc", "limit_page_length": 50,
		}, quote_via=urllib.parse.quote),
	)["data"]
	for inc in incidents:
		link = " (deployment linked)" if inc.get("deployment_event") else ""
		print(f"    {inc['name']}  {inc['severity']:<8} {inc['status']:<12} {inc['event_count']:>3} events  {inc['title']}{link}")

	print(f"\nOpen {URL}/app/wolf-overview (Administrator / admin) and the incidents above.")
	print("Webhook payloads: docker compose logs catcher")


if __name__ == "__main__":
	main()
