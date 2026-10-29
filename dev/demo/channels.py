#!/usr/bin/env python3
"""Exercise the Email and Raven alert channels end to end on the demo stack.

Needs the stack built with WOLF_DEMO_RAVEN=1 and `bench --site wolf.localhost install-app raven`
(see README). Email goes to Mailpit (http://localhost:8025); nothing leaves the machine.
"""

import json
import time
import urllib.error
import urllib.request

from scenario import URL, WEBHOOK, api, step

RULE = "Scheduler failure"
MAILPIT = "http://localhost:8025"
RECIPIENT = "oncall@wolf.localhost"
SCRIPT = "Wolf channel test job"


def upsert(doctype, name, doc):
	try:
		api("GET", f"/api/resource/{doctype}/{name}")
		return api("PUT", f"/api/resource/{doctype}/{name}", doc)["data"]
	except urllib.error.HTTPError as e:
		if e.code != 404:
			raise
		return api("POST", f"/api/resource/{doctype}", doc)["data"]


def mailpit_messages():
	with urllib.request.urlopen(f"{MAILPIT}/api/v1/messages", timeout=10) as r:
		return json.loads(r.read()).get("messages", [])


def main():
	api("POST", "/api/method/login", {"usr": "Administrator", "pwd": "admin"})

	step("Alert links point at the demo URL")
	api("PUT", "/api/resource/Wolf Settings/Wolf Settings", {"public_url": URL})

	step("Outgoing email account -> Mailpit")
	upsert("Email Account", "Wolf Demo SMTP", {
		"email_account_name": "Wolf Demo SMTP",
		"email_id": "wolf@wolf.localhost",
		"service": "",
		"smtp_server": "mailpit",
		"smtp_port": 1025,
		"use_tls": 0,
		"use_ssl_for_outgoing": 0,
		"no_smtp_authentication": 1,
		"enable_outgoing": 1,
		"default_outgoing": 1,
		"enable_incoming": 0,
	})

	step("Raven workspace + channel")
	upsert("Raven Workspace", "Ops", {"workspace_name": "Ops", "type": "Public"})
	channel = upsert("Raven Channel", "ops-wolf-alerts", {"channel_name": "wolf-alerts", "type": "Public", "workspace": "Ops"})
	print("    channel id:", channel["name"])

	step(f"Rule '{RULE}': Email + Raven + Webhook")
	api("PUT", f"/api/resource/Wolf Alert Rule/{RULE}", {"channels": [
		{"channel_type": "Email", "recipients": RECIPIENT, "enabled": 1},
		{"channel_type": "Raven", "raven_channel": channel["name"], "enabled": 1},
		{"channel_type": "Webhook", "webhook_url": WEBHOOK, "enabled": 1},
	]})

	before = len(mailpit_messages())
	step("A scheduled job fails")
	try:
		api("POST", "/api/resource/Server Script", {
			"doctype": "Server Script", "name": SCRIPT, "script_type": "Scheduler Event",
			"event_frequency": "Cron", "cron_format": "* * * * *",
			"script": "frappe.throw('Wolf channel test: payment reminders failed')",
		})
	except urllib.error.HTTPError as e:
		if e.code != 409:
			raise
		api("PUT", f"/api/resource/Server Script/{SCRIPT}", {"disabled": 0})

	step("Wait for incident + notifications (up to 4 min)")
	deadline = time.time() + 240
	mails, raven = [], []
	while time.time() < deadline:
		time.sleep(15)
		inbox = mailpit_messages()
		mails = [m for m in inbox[: max(len(inbox) - before, 0)] if "[Wolf]" in m["Subject"]]
		raven = api("GET", "/api/resource/Raven Message?fields=" + json.dumps(["name", "text", "creation"])
		            + "&filters=" + json.dumps([["channel_id", "=", channel["name"]], ["text", "like", "%[Wolf]%"]]))["data"]
		if mails and raven:
			break
	api("PUT", f"/api/resource/Server Script/{SCRIPT}", {"disabled": 1})

	step("Results")
	for m in mails:
		print(f"    email  -> {[t['Address'] for t in m['To']]}  {m['Subject']}")
	for r in raven:
		print(f"    raven  -> {r['name']}  {r['text'][:110]}")
	if not mails:
		print("    no email arrived (check Email Queue in Desk)")
	if not raven:
		print("    no Raven message (check Error Log for '[Wolf] Raven notification failed')")
	print(f"\nMailpit inbox: {MAILPIT}   Desk: {URL}/app/wolf-incident")


if __name__ == "__main__":
	main()
