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
	                 ("overview+charts", overview), ("scoped user", scoped_user), ("agent secrets", agent_secrets),
	                 ("purge", purge), ("auto-resolve", resolve)]:
		check(name, fn)

	for name in ("smoke disk", "smoke 502", "smoke last", "smoke cron"):
		frappe.db.set_value("Wolf Alert Rule", name, "enabled", 0)
	frappe.db.commit()
	print("all checks passed")


if __name__ == "__main__":
	main()
