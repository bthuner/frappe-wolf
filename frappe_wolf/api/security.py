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
from frappe_wolf.wolf import access, dns_txt, events, posture

RECENT = timedelta(hours=3)  # agent samples older than this make a check unknown
SETTINGS_FIELDS = ("enable_two_factor_auth", "enable_password_policy", "minimum_password_score",
                   "allow_consecutive_login_attempts", "allow_guests_to_upload_files")  # fmt: skip


def _latest(metric, since, **scope):
	"""Newest sample per (host, site) of `metric` since `since`, within the caller's access."""
	allowed, params = access.sql_condition("Wolf Metric Sample")
	where = "".join(f" and `{k}` = %({k})s" for k in scope)
	return frappe.db.sql(
		f"""select s.host, s.site, s.value, s.label_key from `tabWolf Metric Sample` s
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


MAIL_CACHE_SECONDS = 3600
MAIL_ERROR_CACHE_SECONDS = 600


def mail_domains():
	"""The domains this site sends email from (enabled outgoing Email Accounts)."""
	addresses = frappe.get_all("Email Account", filters={"enable_outgoing": 1}, pluck="email_id")
	return sorted({a.rsplit("@", 1)[1].strip().lower() for a in addresses if a and "@" in a})


def _txt(name):
	"""TXT records of `name`, cached; None when DNS could not tell."""
	key = f"wolf_mail_auth|{name}"
	cached = frappe.cache.get_value(key, expires=True, use_local_cache=False)
	if cached is not None:
		return cached.get("records")
	try:
		records = dns_txt.lookup(name)
		ttl = MAIL_CACHE_SECONDS
	except dns_txt.DNSError as e:
		frappe.logger("frappe_wolf").info(f"TXT lookup of {name} failed: {e}")
		records, ttl = None, MAIL_ERROR_CACHE_SECONDS
	frappe.cache.set_value(key, {"records": records}, expires_in_sec=ttl)
	return records


def mail_auth(domains):
	"""({domain: spf verdict}, {domain: dmarc verdict}); a verdict is (ok, why) or None."""
	spf, dmarc = {}, {}
	for domain in domains:
		records = _txt(domain)
		spf[domain] = None if records is None else posture.spf_verdict(records)
		# DMARC: the domain's own record, else its parents' (subdomain policy), as receivers do
		labels = domain.split(".")
		dmarc[domain] = None
		for i in range(len(labels) - 1):
			records = _txt("_dmarc." + ".".join(labels[i:]))
			if records is None:
				break
			if any(r.strip().lower().startswith("v=dmarc1") for r in records) or len(labels) - i <= 2:
				dmarc[domain] = posture.dmarc_verdict(records, subdomain=i > 0)
				break
	return spf, dmarc


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
	hsts = {}
	for r in _latest("hsts_max_age_days", since, site=site):
		domain = (r.label_key or "").partition("domain=")[2] or r.site
		hsts[domain] = min(hsts.get(domain, r.value), r.value)
	facts["hsts_max_age_days"] = hsts
	if site == frappe.local.site:
		facts["spf"], facts["dmarc"] = mail_auth(mail_domains())
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
