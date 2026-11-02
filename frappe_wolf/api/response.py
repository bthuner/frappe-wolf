"""Human-validated response actions (wolf/response.py): suggest, approve, apply, audit.

- `get_response(incident)`: what Wolf proposes from the incident's security events, and the
  actions already taken (Wolf Response Action rows).
- `approve(...)` (POST, Wolf Manager / System Manager): the confirmation. It records a Wolf
  Response Action and a comment on the incident. User actions apply at once on this site;
  `block_ip` waits for the agent's next push (`pending_for_agent`), which applies it on its
  host and reports back (`record_results`). Agents only act when their own config allows it
  (`response.enabled`, off by default).
- `expire_actions` (hourly): blocks whose time is over are marked Expired (the nftables entry
  expires by itself on the host).
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from frappe_wolf.wolf import events, response
from frappe_wolf.wolf.spa import MANAGER_ROLES

EVENT_LIMIT = 500
RESULT_LENGTH = 500


def _incident(name):
	doc = frappe.get_doc("Wolf Incident", name)
	if not frappe.has_permission("Wolf Incident", "read", doc=doc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return doc


def _events(incident):
	rows = frappe.get_all(
		"Wolf Event",
		filters={"incident": incident, "event_type": "security"},
		fields=["event_type", "category", "title", "agent", "host", "bench", "site", "payload"],
		order_by="timestamp desc",
		limit=EVENT_LIMIT,
	)
	for r in rows:
		try:
			r["payload"] = json.loads(r.payload) if r.payload else {}
		except ValueError:
			r["payload"] = {}
	return rows


def _actions(incident):
	return frappe.get_all(
		"Wolf Response Action",
		filters={"incident": incident},
		fields=["name", "action", "target", "status", "agent", "host", "duration_hours", "approved_by",
		        "approved_at", "applied_at", "expires_at", "result", "reason"],  # fmt: skip
		order_by="creation desc",
	)


def _is_manager():
	return frappe.session.user == "Administrator" or bool(set(frappe.get_roles()) & set(MANAGER_ROLES))


def _key(action, target, agent):
	return (action, str(target), agent if action == "block_ip" else None)


def _proposals(doc):
	"""What Wolf proposes for this incident now (approve() only accepts one of these)."""
	taken = {_key(a.action, a.target, a.agent) for a in _actions(doc.name) if a.status in ("Approved", "Applied")}
	proposals = []
	for p in response.suggest(_events(doc.name)):
		key = (p["action"], p["target"], p["agent"] if p["action"] == "block_ip" else None)
		if key in taken:
			continue
		if p["action"] in response.USER_ACTIONS and not frappe.db.exists("User", p["target"]):
			continue  # a failed login for an unknown name: nothing to disable
		p["label"] = response.label(p["action"], p["target"], response.DEFAULT_HOURS, p["host"])
		proposals.append(p)
	return proposals


def _protected_user(user):
	"""A System Manager can only be acted on by another System Manager (or Administrator)."""
	if "System Manager" not in frappe.get_roles(user):
		return False
	return frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles()


@frappe.whitelist()
def get_response(incident):
	doc = _incident(incident)
	from frappe_wolf.wolf import window

	tz = events.system_tz()
	actions = []
	for a in _actions(doc.name):
		for field in ("approved_at", "applied_at", "expires_at"):
			a[field] = window.to_utc(a[field], tz) if a[field] else None  # the UI reads UTC ISO strings
		actions.append(a)
	return {
		"proposals": _proposals(doc),
		"actions": actions,
		"can_approve": _is_manager(),
		"default_hours": response.DEFAULT_HOURS,
		"max_hours": response.MAX_HOURS,
	}


def _apply_user_action(action, user):
	if action == "disable_user":
		frappe.db.set_value("User", user, "enabled", 0)
		from frappe.sessions import clear_sessions

		clear_sessions(user=user, force=True)
		return "user disabled and logged out"
	if action == "logout_sessions":
		from frappe.sessions import clear_sessions

		clear_sessions(user=user, force=True)
		return "every session logged out"
	if action == "revoke_api_keys":
		doc = frappe.get_doc("User", user)
		doc.api_key = None
		doc.api_secret = ""  # an empty Password field removes the stored secret
		doc.flags.ignore_permissions = True
		doc.save()
		return "API key and secret removed"
	raise ValueError(action)


@frappe.whitelist(methods=["POST"])
def approve(incident, action, target, agent=None, duration_hours=None, reason=None):
	"""The Wolf Manager's confirmation of one proposed action."""
	if not _is_manager():
		frappe.throw(_("Only a Wolf Manager can approve a response action."), frappe.PermissionError)
	doc = _incident(incident)
	hours = (cint(duration_hours) or response.DEFAULT_HOURS) if action == "block_ip" else None
	refused = response.check(action, target, frappe.session.user, hours)
	if refused:
		frappe.throw(refused, frappe.ValidationError)
	# only what Wolf proposed from this incident's own events: no free-form action on any user or host
	if _key(action, target, agent) not in {_key(p["action"], p["target"], p["agent"]) for p in _proposals(doc)}:
		frappe.throw(_("Wolf did not propose this action for this incident (or it was already approved)."),
		             frappe.ValidationError)  # fmt: skip
	if action in response.USER_ACTIONS and _protected_user(target):
		frappe.throw(_("{0} is a System Manager: only a System Manager can approve this.").format(target),
		             frappe.PermissionError)  # fmt: skip
	scope = {"host": doc.host, "bench": doc.bench, "site": doc.site}
	if action == "block_ip":
		if not agent or not frappe.db.exists("Wolf Agent", agent):
			frappe.throw(_("block_ip needs the agent that saw the address."), frappe.ValidationError)
		scope["host"] = frappe.db.get_value("Wolf Agent", agent, "hostname") or doc.host
	elif not frappe.db.exists("User", target):
		frappe.throw(_("User {0} does not exist on this site.").format(target), frappe.ValidationError)
	now = now_datetime()
	row = frappe.get_doc({
		"doctype": "Wolf Response Action", "incident": doc.name, "action": action, "target": target,
		"agent": agent if action == "block_ip" else None, **scope, "duration_hours": hours,
		"status": "Approved", "approved_by": frappe.session.user, "approved_at": now,
		"expires_at": add_to_date(now, hours=hours) if hours else None, "reason": (reason or "")[:500] or None,
	})  # fmt: skip
	row.insert(ignore_permissions=True)
	if action in response.USER_ACTIONS:
		try:
			row.result = _apply_user_action(action, target)
			row.status, row.applied_at = "Applied", now_datetime()
		except Exception as e:
			row.status, row.result = "Failed", str(e)[:RESULT_LENGTH]
		row.save(ignore_permissions=True)
	what = response.label(action, target, hours, scope.get("host"))
	note = {"Approved": "approved, waiting for the agent", "Applied": f"applied: {row.result}",
	        "Failed": f"failed: {row.result}"}[row.status]  # fmt: skip
	if action == "block_ip":
		from frappe_wolf.api import crowdsec

		sent = crowdsec.apply_block(row)  # the same block for every CrowdSec bouncer, when set up
		if sent:
			note += f"; {sent}"
	doc.add_comment("Comment", f"Response action {row.name} ({what}) {note}.")
	return row.as_dict()


