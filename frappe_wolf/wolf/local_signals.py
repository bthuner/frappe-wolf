"""Signals captured on the site where Frappe Wolf itself is installed (doc_events)."""

import os

import frappe
from frappe.utils import cint

from frappe_wolf.wolf import app_security, events, geoip
from frappe_wolf.wolf.reputation import lines as setting_lines
from frappe_wolf.wolf.schema import fingerprint


def _settings():
	return frappe.get_cached_doc("Wolf Settings")


def _safe(fn):
	"""Never let observability break the observed operation."""

	def wrapper(doc, method=None):
		if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
			return
		try:
			fn(doc)
		except Exception:
			# Do not call frappe.log_error here: it would feed back into on_error_log.
			frappe.logger("frappe_wolf").exception("local signal failed")

	wrapper.__name__ = fn.__name__
	return wrapper


def on_scheduled_job_failed(job):
	"""Called from WolfScheduledJobType.log_status while the exception is being handled."""
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return
	try:
		if not cint(_settings().capture_scheduler_failures):
			return
		label = job.server_script or job.method
		events.emit(
			"cron_failure",
			f"Scheduled job failed: {label}",
			severity="critical",
			category="scheduled_job",
			source="scheduler",
			payload={
				"job": job.name,
				"method": job.method,
				"server_script": job.server_script,
				"frequency": job.frequency,
				"cron_format": job.cron_format,
				"log": job.scheduler_log.name if getattr(job, "scheduler_log", None) else None,
				"traceback": frappe.get_traceback()[-4000:],
			},
			dedupe_minutes=1,
		)
		# log_status runs after the job's rollback; persist our event explicitly.
		frappe.db.commit()
	except Exception:
		frappe.logger("frappe_wolf").exception("could not record scheduled job failure")


@_safe
def on_error_log(doc):
	if not cint(_settings().capture_error_logs):
		return
	title = doc.get("method") or doc.get("title") or "Error"
	if str(title).startswith("[Wolf]"):
		return
	trace = doc.get("error") or ""
	last_line = trace.strip().splitlines()[-1] if trace.strip() else title
	backup = is_backup_failure(title, trace)
	events.emit(
		"error",
		f"{title}: {last_line}"[:255],
		severity="critical" if backup else "warning",
		category="backup_failed" if backup else "traceback",
		source="frappe",
		payload={"error_log": doc.name, "reference": [doc.get("reference_doctype"), doc.get("reference_name")], "traceback": trace[-4000:]},
		fingerprint=fingerprint("error_log", title, last_line),
		dedupe_minutes=10,
	)


@_safe
def on_activity_log(doc):
	if doc.operation != "Login":
		return
	settings = _settings()
	if (doc.user or "").lower() in decoy_users(settings):
		from frappe_wolf.api import threat

		threat.touched("user", doc.user, {"status": doc.status, "activity_log": doc.name})
		return  # a decoy account: the honeytoken event says it all
	if doc.status == "Failed" and cint(settings.capture_login_failures):
		events.emit(
			"security",
			f"Failed login for {doc.user or 'unknown user'} from {doc.ip_address or 'unknown IP'}",
			severity="warning",
			category="frappe_failed_login",
			source="frappe",
			payload={"user": doc.user, "ip": doc.ip_address, "activity_log": doc.name},
		)
	elif doc.status == "Success" and cint(settings.capture_admin_logins) and doc.ip_address:
		if not is_privileged(doc.user, settings):
			return
		previous = previous_login_ips(doc)
		if not previous or doc.ip_address in previous:
			return
		events.emit(
			"security",
			f"Privileged user {doc.user} logged in from new IP {doc.ip_address}",
			severity="warning",
			category="new_admin_ip",
			source="frappe",
			payload={"user": doc.user, "ip": doc.ip_address, "activity_log": doc.name},
		)
		check_new_country(doc, previous, settings)


def check_new_country(doc, previous, settings):
	"""`admin_login_new_country` when the new IP's country differs from every earlier login's.

	Uses the offline database in Wolf Settings → GeoIP Database; never a network service.
	"""
	path = (settings.get("geoip_database") or "").strip()
	if not path or not cint(settings.get("capture_admin_countries")):
		return
	if not os.path.isabs(path):
		path = frappe.get_site_path(path)  # e.g. private/files/GeoLite2-Country.mmdb
	try:
		country = geoip.country(path, doc.ip_address)
		if not country:
			return
		known = {geoip.country(path, ip) for ip in previous} - {None}
	except (OSError, ValueError) as e:
		frappe.logger("frappe_wolf").warning(f"GeoIP lookup with {path} failed: {e}")
		return
	if not known or country in known:
		return  # no baseline yet (only private IPs so far), or a known country
	events.emit(
		"security",
		f"Privileged user {doc.user} logged in from a new country: {country} ({doc.ip_address})",
		severity="critical",
		category="admin_login_new_country",
		source="frappe",
		payload={
			"user": doc.user, "ip": doc.ip_address, "country": country,
			"known_countries": sorted(known), "activity_log": doc.name,
		},
	)  # fmt: skip


