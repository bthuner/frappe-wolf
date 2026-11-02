"""GitHub Security Advisories of Frappe and ERPNext, kept on disk for the posture check.

`refresh_advisories` (daily, and from the hourly score job when the copy is missing or a day
old) reads the published advisories of each repository in wolf/advisories.REPOS. A repository
that fails keeps its last copy; a copy older than STALE_DAYS is not used, so the check turns
unknown instead of judging with an old list. Nothing about this installation is sent: the
requests are unauthenticated reads of public data. Wolf Settings → Check versions against
GitHub Security Advisories turns it off.
"""

import importlib
import json
import os
from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from frappe_wolf.wolf import advisories
from frappe_wolf.wolf.spa import MANAGER_ROLES

REFRESH_AFTER = timedelta(hours=23)
RETRY_AFTER = timedelta(hours=1)  # after a failed attempt, from the hourly job
STALE_DAYS = 7


def store_path():
	return frappe.get_site_path("private", "wolf", "security_advisories.json")


def enabled():
	return bool(cint(frappe.get_cached_doc("Wolf Settings").get("check_security_advisories")))


def read_store():
	try:
		with open(store_path()) as f:
			return json.load(f)
	except (OSError, ValueError):
		return {}


def _write_store(data):
	path = store_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	tmp = path + ".tmp"
	with open(tmp, "w") as f:
		json.dump(data, f)
	os.replace(tmp, path)


def refresh_advisories(force=False, fetch=None):
	"""Scheduler (daily): every repository's advisories; a failing one keeps its last copy."""
	if not enabled() and not force:
		return None
	fetch = fetch or advisories.fetch
	old = read_store().get("apps") or {}
	now = str(now_datetime())
	apps = {}
	for app, repo in advisories.REPOS.items():
		try:
			found = fetch(repo)
			apps[app] = {"repo": repo, "ok": True, "fetched_at": now, "advisories": found}
		except Exception as e:  # OSError, HTTPError, ValueError on a bad answer
			frappe.logger("frappe_wolf").warning(f"security advisories of {repo}: {e}")
			kept = old.get(app) or {}
			apps[app] = {"repo": repo, "ok": False, "error": str(e)[:300], "fetched_at": kept.get("fetched_at"),
			             "advisories": kept.get("advisories")}  # fmt: skip
	_write_store({"attempted_at": now, "apps": apps})
	return {app: {k: v for k, v in data.items() if k != "advisories"} for app, data in apps.items()}


def refresh_if_due(now=None):
	"""From the hourly score job: fetch when there is no copy or it is a day old (a failed
	attempt is retried after an hour), so a new install does not wait for the daily job."""
	if not enabled():
		return
	now = now or now_datetime()
	store = read_store()
	attempted = store.get("attempted_at")
	ok = all((store.get("apps") or {}).get(app, {}).get("ok") for app in advisories.REPOS)
	due = REFRESH_AFTER if ok else RETRY_AFTER
	if not attempted or now - get_datetime(attempted) >= due:
		refresh_advisories()


def sources(now=None):
	"""What posture.check_advisories reads: None when off, else {app: {advisories, why}}."""
	if not enabled():
		return None
	now = now or now_datetime()
	apps = read_store().get("apps") or {}
	out = {}
	for app in advisories.REPOS:
		data = apps.get(app) or {}
		fetched = data.get("fetched_at")
		if data.get("advisories") is None or not fetched:
			why = f"advisories not fetched yet ({data['error']})" if data.get("error") else "advisories not fetched yet"
			out[app] = {"advisories": None, "why": why}
		elif now - get_datetime(fetched) > timedelta(days=STALE_DAYS):
			out[app] = {"advisories": None, "why": f"advisories last fetched on {str(fetched)[:10]}" + (f" ({data['error']})" if data.get("error") else "")}
		else:
			out[app] = {"advisories": data["advisories"], "why": None}
	return out


def local_versions():
	"""{app: {(major, minor, patch)}} of the apps advisories cover, installed on this site."""
	out = {}
	for app in frappe.get_installed_apps():
		if app not in advisories.REPOS:
			continue
		try:
			version = advisories.parse_version(getattr(importlib.import_module(app), "__version__", None))
		except ImportError:
			continue
		if version:
			out.setdefault(app, set()).add(version)
	return out


def versions_from_rows(rows):
	"""{app: {version}} from `app_version` samples (label_key "app=<app>")."""
	out = {}
	for r in rows:
		app = (r.label_key or "").partition("app=")[2]
		if app in advisories.REPOS and r.value:
			out.setdefault(app, set()).add(advisories.decode(r.value))
	return out


@frappe.whitelist()
def get_status():
	frappe.only_for(MANAGER_ROLES)
	store = read_store()
	return {
		"enabled": enabled(), "attempted_at": store.get("attempted_at"),
		"apps": {app: {"repo": d.get("repo"), "ok": d.get("ok"), "error": d.get("error"), "fetched_at": d.get("fetched_at"),
		               "count": len(d.get("advisories") or [])} for app, d in (store.get("apps") or {}).items()},
	}  # fmt: skip


@frappe.whitelist(methods=["POST"])
def refresh_now():
	frappe.only_for(MANAGER_ROLES)
	return refresh_advisories(force=True)
