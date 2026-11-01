"""Deception and threat intelligence (wolf/reputation.py): honeytokens and IP reputation lists.

Honeytokens (Wolf Settings → Threat intelligence and deception), each touch a critical
`honeytoken_triggered` event:
- decoy users: any login attempt, failed or not (Activity Log, local_signals.py);
- decoy API keys: any request whose Authorization header carries one, before Frappe rejects
  it (`before_request`, so the event is committed at once);
- decoy paths: any request for one, e.g. `/app/backup-admin` linked from nowhere.

IP reputation: when enabled, `refresh_reputation` (daily) downloads the listed blocklists
(`name url` per line, or a file path) and keeps them in `private/wolf/ip_reputation.json`. At
ingest, a security event whose payload `ip` is listed gets `reputation` and one severity step
more. Lookups never leave the site; a source that fails keeps its previous copy.
"""

import json
import os

import frappe
from frappe.utils import cint, now_datetime

from frappe_wolf.wolf import events, reputation, schema

MAX_DOWNLOAD = 20 * 1024 * 1024
_cache = {"key": None, "index": None}


def store_path():
	return frappe.get_site_path("private", "wolf", "ip_reputation.json")


def _settings():
	return frappe.get_cached_doc("Wolf Settings")


# --- IP reputation ---------------------------------------------------------------------------


def _fetch(source):
	if source.startswith(("http://", "https://")):
		import requests

		with requests.get(source, timeout=30, stream=True, headers={"User-Agent": "frappe-wolf"}) as r:
			r.raise_for_status()
			chunks, size = [], 0
			for chunk in r.iter_content(65536):
				size += len(chunk)
				if size > MAX_DOWNLOAD:
					raise ValueError(f"larger than {MAX_DOWNLOAD // (1024 * 1024)} MB")
				chunks.append(chunk)
		return b"".join(chunks).decode("utf-8", "replace")
	path = source if os.path.isabs(source) else frappe.get_site_path(source)
	with open(path, encoding="utf-8", errors="replace") as f:
		return f.read(MAX_DOWNLOAD)


def _read_store():
	try:
		with open(store_path()) as f:
			return json.load(f)
	except (OSError, ValueError):
		return {}


def refresh_reputation(force=False):
	"""Scheduler (daily): download every source; a failed one keeps its last copy."""
	settings = _settings()
	if not cint(settings.get("ip_reputation_enabled")) and not force:
		return None
	old = _read_store()
	old_index = reputation.Index.from_json(old.get("index"))
	index = reputation.Index()
	status = {}
	for name, source in reputation.parse_sources(settings.get("ip_reputation_sources")):
		try:
			nets = reputation.parse_list(_fetch(source))
			index.add(name, nets)
			status[name] = {"entries": len(nets), "ok": True}
		except Exception as e:
			kept = {k: v for k, v in old_index.ranges.items() if k[0] == name}
			index.ranges.update(kept)
			status[name] = {"ok": False, "error": str(e)[:300], "kept_previous": bool(kept)}
	data = {"refreshed_at": str(now_datetime()), "lists": status, "index": index.to_json()}
	path = store_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	tmp = path + ".tmp"
	with open(tmp, "w") as f:
		json.dump(data, f)
	os.replace(tmp, path)
	_cache["key"] = None
	return {"refreshed_at": data["refreshed_at"], "lists": status}


def load_index():
	"""The stored index, reloaded when the file changes; None when there is none."""
	path = store_path()
	try:
		key = (path, os.stat(path).st_mtime_ns)
	except OSError:
		return None
	if _cache["key"] != key:
		_cache["index"] = reputation.Index.from_json(_read_store().get("index"))
		_cache["key"] = key
	return _cache["index"]


def enrich(data):
	"""events.insert_event: tag a security event whose payload `ip` is on a list."""
	try:
		if data.get("event_type") != "security" or not cint(_settings().get("ip_reputation_enabled")):
			return data
		payload = json.loads(data["payload"]) if data.get("payload") else None
		if not isinstance(payload, dict) or "reputation" in payload:
			return data
		tagged, severity = reputation.tag(payload, data.get("severity"), load_index())
		if tagged is payload:
			return data
		return {**data, "payload": schema.dump_payload(tagged), "severity": severity}
	except Exception:
		frappe.logger("frappe_wolf").exception("IP reputation lookup failed")
		return data


@frappe.whitelist()
def get_reputation_status():
	frappe.only_for(("System Manager", "Wolf Manager"))
	data = _read_store()
	return {"enabled": bool(cint(_settings().get("ip_reputation_enabled"))), "refreshed_at": data.get("refreshed_at"),
	        "lists": data.get("lists") or {}}  # fmt: skip


@frappe.whitelist(methods=["POST"])
def refresh_now():
	frappe.only_for(("System Manager", "Wolf Manager"))
	return refresh_reputation(force=True)


# --- honeytokens -----------------------------------------------------------------------------


def touched(kind, value, extra=None):
	"""Record a honeytoken touch (critical), once per token and address every 10 minutes."""
	request = getattr(frappe.local, "request", None)
	ip = getattr(frappe.local, "request_ip", None)
	agent = request.headers.get("User-Agent", "")[:300] if request is not None else None
	techniques = {"user": ["T1078", "T1110"], "api_key": ["T1552"], "path": ["T1083"]}[kind]
	label = {"user": "decoy user", "api_key": "decoy API key", "path": "decoy path"}[kind]
	events.emit(
		"security",
		f"Honeytoken touched: {label} {value}" + (f" from {ip}" if ip else ""),
		severity="critical",
		category="honeytoken_triggered",
		source="frappe",
		payload={"kind": kind, "token": value, "ip": ip, "user_agent": agent,
		         "path": getattr(request, "path", None), "attack_techniques": techniques, **(extra or {})},  # fmt: skip
		fingerprint=schema.fingerprint("honeytoken", kind, value, ip),
		dedupe_minutes=10,
	)


def before_request():
	"""hooks.py before_request: decoy API keys and paths. Runs before Frappe's token check."""
	try:
		if frappe.flags.in_install or frappe.flags.in_migrate:
			return
		settings = _settings()
		keys = reputation.lines(settings.get("honeytoken_api_keys"))
		paths = reputation.lines(settings.get("honeytoken_paths"))
		if not keys and not paths:
			return
		request = frappe.local.request
		hit = None
		if keys:
			key = reputation.api_key_from_header(request.headers.get("Authorization"))
			if key and key in keys:
				hit = ("api_key", key)
		if not hit and paths and request.path.rstrip("/") in {p.rstrip("/") for p in paths}:
			hit = ("path", request.path)
		if hit:
			touched(*hit)
			frappe.db.commit()  # the request may fail (bad token): keep the event
	except Exception:
		frappe.logger("frappe_wolf").exception("honeytoken check failed")
