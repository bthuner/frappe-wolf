"""Security posture of a site: weighted checks and a score (no frappe imports).

Each check is `pass`, `fail` or `unknown`, with the evidence it read and the fix. The score is
the weight of the checks that passed over the weight of all checks: a check Wolf cannot read
(no agent on the host, a remote site whose settings Wolf cannot see, a stale sample) counts as
not passed, so a site never looks safer because Wolf knows less about it. `coverage` says how
much of the weight Wolf could read at all.

Facts come from two places (api/security.py gathers them):
- the site Wolf runs on: its System Settings, roles and site config;
- the agent's latest samples for any site: `site_config_risks`, `exposed_services` on the
  hosts serving it, `backup_age_hours`, `tls_days_left` and `hsts_max_age_days`.

The versions of Frappe and ERPNext come from the agent's `app_version` samples for the site's
benches (and, on the Wolf site, from its own packages); the advisories from GitHub
(wolf/advisories.py, api/advisories.py).

SPF and DMARC are read from DNS by the site itself, for the domains its outgoing Email
Accounts send from (api/security.py); other sites' mail domains are unknown to Wolf.
"""

from frappe_wolf.wolf import advisories

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
MIN_PASSWORD_SCORE = 3
MAX_LOGIN_ATTEMPTS = 10
MAX_BACKUP_HOURS = 26
MIN_TLS_DAYS = 14
MIN_HSTS_DAYS = 180  # browsers' preload list asks a year; six months is the usual floor
STRICT_DMARC = ("quarantine", "reject")

# id, label, weight, fix
CHECKS = [
	("two_factor", "Two-factor authentication for System Managers", 3,
	 "System Settings → Enable Two Factor Auth, and tick Two Factor Authentication on the System Manager role."),
	("password_policy", "Password policy", 2,
	 f"System Settings → Enable Password Policy, minimum score {MIN_PASSWORD_SCORE} or more."),
	("login_lockout", "Lockout after failed logins", 1,
	 f"System Settings → Allow Consecutive Login Attempts between 1 and {MAX_LOGIN_ATTEMPTS}."),
	("guest_uploads", "Guests cannot upload files", 1, "System Settings → untick Allow Guests to Upload Files."),
	("site_config", "No development settings in site_config.json", 3,
	 "Remove developer_mode and allow_tests, use https:// in host_name, list allowed origins in allow_cors."),
	("exposure", "Bench services not listening beyond loopback", 3,
	 "Bind MariaDB, Redis, socket.io and gunicorn to 127.0.0.1 (or a private address) and keep a firewall in front."),
	("backup", f"A backup newer than {MAX_BACKUP_HOURS} h", 2, "Check the scheduler and the backup job (bench backup)."),
	("tls", f"TLS certificates valid for {MIN_TLS_DAYS} days or more", 2, "Renew the certificate (bench setup lets-encrypt)."),
	("hsts", f"HSTS for {MIN_HSTS_DAYS} days or more", 1,
	 'Add `add_header Strict-Transport-Security "max-age=31536000" always;` to the site\'s HTTPS server block in nginx.'),
	("spf", "SPF for the mail domain, ending in -all or ~all", 1,
	 "Publish one TXT record `v=spf1 <your senders> -all` on the domain the site sends email from."),
	("dmarc", "DMARC policy quarantine or reject", 1,
	 "Publish `v=DMARC1; p=quarantine; rua=mailto:...` at _dmarc.<domain> (after a while at p=none to check reports)."),
	("advisories", "No published security advisory applies to Frappe or ERPNext", 3,
	 "Update the bench (bench update) to the version the evidence names, or a later one of the same line."),
]  # fmt: skip


def _row(check_id, status, evidence):
	_, label, weight, fix = next(c for c in CHECKS if c[0] == check_id)
	return {"id": check_id, "label": label, "weight": weight, "status": status, "evidence": evidence,
	        "fix": fix if status == FAIL else None}  # fmt: skip


