"""Security posture of a site: weighted checks and a score (no frappe imports).

Each check is `pass`, `fail` or `unknown`, with the evidence it read and the fix. The score is
the weight of the checks that passed over the weight of all checks: a check Wolf cannot read
(no agent on the host, a remote site whose settings Wolf cannot see, a stale sample) counts as
not passed, so a site never looks safer because Wolf knows less about it. `coverage` says how
much of the weight Wolf could read at all.

Facts come from two places (api/security.py gathers them):
- the site Wolf runs on: its System Settings, roles and site config;
- the agent's latest samples for any site: `site_config_risks`, `exposed_services` on the
  hosts serving it, `backup_age_hours` and `tls_days_left`.
"""

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
MIN_PASSWORD_SCORE = 3
MAX_LOGIN_ATTEMPTS = 10
MAX_BACKUP_HOURS = 26
MIN_TLS_DAYS = 14

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
	return {"checks": rows, **score(rows)}
