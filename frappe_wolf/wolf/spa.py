"""Boot data for the Wolf UI at /wolf (frontend/), kept free of frappe imports for unit tests.

The SPA renders what the server allows: `can` only hides buttons, the APIs still check every
action. `capabilities` says which views have a backend behind them, so the UI explains a
missing feature instead of showing an empty page.
"""

READ_ROLES = ("System Manager", "Wolf Manager", "Wolf User")
MANAGER_ROLES = ("System Manager", "Wolf Manager")

# Desk pages that still do the job of a view the SPA does not have yet (Desk stays available).
DESK_FALLBACKS = {
	"overview": "/app/wolf-overview",
	"incidents": "/app/wolf-incident",
	"events": "/app/wolf-event",
	"errors": "/app/wolf-event?event_type=error",
	"metrics": "/app/wolf-overview",
	"monitoring": "/app/wolf-agent",
	"alert_rules": "/app/wolf-alert-rule",
	"settings": "/app/wolf-settings",
}


def access(user, roles):
	"""'login' for guests, 'forbidden' without a Wolf role, None when the user may open /wolf."""
	if not user or user == "Guest":
		return "login"
	if user == "Administrator" or set(roles) & set(READ_ROLES):
		return None
	return "forbidden"


def permissions(user, roles):
	"""What the UI may offer. The server enforces the same rules (api/incident.py, wolf/workflow.py)."""
	manager = user == "Administrator" or bool(set(roles) & set(MANAGER_ROLES))
	return {
		"read": access(user, roles) is None,
		"comment": access(user, roles) is None,  # read permission on the incident is enough
		"act": manager,  # acknowledge, resolve, assign, merge, split, postmortem
		"configure": manager,  # alert rules, silences, channels, settings (in Desk)
	}


def capabilities(is_satellite, restricted):
	"""Which views have a backend. Raw-log search has none yet: logs are parsed into events."""
	return {
		"logs": {
			"available": False,
			"reason": "Wolf stores parsed events and metrics, not raw log lines, so there is nothing to search yet.",
		},
		"metrics": {"available": True, "resolutions": ["raw", "1m", "1h"]},
		"incidents": {"available": not is_satellite},
		"realtime": {"available": False, "reason": "Live updates are not wired yet; views refresh on demand."},
		# a restricted user (Wolf User Scope rows) only sees granted sites, benches and hosts
		"restricted_scope": restricted,
	}


def boot(user, full_name, roles, time_zone, server_version, is_satellite, restricted, desk_url="/app"):
	return {
		"user": {"name": user, "full_name": full_name or user},
		"can": permissions(user, roles),
		"capabilities": capabilities(is_satellite, restricted),
		"time_zone": time_zone,
		"server_version": server_version,
		"satellite": bool(is_satellite),
		"desk": {"home": desk_url, "fallbacks": DESK_FALLBACKS},
	}
