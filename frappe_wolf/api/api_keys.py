"""API key used from an IP never seen for it (wolf/api_keys.py decides; this records).

hooks.py after_request: for every successful request authenticated by an API key, the key's
address goes into Wolf API Key IP. A Redis mark per key and address makes the common case one
cache read; the table is read and written at most once a day per key and address. Switched
with the other application signals (Wolf Settings → Capture application security changes).
"""

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from frappe_wolf.wolf import api_keys, events, local_signals, reputation, schema

DOCTYPE = "Wolf API Key IP"
CACHE_PREFIX = "wolf_apikey_ip|"
CACHE_SECONDS = 24 * 3600


def row_name(api_key, ip):
	return schema.fingerprint("api_key_ip", api_key, ip)[:32]


def after_request(response=None, request=None):
	try:
		status = getattr(response, "status_code", None)
		if not status or status >= 400 or request is None:
			return
		header = request.headers.get("Authorization")
		if not header or header.split(" ", 1)[0].lower() not in ("token", "basic"):
			return
		user = frappe.session.user
		ip = getattr(frappe.local, "request_ip", None)
		if not ip or not user or user == "Guest":
			return
		api_key = reputation.api_key_from_header(header)
		if not api_key or not cint(local_signals._settings().get("capture_app_security")):
			return
		if record(api_key, ip, user, path=getattr(request, "path", None), agent=request.headers.get("User-Agent")):
			frappe.db.commit()  # after_request runs after the request's own commit
	except Exception:
		frappe.logger("frappe_wolf").exception("could not record an API key address")


def record(api_key, ip, user, path=None, agent=None, now=None):
	"""Note that `api_key` was used from `ip`; True when the database was written."""
	mark = f"{CACHE_PREFIX}{api_key}|{ip}"
	if frappe.cache.get_value(mark, expires=True, use_local_cache=False):
		return False
	now = now or now_datetime()
	name = row_name(api_key, ip)
	if frappe.db.exists(DOCTYPE, name):
		frappe.db.set_value(DOCTYPE, name, "last_seen", now, update_modified=False)
		frappe.cache.set_value(mark, 1, expires_in_sec=CACHE_SECONDS)
		return True
	known = frappe.get_all(DOCTYPE, filters={"api_key": api_key}, fields=["ip", "first_seen"], order_by="first_seen asc", limit=1000)
	first_seen = get_datetime(known[0].first_seen) if known and known[0].first_seen else None
	doc = frappe.get_doc({"doctype": DOCTYPE, "name": name, "api_key": api_key, "user": user, "ip": ip,
	                      "first_seen": now, "last_seen": now})  # fmt: skip
	try:
		doc.db_insert()
	except frappe.DuplicateEntryError:
		return False  # a concurrent request recorded it first (and decided)
	frappe.cache.set_value(mark, 1, expires_in_sec=CACHE_SECONDS)
	known_ips = [k.ip for k in known]
	outcome = api_keys.verdict(ip, known_ips, first_seen, now)
	if outcome == "new":
		privileged = local_signals.is_privileged(user, local_signals._settings())
		events.emit(
			"security",
			f"API key of {'privileged user ' if privileged else ''}{user} used from new IP {ip}",
			severity="critical" if privileged else "warning",
			category="api_key_new_ip",
			source="frappe",
			payload={"user": user, "api_key": api_key, "ip": ip, "privileged": privileged, "path": (path or "")[:200],
			         "user_agent": (agent or "")[:300] or None, "known_ips": known_ips[-api_keys.MAX_KNOWN_IN_PAYLOAD:],
			         "known_count": len(known_ips), "key_first_seen": str(first_seen)},  # fmt: skip
			fingerprint=schema.fingerprint("api_key_new_ip", api_key, ip),
		)
	return True


def purge(days=365):
	"""Daily: forget addresses not used for `days` (they count as new again)."""
	frappe.db.delete(DOCTYPE, {"last_seen": ("<", add_to_date(now_datetime(), days=-days))})
