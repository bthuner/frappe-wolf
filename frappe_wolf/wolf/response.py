"""Human-validated response: what Wolf may propose, and what it refuses (no frappe imports).

Wolf proposes; a Wolf Manager confirms each action; nothing runs on its own.

- `block_ip`: proposed for the public addresses in an incident's security events, applied by
  the agent that saw them, as an nftables entry that expires by itself (1 to 168 hours).
  Loopback, private, link-local and reserved addresses are never proposed nor accepted.
- `disable_user`, `logout_sessions`, `revoke_api_keys`: proposed for the users named by the
  site's own security events, applied on the Wolf site itself. Administrator, Guest and the
  approver are refused, so a click cannot lock everyone out.
"""

import ipaddress

ACTIONS = ("block_ip", "disable_user", "logout_sessions", "revoke_api_keys")
USER_ACTIONS = ("disable_user", "logout_sessions", "revoke_api_keys")
LABELS = {
	"block_ip": "Block {target} for {hours} h on {host}",
	"disable_user": "Disable user {target}",
	"logout_sessions": "Log out every session of {target}",
	"revoke_api_keys": "Revoke the API keys of {target}",
}
DEFAULT_HOURS = 24
MAX_HOURS = 168
PROTECTED_USERS = {"administrator", "guest"}
# categories whose payload `user` is the account at risk or misused (not the one who acted)
USER_CATEGORIES = {"frappe_failed_login", "new_admin_ip", "admin_login_new_country", "privileged_user", "api_key_generated"}
MAX_PROPOSALS = 20


def blockable(address):
	"""(True, None) for a public unicast address, else (False, why)."""
	try:
		ip = ipaddress.ip_address(str(address).strip())
	except ValueError:
		return False, "not an IP address"
	if getattr(ip, "ipv4_mapped", None):
		ip = ip.ipv4_mapped
	if ip.is_loopback or ip.is_unspecified:
		return False, "a loopback or unspecified address"
	if ip.is_private or ip.is_link_local:
		return False, "a private or link-local address: block it on your own network instead"
	if ip.is_multicast or ip.is_reserved or not ip.is_global:
		return False, "a reserved or non-global address"
	return True, None


def check(action, target, approver=None, hours=None):
	"""None when the action may run, else the reason it is refused."""
	if action not in ACTIONS:
		return f"unknown action {action!r}"
	if not target:
		return "no target"
	if action == "block_ip":
		ok, why = blockable(target)
		if not ok:
			return f"{target} is {why}"
		if hours is not None and not (1 <= int(hours) <= MAX_HOURS):
			return f"duration must be 1 to {MAX_HOURS} hours"
		return None
	user = str(target).strip().lower()
	if user in PROTECTED_USERS:
		return f"{target} is protected"
	if approver and user == str(approver).lower():
		return "you cannot apply this to your own account"
	return None


def suggest(events):
	"""Proposals `{action, target, agent, host, bench, site, reason}` from an incident's events.

	`events`: dicts with event_type, category, agent, host, bench, site and payload (a dict).
	"""
	out, seen = [], set()

	def add(action, target, ev, reason):
		key = (action, target, ev.get("agent") if action == "block_ip" else None)
		if key in seen or len(out) >= MAX_PROPOSALS or check(action, target):
			return
		seen.add(key)
		out.append({"action": action, "target": target, "agent": ev.get("agent"), "host": ev.get("host"),
		            "bench": ev.get("bench"), "site": ev.get("site"), "reason": reason})  # fmt: skip

	for ev in events:
		if ev.get("event_type") != "security":
			continue
		payload = ev.get("payload") or {}
		ip = payload.get("ip")
		if ip and ev.get("agent"):  # only an agent can apply a block on its host
			add("block_ip", str(ip), ev, f"{ev.get('category')}: {ev.get('title') or ip}")
		user = payload.get("user")
		if user and ev.get("category") in USER_CATEGORIES and not ev.get("agent"):
			for action in USER_ACTIONS:
				add(action, str(user), ev, f"{ev.get('category')}: {ev.get('title') or user}")
	return out


def label(action, target, hours=None, host=None):
	return LABELS[action].format(target=target, hours=hours or DEFAULT_HOURS, host=host or "its host")