@_safe
def on_user_update(doc):
	settings = _settings()
	check_api_key(doc, settings)
	if not cint(settings.capture_privileged_users) or not doc.enabled:
		return
	privileged = privileged_roles(settings)
	before = doc.get_doc_before_save()
	old = {r.role for r in before.roles} if before else set()
	added = sorted(({r.role for r in doc.roles} - old) & privileged)
	if not added:
		return
	events.emit(
		"security",
		f"{doc.name} was granted {', '.join(added)}",
		severity="critical" if "Administrator" in added or "System Manager" in added else "warning",
		category="privileged_user",
		source="frappe",
		payload={"user": doc.name, "roles": added, "by": frappe.session.user, "new_user": before is None},
	)


# --- Frappe application security (Wolf Settings → Flag risky application changes) ---


def _app_security(settings=None):
	return cint((settings or _settings()).get("capture_app_security"))


def _before(doc):
	before = doc.get_doc_before_save()
	return before.as_dict() if before else None


def on_user_before_save(doc, method=None):
	"""Frappe masks Password fields (`*****`) after before_save, so a new API secret is only
	visible here: note it for on_update, which records it once the save went through."""
	try:
		doc.flags.wolf_new_api_secret = app_security.new_api_secret(doc.get("api_secret"))
	except Exception:
		frappe.logger("frappe_wolf").exception("could not inspect the API secret")


def check_api_key(doc, settings):
	"""`api_key_generated`: a User save wrote a new API secret (generate keys, or by hand)."""
	if not _app_security(settings) or not doc.flags.get("wolf_new_api_secret"):
		return
	privileged = is_privileged(doc.name, settings)
	events.emit(
		"security",
		f"API key generated for {'privileged user ' if privileged else ''}{doc.name}",
		severity="critical" if privileged else "warning",
		category="api_key_generated",
		source="frappe",
		payload={"user": doc.name, "api_key": doc.get("api_key"), "privileged": privileged, "by": frappe.session.user},
	)


def _script_event(doc, kind, fields, category, label):
	if not _app_security():
		return
	change = app_security.script_change(_before(doc), doc.as_dict(), fields)
	if not change:
		return
	critical = kind == "server" and (doc.get("script_type") in ("API", "Scheduler Event") or cint(doc.get("allow_guest")))
	target = doc.get("api_method") or doc.get("reference_doctype") or doc.get("dt") or doc.get("event_frequency") or ""
	events.emit(
		"security",
		f"{label} {doc.name} {change}" + (f" ({target})" if target else ""),
		severity="critical" if critical else "warning",
		category=category,
		source="frappe",
		payload={
			"script": doc.name, "change": change, "script_type": doc.get("script_type"),
			"reference_doctype": doc.get("reference_doctype") or doc.get("dt"), "api_method": doc.get("api_method"),
			"allow_guest": cint(doc.get("allow_guest")), "event_frequency": doc.get("event_frequency"),
			"by": frappe.session.user, "excerpt": app_security.excerpt(doc.get("script")),
		},
	)  # fmt: skip


@_safe
def on_server_script(doc):
	_script_event(doc, "server", app_security.SERVER_SCRIPT_FIELDS, "server_script_change", "Server Script")


@_safe
def on_client_script(doc):
	_script_event(doc, "client", app_security.CLIENT_SCRIPT_FIELDS, "client_script_change", "Client Script")


@_safe
def on_console_log(doc):
	"""System Console runs arbitrary Python (or SQL) as the user: record each run."""
	if not _app_security():
		return
	events.emit(
		"security",
		f"System Console run by {doc.owner}" + (" (committed)" if cint(doc.get("committed")) else ""),
		severity="warning",
		category="console_execution",
		source="frappe",
		payload={
			"console_log": doc.name, "by": doc.owner, "type": doc.get("type"), "committed": cint(doc.get("committed")),
			"excerpt": app_security.excerpt(doc.get("script")),
		},
	)  # fmt: skip


# Role Permission Manager (Desk): `add` inserts a Custom DocPerm, but `update`, `remove` and
# `reset` write it with raw SQL, so no doc event fires. They are read from the request instead.
PERMISSION_MANAGER = "frappe.core.page.permission_manager.permission_manager."
PERMISSION_ACTIONS = {"add": "added", "update": "changed", "remove": "removed", "reset": "reset to standard"}


