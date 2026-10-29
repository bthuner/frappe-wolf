import hmac
import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, now_datetime
from frappe.utils.password import get_decrypted_password

from frappe_wolf import __version__


def _request_json():
	data = frappe.request.get_json(silent=True) if frappe.request else None
	return data if isinstance(data, dict) else frappe.form_dict


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def register():
	"""Register (or re-register) a host agent. Returns credentials once."""
	data = _request_json()
	expected = get_decrypted_password("Wolf Settings", "Wolf Settings", "registration_token", raise_exception=False)
	token = str(data.get("registration_token") or "")
	if not expected or not hmac.compare_digest(expected, token):
		frappe.throw(_("Invalid registration token"), frappe.AuthenticationError)

	hostname = (data.get("hostname") or "").strip()[:140]
	if not hostname:
		frappe.throw(_("hostname is required"))

	secret = frappe.generate_hash(length=40)
	name = frappe.db.get_value("Wolf Agent", {"hostname": hostname})
	doc = frappe.get_doc("Wolf Agent", name) if name else frappe.new_doc("Wolf Agent")
	doc.update(
		{
			"hostname": hostname,
			"status": "Active",
			"agent_secret": secret,
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
	"""Validate X-Wolf-Agent-Id / X-Wolf-Agent-Secret headers and return the agent name."""
	agent_id = frappe.get_request_header("X-Wolf-Agent-Id") or ""
	secret = frappe.get_request_header("X-Wolf-Agent-Secret") or ""
	status = frappe.db.get_value("Wolf Agent", agent_id, "status") if agent_id else None
	stored = (
		get_decrypted_password("Wolf Agent", agent_id, "agent_secret", raise_exception=False) if status else None
	)
	if not stored or not secret or not hmac.compare_digest(stored, secret):
		frappe.throw(_("Invalid agent credentials"), frappe.AuthenticationError)
	if status != "Active":
		frappe.throw(_("Agent is disabled"), frappe.PermissionError)
	return agent_id