def _num(value):
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def check_settings(settings, system_manager_2fa):
	"""The four System Settings checks. `settings` is None for a site Wolf cannot read."""
	if settings is None:
		why = "settings of this site are only readable on the site itself (install Wolf there as a satellite)"
		return [_row(c, UNKNOWN, why) for c in ("two_factor", "password_policy", "login_lockout", "guest_uploads")]
	rows = []
	on = bool(_num(settings.get("enable_two_factor_auth")))
	if on and system_manager_2fa:
		rows.append(_row("two_factor", PASS, "enabled, and required for the System Manager role"))
	else:
		rows.append(_row("two_factor", FAIL, "disabled in System Settings" if not on else "enabled, but not required for System Managers"))
	score = _num(settings.get("minimum_password_score")) or 0
	if bool(_num(settings.get("enable_password_policy"))) and score >= MIN_PASSWORD_SCORE:
		rows.append(_row("password_policy", PASS, f"enabled, minimum score {score:g}"))
	else:
		rows.append(_row("password_policy", FAIL, "disabled" if not _num(settings.get("enable_password_policy")) else f"minimum score {score:g}"))
	attempts = _num(settings.get("allow_consecutive_login_attempts"))
	if attempts is not None and 1 <= attempts <= MAX_LOGIN_ATTEMPTS:
		rows.append(_row("login_lockout", PASS, f"locked after {attempts:g} failed logins"))
	else:
		rows.append(_row("login_lockout", FAIL, "no lockout" if not attempts else f"{attempts:g} attempts allowed"))
	uploads = bool(_num(settings.get("allow_guests_to_upload_files")))
	rows.append(_row("guest_uploads", FAIL if uploads else PASS, "guests may upload files" if uploads else "guests may not upload"))
	return rows


def check_site_config(findings=None, risks=None):
	"""`findings` (settings names, read on the site itself) win over the agent's `risks` count."""
	if findings is not None:
		if findings:
			return _row("site_config", FAIL, ", ".join(findings))
		return _row("site_config", PASS, "none of developer_mode, allow_tests, plain-HTTP host_name, allow_cors *")
	if risks is None:
		return _row("site_config", UNKNOWN, "no recent site_config_risks sample from an agent")
	if risks:
		return _row("site_config", FAIL, f"{risks:g} risky setting(s) reported by the agent (see the insecure_site_config event)")
	return _row("site_config", PASS, "the agent found no risky setting")


def check_exposure(per_host):
	"""`per_host`: {host: exposed_services} for the hosts serving the site (None when unknown)."""
	known = {h: v for h, v in (per_host or {}).items() if v is not None}
	if not known:
		return _row("exposure", UNKNOWN, "no recent exposed_services sample from the agent on this site's hosts")
	exposed = {h: v for h, v in known.items() if v}
	if exposed:
		return _row("exposure", FAIL, "; ".join(f"{h}: {v:g} service(s)" for h, v in sorted(exposed.items()))
		            + " (see the service_exposed events; a firewall may still block them)")  # fmt: skip
	return _row("exposure", PASS, f"nothing beyond loopback on {', '.join(sorted(known))}")


def check_backup(age_hours):
	if age_hours is None:
		return _row("backup", UNKNOWN, "no recent backup_age_hours sample")
	if age_hours <= MAX_BACKUP_HOURS:
		return _row("backup", PASS, f"newest backup {age_hours:.1f} h old (recency only, not a restore test)")
	return _row("backup", FAIL, f"newest backup {age_hours:.1f} h old")


def check_tls(days_left):
	if days_left is None:
		return _row("tls", UNKNOWN, "no recent tls_days_left sample")
	if days_left >= MIN_TLS_DAYS:
		return _row("tls", PASS, f"the first certificate to expire has {days_left:.0f} days left")
	return _row("tls", FAIL, f"a certificate expires in {days_left:.0f} days")


def check_hsts(per_domain):
	"""`per_domain`: {domain: max-age in days} from the agent's latest samples (0: no header)."""
	if not per_domain:
		return _row("hsts", UNKNOWN, "no recent hsts_max_age_days sample (the agent reads it with the TLS check)")
	short = {d: v for d, v in per_domain.items() if v < MIN_HSTS_DAYS}
	if short:
		return _row("hsts", FAIL, "; ".join(f"{d}: {'no header' if not v else f'{v:g} days'}" for d, v in sorted(short.items())))
	return _row("hsts", PASS, f"{', '.join(sorted(per_domain))}: {min(per_domain.values()):g} days or more")


def spf_verdict(records):
	"""(ok, why) for a domain's TXT records."""
	spf = [r.strip() for r in records if r.strip().lower().startswith("v=spf1")]
	if not spf:
		return False, "no SPF record"
	if len(spf) > 1:
		return False, f"{len(spf)} SPF records: receivers treat that as an error"
	terms = spf[0].lower().split()[1:]
	last = next((t for t in reversed(terms) if t.lstrip("+-~?") == "all"), None)
	if last in ("-all", "~all"):
		return True, f"ends in {last}"
	if last is None:
		redirect = next((t for t in terms if t.startswith("redirect=")), None)
		if redirect:
			return True, f"delegated with {redirect} (not followed)"
		return False, "no all term: any other sender passes as neutral"
	return False, f"ends in {last}: any sender is accepted" if last in ("all", "+all") else f"ends in {last}: neutral"


