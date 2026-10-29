"""Exercise every Wolf SQL path against a site's database (MariaDB or Postgres).

Run from the bench's sites/ directory:  ../env/bin/python /path/to/ci/db_smoke.py <site>
Inserts demo rows, runs ingest, rules, correlation, timeline, charts, channels, purge and the
incident workflow (assignment, comments, merge/split, postmortem, metrics report, public status page),
and prints one line per check ("ok" or "FAIL"). Exits non-zero on the first failure.
CI runs it from ci/integration.sh.
"""

import json
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

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
			# Activity Log takes a login's IP from the request, not from the document
			frappe.local.request_ip = ip
			frappe.get_doc({"doctype": "Activity Log", "operation": "Login", "status": "Success", "user": user,
			                "subject": "smoke login", "ip_address": ip}).insert(ignore_permissions=True)
		frappe.local.request_ip = None
		new_ip = frappe.db.count("Wolf Event", {"category": "new_admin_ip"})
		assert new_ip >= 2, f"new_admin_ip events: {new_ip}"
		events_found = frappe.get_all("Wolf Event", filters={"category": "admin_login_new_country"},
		                              fields=["title", "payload"])
		settings.geoip_database = None
		settings.save()
		os.unlink(path)
		assert len(events_found) == 1, events_found
		return events_found[0].title

	def scoped_user():
		"""A Wolf User restricted to one site sees only that site, in lists, forms and Wolf APIs."""
		from frappe_wolf.wolf import access

		user = "smoke-scoped@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "Smoke", "send_welcome_email": 0,
			                "roles": [{"role": "Wolf User"}]}).insert(ignore_permissions=True)
		frappe.db.delete("Wolf User Scope", {"user": user})
		frappe.get_doc({"doctype": "Wolf User Scope", "user": user, "scope_type": "site",
		                "value": "smoke.localhost"}).insert()
		other = events.insert_event(schema.normalize_event(
			{"type": "error", "host": HOST, "bench": "b1", "site": "other.localhost", "severity": "warning",
			 "title": "smoke other site"}, tz, now))
		inc = frappe.db.get_value("Wolf Incident", {"alert_rule": "smoke 502"}, "name", order_by="creation desc")
		access.clear_cache()
		frappe.set_user(user)
		try:
			sites = set(frappe.get_list("Wolf Event", filters={"host": HOST}, pluck="site", limit_page_length=0))
			assert sites == {"smoke.localhost"}, f"list leaked {sites}"
			assert set(frappe.get_list("Wolf Incident", pluck="site", limit_page_length=0)) <= {"smoke.localhost"}
			assert not frappe.has_permission("Wolf Event", "read", doc=other, user=user), "form leaked"
			data = ui.get_overview(range="1h")
			assert {e.site for e in data["events"]} == {"smoke.localhost"}, "overview events leaked"
			assert all(i.site == "smoke.localhost" for i in data["open_incidents"] + data["incidents"])
			assert data["scopes"]["site"] == ["smoke.localhost"], data["scopes"]
			assert not data["agents"] or all(a.hostname == "smoke.localhost" for a in data["agents"])
			series = ui.get_metric_series(HOST, metrics=json.dumps(["disk_used_percent", "http_502"]), range="1h")
			names = {key.split(" [")[0] for key in series["series"]}
			assert names == {"http_502"}, f"metric series leaked {sorted(names)}"  # disk_used_percent has no site
			rows = ui.get_incident_timeline(inc)["events"]
			assert rows and all(r.site == "smoke.localhost" for r in rows), "timeline leaked"
		finally:
			frappe.set_user("Administrator")
		assert frappe.has_permission("Wolf Event", "read", doc=other), "manager restricted"
		admin_sites = set(frappe.get_list("Wolf Event", filters={"host": HOST}, pluck="site", limit_page_length=0))
		assert "other.localhost" in admin_sites
		frappe.db.delete("Wolf Event", {"name": other})
		return f"site-scoped user sees {sorted(sites)}, {len(rows)} timeline rows; Administrator sees {len(admin_sites)} sites"

	def agent_secrets():
		"""Register, authenticate, migrate a legacy Password secret and hit the push rate limit."""
		from frappe.utils.password import get_decrypted_password, set_encrypted_password
		from werkzeug.test import EnvironBuilder

		from frappe_wolf.api import agent as agent_api
		from frappe_wolf.api import ingest
		from frappe_wolf.patches import hash_agent_secrets

		def request(agent_id=None, secret=None, body=None):
			headers = {"X-Wolf-Agent-Id": agent_id, "X-Wolf-Agent-Secret": secret} if agent_id else {}
			frappe.local.request = EnvironBuilder(method="POST", json=body or {}, headers=headers).get_request()
			frappe.local.request_ip = "127.0.0.1"

		def legacy_rows():
			return frappe.db.sql("select count(*) from `__Auth` where doctype = 'Wolf Agent' and fieldname = 'agent_secret'")[0][0]

		saved_request = getattr(frappe.local, "request", None)
		settings = frappe.get_single("Wolf Settings")
		saved_limit = settings.max_pushes_per_minute
		try:
			token = get_decrypted_password("Wolf Settings", "Wolf Settings", "registration_token")
			request(body={"registration_token": token, "hostname": "smoke-agent", "agent_version": "smoke"})
			creds = agent_api.register()
			agent_id, secret = creds["agent_id"], creds["agent_secret"]
			stored = frappe.db.get_value("Wolf Agent", agent_id, "secret_hash")
			assert stored.startswith("hmac-sha256$") and secret not in stored, stored
			assert legacy_rows() == 0, "secret still stored reversibly"
			request(agent_id, secret)
			assert agent_api.authenticate_agent() == agent_id
			request(agent_id, secret[:-1] + ("0" if secret[-1] != "0" else "1"))
			try:
				agent_api.authenticate_agent()
				raise AssertionError("wrong secret accepted")
			except frappe.AuthenticationError:
				frappe.clear_messages()

			# an agent registered before hashing: Password value only, the patch hashes it
			frappe.db.set_value("Wolf Agent", agent_id, "secret_hash", None)
			set_encrypted_password("Wolf Agent", agent_id, "legacy-secret-0123456789", "agent_secret")
			assert legacy_rows() == 1
			hash_agent_secrets.execute()
			assert legacy_rows() == 0, "patch left the Password value"
			request(agent_id, "legacy-secret-0123456789")
			assert agent_api.authenticate_agent() == agent_id, "legacy secret stopped working"

			# lazy migration on first use (code updated, migrate not run yet)
			frappe.db.set_value("Wolf Agent", agent_id, "secret_hash", None)
			set_encrypted_password("Wolf Agent", agent_id, "legacy-secret-lazy", "agent_secret")
			request(agent_id, "legacy-secret-lazy")
			assert agent_api.authenticate_agent() == agent_id
			assert legacy_rows() == 0 and frappe.db.get_value("Wolf Agent", agent_id, "secret_hash")

			settings.max_pushes_per_minute = 1
			settings.save()
			limited = 0
			for _ in range(3):  # 3 pushes: two land in one minute window even across a boundary
				request(agent_id, "legacy-secret-lazy", body={"metrics": [], "events": [], "hostname": "smoke-agent"})
				try:
					ingest.push()
				except frappe.TooManyRequestsError:
					frappe.clear_messages()
					limited += 1
			assert limited, "push rate limit never triggered"
		except Exception:
			frappe.db.rollback()  # Postgres: the failed transaction must end before the cleanup below
			raise
		finally:
			frappe.local.request = saved_request
			settings.reload()
			settings.max_pushes_per_minute = saved_limit
			settings.save()
		frappe.delete_doc("Wolf Agent", agent_id, ignore_permissions=True, force=True)
		return f"hashed secret auth ok, legacy secret migrated, {limited} of 3 pushes rate limited"

	def purge():
		old = events.insert_event(schema.normalize_event(
			{"type": "error", "host": HOST, "severity": "warning", "title": "smoke old unlinked"},
			tz, now - timedelta(days=400)))
		old_delivery = frappe.get_doc({"doctype": "Wolf Delivery", "channel_type": "Email", "kind": "fired",
		                               "at": now - timedelta(days=400)}).insert(ignore_permissions=True).name
		tasks.purge_old_data()
		assert not frappe.db.exists("Wolf Event", old), "old unattached event survived purge"
		assert not frappe.db.exists("Wolf Delivery", old_delivery), "old delivery survived purge"
		kept = frappe.db.count("Wolf Event", {"incident": ("is", "set"), "host": HOST})
		return f"old unattached event purged, {kept} incident-linked events kept"

	def channels():
		"""Native channels against a local fake: secrets encrypted, deliveries recorded, never leaked."""
		from frappe_wolf.wolf import notify

		received = []

		class Fake(BaseHTTPRequestHandler):
			def do_POST(self):
				body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
				received.append((self.path, self.headers.get("Authorization"), body))
				failing = self.path.startswith("/fail")
				self.send_response(500 if failing else 200)
				self.end_headers()
				# A careless receiver echoing the request path (and its token) back.
				self.wfile.write(f"bad request {self.path}".encode() if failing else b"{}")

			def log_message(self, *args):
				pass

		server = HTTPServer(("127.0.0.1", 0), Fake)
		threading.Thread(target=server.serve_forever, daemon=True).start()
		base = f"http://127.0.0.1:{server.server_port}"
		key, token, path_secret = "smoke-routing-key-123", "tk_smoke_secret_456", "SLACKPATHSECRET789"
		try:
			doc = rule(
				"smoke channels", signal_type="event", event_type="security", filter_host="smoke-channels-none", operator=">=", threshold=1,
				window_minutes=5, group_by="none", notify_on_resolve=0,
				channels=[
					{"channel_type": "PagerDuty", "enabled": 1, "pagerduty_url": f"{base}/v2/enqueue",
					 "pagerduty_routing_key": key},
					{"channel_type": "ntfy", "enabled": 1, "ntfy_server": base, "ntfy_topic": "wolf-smoke",
					 "ntfy_token": token},
					{"channel_type": "Slack", "enabled": 1, "chat_webhook_url": f"{base}/fail/services/{path_secret}"},
				],
			)
			pd_row = doc.channels[0].name
			stored = frappe.db.get_value("Wolf Alert Channel", pd_row, "pagerduty_routing_key")
			assert stored != key, "routing key stored in clear"
			assert frappe.get_doc("Wolf Alert Rule", doc.name).channels[0].get_password("pagerduty_routing_key") == key

			res = frappe.get_doc("Wolf Alert Rule", doc.name).send_test_notification()
			outcome = {(r["channel_type"], r.get("action")): r for r in res["results"]}
			assert outcome[("PagerDuty", "trigger")]["ok"] and outcome[("PagerDuty", "resolve")]["ok"], res
			assert outcome[("ntfy", "publish")]["ok"], res
			slack = outcome[("Slack", "post")]
			assert not slack["ok"] and slack["http_status"] == 500, slack
			pd_bodies = [json.loads(b) for p, _a, b in received if p == "/v2/enqueue"]
			assert [b["event_action"] for b in pd_bodies] == ["trigger", "resolve"], pd_bodies
			assert ("/wolf-smoke", f"Bearer {token}") in [(p, a) for p, a, _b in received]

			rows = ui.get_incident_deliveries(res["incident"])
			assert len(rows) == 4, rows
			leaks = [s for s in (key, token, path_secret) for r in rows if s in json.dumps(r, default=str)]
			assert not leaks, f"secret in a delivery row: {leaks}"
			logged = frappe.db.sql("select error from `tabError Log` where error like %s or error like %s",
			                       (f"%{path_secret}%", f"%{token}%"))
			assert not logged, "secret in the Error Log"

			# A person acknowledges, then resolves: only the stateful channel hears it.
			inc = frappe.get_doc({"doctype": "Wolf Incident", "title": "smoke channels incident", "status": "Open",
			                      "severity": "critical", "alert_rule": doc.name, "started_at": now_datetime(),
			                      "notified_at": now_datetime()}).insert(ignore_permissions=True)
			assert notify.has_stateful(doc.name)
			inc.acknowledge()
			before = len(received)
			notify.send(inc.name, "acknowledged")
			notify.send(inc.name, "resolved", only_stateful=True)
			later = [(p, json.loads(b).get("event_action")) for p, _a, b in received[before:]]
			assert later == [("/v2/enqueue", "acknowledge"), ("/v2/enqueue", "resolve")], later
			assert frappe.db.count("Wolf Delivery", {"incident": inc.name, "ok": 1}) == 2
			frappe.db.set_value("Wolf Alert Rule", doc.name, "enabled", 0)
			return f"{len(received)} requests to the fake, {len(rows)} deliveries on the test incident, no secret stored"
		finally:
			server.shutdown()
			server.server_close()

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
	                 ("overview+charts", overview), ("presets+signal rules", presets_and_signal_rules),
	                 ("email queue", email_queue_signals), ("backup failure+geoip", backup_failure_and_geoip),
	                 ("scoped user", scoped_user), ("agent secrets", agent_secrets),
	                 ("channels", channels), ("purge", purge), ("auto-resolve", resolve),
	                 ("assign+comment+activity", workflow), ("merge+split", merge_split), ("postmortem", postmortem),
	                 ("metrics report", metrics), ("status page", status_page)]:
		check(name, fn)

	for name in ("smoke disk", "smoke 502", "smoke last", "smoke cron"):
		frappe.db.set_value("Wolf Alert Rule", name, "enabled", 0)
	frappe.db.commit()
	print("all checks passed")


if __name__ == "__main__":
	main()