def pending_for_agent(agent, now=None):
	"""Approved, unexpired blocks for this agent: what the push response hands it."""
	now = now or now_datetime()
	rows = frappe.get_all(
		"Wolf Response Action",
		filters={"agent": agent, "action": "block_ip", "status": "Approved", "expires_at": (">", now)},
		fields=["name", "target", "expires_at"],
		limit=100,
	)
	return [{"id": r.name, "action": "block_ip", "ip": r.target,
	         "seconds": max(int((r.expires_at - now).total_seconds()), 60)} for r in rows]  # fmt: skip


def record_results(agent, results):
	"""`[{id, ok, detail}]` sent by an agent about its actions (protocol §2)."""
	for item in results or []:
		if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
			continue  # a string id only: anything else would act as a filter
		row = frappe.db.get_value("Wolf Response Action", item["id"], ["agent", "status", "incident"], as_dict=True)
		if not row or row.agent != agent or row.status != "Approved":
			continue  # only the agent it was given to, once
		ok = bool(item.get("ok"))
		detail = str(item.get("detail") or ("applied" if ok else "failed"))[:RESULT_LENGTH]
		frappe.db.set_value("Wolf Response Action", item["id"], {
			"status": "Applied" if ok else "Failed", "result": detail, "applied_at": now_datetime() if ok else None,
		})  # fmt: skip
		from frappe_wolf.api import journal

		journal.capture("response_action", "Wolf Response Action", item["id"],
		                {"status": "Applied" if ok else "Failed", "result": detail, "agent": agent},
		                f"{item['id']} reported by {agent}: {'applied' if ok else 'failed'}")  # fmt: skip
		if row.incident:
			frappe.get_doc("Wolf Incident", row.incident).add_comment(
				"Comment", f"Response action {item['id']} {'applied' if ok else 'failed'} on the host: {detail}"
			)


def expire_actions(now=None):
	"""Scheduler (hourly)."""
	now = now or now_datetime()
	names = frappe.get_all(
		"Wolf Response Action",
		filters={"action": "block_ip", "status": ("in", ("Approved", "Applied")), "expires_at": ("<=", now)},
		pluck="name",
	)
	from frappe_wolf.api import journal

	for name in names:
		frappe.db.set_value("Wolf Response Action", name, "status", "Expired")
		journal.capture("response_action", "Wolf Response Action", name, {"status": "Expired"}, f"{name} expired")
	return names