def dmarc_verdict(records, subdomain=False):
	"""(ok, why) for the TXT records of _dmarc.<domain>; `subdomain` reads sp= when present."""
	dmarc = [r.strip() for r in records if r.strip().lower().startswith("v=dmarc1")]
	if not dmarc:
		return False, "no DMARC record"
	if len(dmarc) > 1:
		return False, f"{len(dmarc)} DMARC records: receivers ignore them all"
	tags = dict((k.strip().lower(), v.strip().lower()) for k, _, v in (t.partition("=") for t in dmarc[0].split(";")) if k.strip())
	policy = (tags.get("sp") if subdomain else None) or tags.get("p") or "none"
	pct = tags.get("pct", "100")
	if policy in STRICT_DMARC:
		return True, f"p={policy}" + (f", applied to {pct}% of messages" if pct != "100" else "")
	return False, f"p={policy}: reports only, spoofed mail is still delivered"


def _mail_check(check_id, per_domain):
	"""`per_domain`: {domain: (ok, why)} or {domain: None} when DNS could not tell."""
	if not per_domain:
		return _row(check_id, UNKNOWN, "no outgoing Email Account on this site, or another site's mail domain")
	if any(v is None for v in per_domain.values()):
		failed = ", ".join(sorted(d for d, v in per_domain.items() if v is None))
		return _row(check_id, UNKNOWN, f"DNS did not answer for {failed}")
	bad = {d: why for d, (ok, why) in per_domain.items() if not ok}
	if bad:
		return _row(check_id, FAIL, "; ".join(f"{d}: {why}" for d, why in sorted(bad.items())))
	return _row(check_id, PASS, "; ".join(f"{d}: {why}" for d, (_, why) in sorted(per_domain.items())))


def check_spf(per_domain):
	return _mail_check("spf", per_domain)


def check_dmarc(per_domain):
	return _mail_check("dmarc", per_domain)


def check_advisories(versions, sources):
	"""`versions`: {app: {(major, minor, patch)}} for the apps advisories cover.
	`sources`: None when the check is off, else {app: {"advisories": [...] or None, "why": str}}
	("why" says why there is no usable list: never fetched, failing, too old)."""
	if sources is None:
		return _row("advisories", UNKNOWN, "the advisory check is off in Wolf Settings")
	versions = {app: v for app, v in (versions or {}).items() if v}
	if not versions:
		return _row("advisories", UNKNOWN, "no app_version sample from an agent on this site's benches")
	affected, unknown, clear = [], [], []
	for app in sorted(versions):
		source = sources.get(app) or {}
		for version in sorted(versions[app]):
			if source.get("advisories") is None:
				unknown.append(f"{app} {advisories.fmt(version)}: {source.get('why') or 'advisories not fetched yet'}")
				continue
			result = advisories.assess(app, version, source["advisories"])
			if result["status"] == "affected":
				ids = ", ".join(a["id"] for a in result["found"][:3] if a.get("id"))
				affected.append(f"{advisories.summary(result)}" + (f" ({ids}{', ...' if len(result['found']) > 3 else ''})" if ids else ""))
			elif result["status"] == "unknown":
				unknown.append(result["why"])
			else:
				clear.append(f"{app} {result['version']}")
	if affected:
		return _row("advisories", FAIL, "; ".join(affected + unknown))
	if unknown:
		return _row("advisories", UNKNOWN, "; ".join(unknown + [f"{c}: no advisory applies" for c in clear]))
	return _row("advisories", PASS, f"{', '.join(clear)}: no published advisory applies")


def score(rows):
	"""`{score, coverage, passed, failed, unknown}`: score and coverage in 0..100."""
	total = sum(r["weight"] for r in rows) or 1
	passed = sum(r["weight"] for r in rows if r["status"] == PASS)
	known = sum(r["weight"] for r in rows if r["status"] != UNKNOWN)
	count = {s: sum(1 for r in rows if r["status"] == s) for s in (PASS, FAIL, UNKNOWN)}
	return {
		"score": round(100 * passed / total),
		"coverage": round(100 * known / total),
		"passed": count[PASS], "failed": count[FAIL], "unknown": count[UNKNOWN],
	}  # fmt: skip


def evaluate(facts):
	"""All checks and the score from a facts dict (see api/security.py for its keys)."""
	rows = check_settings(facts.get("settings"), facts.get("system_manager_2fa"))
	rows.append(check_site_config(facts.get("site_config_findings"), facts.get("site_config_risks")))
	rows.append(check_exposure(facts.get("exposed_services")))
	rows.append(check_backup(facts.get("backup_age_hours")))
	rows.append(check_tls(facts.get("tls_days_left")))
	rows.append(check_hsts(facts.get("hsts_max_age_days")))
	rows.append(check_spf(facts.get("spf")))
	rows.append(check_dmarc(facts.get("dmarc")))
	rows.append(check_advisories(facts.get("app_versions"), facts.get("advisories")))
	return {"checks": rows, **score(rows)}
