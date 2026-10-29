"""Signals captured on the site where Frappe Wolf itself is installed (doc_events)."""

import os

import frappe
from frappe.utils import cint

from frappe_wolf.wolf import events, geoip
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


def after_migrate_marker():
	"""`bench migrate` usually means a deployment: record it for correlation."""
	try:
		events.emit("deployment", f"bench migrate on {frappe.local.site}", severity="info", category="migrate", source="frappe")
	except Exception:
		frappe.logger("frappe_wolf").exception("could not record migrate deployment")


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