def after_request(response=None, request=None):
	"""hooks.py after_request: runs after the request's commit, so it commits its own event."""
	try:
		path = getattr(request, "path", "") or ""
		if not path.startswith("/api/method/" + PERMISSION_MANAGER):
			return
		action = PERMISSION_ACTIONS.get(path.rsplit(".", 1)[-1])
		if not action or getattr(response, "status_code", None) != 200 or not _app_security():
			return
		args = frappe.form_dict
		doctype = args.get("doctype") or args.get("parent")
		role = args.get("role")
		detail = {k: args.get(k) for k in ("permlevel", "ptype", "value", "if_owner") if args.get(k) is not None}
		subject = f"{role} on {doctype}" if role else doctype
		events.emit(
			"security",
			f"Permission rule {action}: {subject}"[:255],
			severity="warning",
			category="permission_change",
			source="frappe",
			payload={"doctype": doctype, "role": role, "action": action, **detail, "by": frappe.session.user},
			fingerprint=fingerprint("permission_change", doctype, role),
			dedupe_minutes=10,
		)
		frappe.db.commit()
	except Exception:
		frappe.logger("frappe_wolf").exception("could not record a permission change")


@_safe
def on_role_profile(doc):
	"""A Role Profile change re-grants its roles to every user who has the profile."""
	if not _app_security():
		return
	before = _before(doc)
	roles = sorted({r.role for r in doc.get("roles") or []})
	old = sorted({r.get("role") for r in (before or {}).get("roles") or []})
	added = sorted(set(roles) - set(old))
	if before and not added:
		return  # roles only removed, or nothing that grants more
	events.emit(
		"security",
		f"Role Profile {doc.name} {'created' if not before else 'now grants ' + ', '.join(added)}"[:255],
		severity="warning",
		category="permission_change",
		source="frappe",
		payload={"role_profile": doc.name, "roles": roles, "added": added, "by": frappe.session.user},
	)


@_safe
def on_system_settings(doc):
	"""`security_settings_weakened`: a System Settings save lowered a defence."""
	if not _app_security():
		return
	rows = app_security.weakened(_before(doc), doc.as_dict())
	if not rows:
		return
	events.emit(
		"security",
		f"Security settings weakened: {', '.join(r['label'] for r in rows)}"[:255],
		severity="critical",
		category="security_settings_weakened",
		source="frappe",
		payload={"changes": rows, "by": frappe.session.user},
	)


@_safe
def on_webhook(doc):
	"""`webhook_external`: a Webhook created, enabled or pointed at another host sends documents out."""
	if not _app_security() or not cint(doc.get("enabled")):
		return
	before = _before(doc)
	domain = app_security.url_domain(doc.get("request_url"))
	if before and cint(before.get("enabled")) and app_security.url_domain(before.get("request_url")) == domain:
		return
	events.emit(
		"security",
		f"Webhook {doc.name} sends {doc.get('webhook_doctype')} to {domain or 'an unknown host'}"[:255],
		severity="warning",
		category="webhook_external",
		source="frappe",
		payload={
			"webhook": doc.name, "doctype": doc.get("webhook_doctype"), "event": doc.get("webhook_docevent"),
			"domain": domain, "previous_domain": app_security.url_domain((before or {}).get("request_url")),
			"by": frappe.session.user,
		},
	)  # fmt: skip


def after_migrate_marker():
	"""`bench migrate` usually means a deployment: record it for correlation."""
	try:
		events.emit("deployment", f"bench migrate on {frappe.local.site}", severity="info", category="migrate", source="frappe")
	except Exception:
		frappe.logger("frappe_wolf").exception("could not record migrate deployment")


def decoy_users(settings):
	return {u.lower() for u in setting_lines(settings.get("honeytoken_users"))}


def privileged_roles(settings):
	return {r.strip() for r in (settings.privileged_roles or "").splitlines() if r.strip()}


def is_privileged(user, settings):
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	return bool(roles & privileged_roles(settings))


def previous_login_ips(doc):
	"""IPs of this user's earlier successful logins (latest 500)."""
	rows = frappe.get_all(
		"Activity Log",
		filters={"user": doc.user, "operation": "Login", "status": "Success", "name": ("!=", doc.name)},
		fields=["ip_address"],
		limit=500,
		order_by="creation desc",
	)
	return {r.ip_address for r in rows if r.ip_address}


BACKUP_TRACE_MARKERS = ("frappe/utils/backups.py", "frappe.utils.backups", "offsite_backup_utils")


def is_backup_failure(title, trace=""):
	"""Error Logs of failed backups. Frappe's S3/Dropbox/Google Drive integrations name the
	backup in the title or method ("Dropbox Backup Failed", "...take_backups_..."); a
	traceback through frappe/utils/backups.py is a failed `bench backup` / scheduled backup."""
	if "backup" in str(title or "").lower():
		return True
	tail = (trace or "")[-4000:]
	return any(m in tail for m in BACKUP_TRACE_MARKERS)
