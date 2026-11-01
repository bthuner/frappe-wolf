"""Security posture of a site (wolf/posture.py): checks, evidence, fixes and a score.

`get_posture(site)` reads the facts live. The `record_security_scores` job (hourly) stores the
score as the `security_score` metric of each site, so it has a history, a chart and a rule
("Security score low"). Satellites forward events only, so on a satellite site the job does
nothing: its central site scores it from the agent's samples, with the settings checks
unknown.
"""

from datetime import timedelta

import frappe
from frappe.utils import cint, now_datetime

from frappe_wolf import satellite
from frappe_wolf.api.ui import READ_ROLES
from frappe_wolf.wolf import access, events, posture

RECENT = timedelta(hours=3)  # agent samples older than this make a check unknown
SETTINGS_FIELDS = ("enable_two_factor_auth", "enable_password_policy", "minimum_password_score",
                   "allow_consecutive_login_attempts", "allow_guests_to_upload_files")  # fmt: skip


def _latest(metric, since, **scope):
	"""Newest sample per (host, site) of `metric` since `since`, within the caller's access."""
	allowed, params = access.sql_condition("Wolf Metric Sample")
	where = "".join(f" and `{k}` = %({k})s" for k in scope)
	return frappe.db.sql(
		f"""select s.host, s.site, s.value from `tabWolf Metric Sample` s
		join (select host, coalesce(site, '') as site_key, max(`timestamp`) as ts from `tabWolf Metric Sample`
			where metric_name = %(metric)s and `timestamp` >= %(since)s {where} {allowed}
			group by host, coalesce(site, '')) latest
			on latest.host = s.host and latest.site_key = coalesce(s.site, '') and latest.ts = s.`timestamp`
		where s.metric_name = %(metric)s""",
		{"metric": metric, "since": since, **scope, **params},
		as_dict=True,
	)


def _local_facts():
	"""What only the site Wolf runs on can read about itself."""
	settings = frappe.get_cached_doc("System Settings")
	conf = frappe.get_site_config() or {}
	findings = [k for k in ("developer_mode", "allow_tests") if cint(conf.get(k))]
	if str(conf.get("host_name") or "").lower().startswith("http://"):
		findings.append("host_name")
	if conf.get("allow_cors") in ("*", ["*"]):
		findings.append("allow_cors")
	return {
		"settings": {f: settings.get(f) for f in SETTINGS_FIELDS},
		"system_manager_2fa": bool(cint(frappe.db.get_value("Role", "System Manager", "two_factor_auth"))),
		"site_config_findings": findings,
	}


def facts_for(site, now=None):
	since = (now or now_datetime()) - RECENT
	facts = {"settings": None}
	if site == frappe.local.site:
		facts.update(_local_facts())
	risks = _latest("site_config_risks", since, site=site)
	if risks:
		facts["site_config_risks"] = max(r.value for r in risks)
	hosts = {r.host for r in risks} or {r.host for r in _latest("backup_age_hours", since, site=site)}
	if site == frappe.local.site:
		hosts.add(events.local_scope().get("host"))
	exposed = {}
	for host in sorted(h for h in hosts if h):
		rows = _latest("exposed_services", since, host=host)
		exposed[host] = max((r.value for r in rows), default=None)
	facts["exposed_services"] = exposed
	backups = _latest("backup_age_hours", since, site=site)
	facts["backup_age_hours"] = min((r.value for r in backups), default=None)
	tls = _latest("tls_days_left", since, site=site)
	facts["tls_days_left"] = min((r.value for r in tls), default=None)
	return facts


def known_sites(now=None):
	since = (now or now_datetime()) - RECENT
	sites = {r.site for metric in ("site_config_risks", "backup_age_hours") for r in _latest(metric, since) if r.site}
	if not satellite.is_satellite():
		sites.add(frappe.local.site)
	return sorted(sites)


@frappe.whitelist()
def get_posture(site=None):
	frappe.only_for(READ_ROLES)
	site = site or frappe.local.site
	if not access.visible("Wolf Metric Sample", {"site": site}):
		frappe.throw(frappe._("You may not see this site."), frappe.PermissionError)
	facts = facts_for(site)
	result = posture.evaluate(facts)
	return {"site": site, "hosts": sorted(facts["exposed_services"]), "local": site == frappe.local.site, "recent_hours": RECENT.total_seconds() / 3600, **result}


@frappe.whitelist()
def list_sites():
	frappe.only_for(READ_ROLES)
	return [s for s in known_sites() if access.visible("Wolf Metric Sample", {"site": s})]


def record_security_scores():
	"""Scheduler (hourly): one `security_score` sample per known site."""
	if satellite.is_satellite():
		return
	now = now_datetime()
	rows = []
	host = events.local_scope().get("host")
	for site in known_sites(now):
		facts = facts_for(site, now)
		result = posture.evaluate(facts)
		# the site's first host, so the sample sits with the site's other series
		site_host = host if site == frappe.local.site else next(iter(sorted(facts["exposed_services"])), None)
		rows.append({
			"metric_name": "security_score", "value": result["score"], "timestamp": now,
			"host": site_host, "bench": None, "site": site,
			"label_key": None, "labels": None,
		})  # fmt: skip
	if rows:
		events.insert_metrics(rows)
