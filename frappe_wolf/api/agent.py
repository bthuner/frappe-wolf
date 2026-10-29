import hmac
import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, now_datetime
from frappe.utils.password import get_decrypted_password

from frappe_wolf import __version__, satellite
from frappe_wolf.wolf import agent_secrets

KINDS = ("agent", "satellite")


def _request_json():
	data = frappe.request.get_json(silent=True) if frappe.request else None
	return data if isinstance(data, dict) else frappe.form_dict


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def register():
	"""Register (or re-register) a host agent or a satellite site. Returns credentials once."""
	data = _request_json()
	if satellite.is_satellite():
		frappe.throw(_("This Wolf site is a satellite: register with its central site instead"), frappe.PermissionError)
	expected = get_decrypted_password("Wolf Settings", "Wolf Settings", "registration_token", raise_exception=False)
	token = str(data.get("registration_token") or "")
	if not expected or not hmac.compare_digest(expected, token):
		frappe.throw(_("Invalid registration token"), frappe.AuthenticationError)

	hostname = (data.get("hostname") or "").strip()[:140]
	if not hostname:
		frappe.throw(_("hostname is required"))
	kind = data.get("kind") or "agent"
	if kind not in KINDS:
		frappe.throw(_("kind must be agent or satellite"))

	secret = agent_secrets.new_secret()
	name = frappe.db.get_value("Wolf Agent", {"hostname": hostname})
	if name and (frappe.db.get_value("Wolf Agent", name, "kind") or "agent") != kind:
		frappe.throw(_("{0} is already registered as another kind of Wolf Agent").format(hostname))
	doc = frappe.get_doc("Wolf Agent", name) if name else frappe.new_doc("Wolf Agent")
	doc.update(
		{
			"hostname": hostname,
			"kind": kind,
			"status": "Active",
			"secret_hash": agent_secrets.hash_secret(secret),
			"secret_issued_at": now_datetime(),
			"agent_version": str(data.get("agent_version") or "")[:140],
			"os": str(data.get("os") or "")[:140],
			"benches": json.dumps(data.get("benches") or [], indent=1),
			"last_seen": now_datetime(),
			"last_ip": frappe.local.request_ip,
			"silence_reported": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()

	return {
		"agent_id": doc.name,
		"agent_secret": secret,
		"push_interval": push_interval(),
		"server_version": __version__,
	}


def push_interval():
	return cint(frappe.db.get_single_value("Wolf Settings", "push_interval")) or 30


def authenticate_agent():
	"""Validate X-Wolf-Agent-Id / X-Wolf-Agent-Secret headers and return the agent name.

	Only a salted hash of the secret is stored (wolf/agent_secrets.py).
	"""
	agent_id = frappe.get_request_header("X-Wolf-Agent-Id") or ""
	secret = frappe.get_request_header("X-Wolf-Agent-Secret") or ""
	row = frappe.db.get_value("Wolf Agent", agent_id, ["status", "secret_hash"], as_dict=True) if agent_id else None
	stored = (row.secret_hash or migrate_legacy_secret(agent_id)) if row else None
	# verify() runs even without a stored hash, so unknown ids cost the same as wrong secrets
	if not agent_secrets.verify(secret, stored) or not row:
		frappe.throw(_("Invalid agent credentials"), frappe.AuthenticationError)
	if row.status != "Active":
		frappe.throw(_("Agent is disabled"), frappe.PermissionError)
	return agent_id


def migrate_legacy_secret(agent_id):
	"""Agents registered before secrets were hashed: hash the encrypted Password value, then drop it.

	The post_model_sync patch does this for every agent. Doing it on first use too keeps agents
	working between a code update and `bench migrate` (a 401 makes an agent exit for good).
	"""
	try:
		plain = get_decrypted_password("Wolf Agent", agent_id, "agent_secret", raise_exception=False)
	except Exception:
		plain = None  # e.g. the site's encryption key changed: the agent has to re-register
	if not plain:
		return None
	hashed = agent_secrets.hash_secret(plain)
	frappe.db.set_value("Wolf Agent", agent_id, "secret_hash", hashed, update_modified=False)
	frappe.db.delete("__Auth", {"doctype": "Wolf Agent", "name": agent_id, "fieldname": "agent_secret"})
	return hashed
