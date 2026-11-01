"""Frappe application security: what makes a site change weaker or riskier (no frappe imports).

The handlers in local_signals.py feed documents in; this module decides what is worth an event:

- `weakened(before, after)`: System Settings changes that lower a defence (2FA off, password
  policy relaxed, lockout loosened, longer sessions, guests allowed to upload), one row each.
- `script_change(before, after, fields)`: whether a Server/Client Script change can run new code
  (created, enabled, or its code, type or trigger changed); a description edit is not one.
- `url_domain(url)`: the host a Webhook sends documents to.
- `new_api_secret(value)`: a User save that wrote a new API secret (Desk shows saved Password
  fields as asterisks, so an unchanged one never looks new).
"""

from urllib.parse import urlsplit

# field, label, how it gets weaker: "off" (1 -> 0), "on" (0 -> 1), "lower", "higher", "longer" (HH:MM)
SETTINGS_RULES = [
	("enable_two_factor_auth", "Two-factor authentication", "off"),
	("enable_password_policy", "Password policy", "off"),
	("minimum_password_score", "Minimum password score", "lower"),
	("allow_consecutive_login_attempts", "Failed logins before lockout", "higher"),
	("allow_login_after_fail", "Lockout duration (seconds)", "lower"),
	("session_expiry", "Session expiry", "longer"),
	("deny_multiple_sessions", "One session per user", "off"),
	("disable_user_pass_login", "Password login disabled (SSO only)", "off"),
	("logout_on_password_reset", "Log out all sessions on password reset", "off"),
	("allow_guests_to_upload_files", "Guests may upload files", "on"),
]

SERVER_SCRIPT_FIELDS = ("script", "script_type", "reference_doctype", "doctype_event", "api_method",
                        "allow_guest", "event_frequency", "cron_format")  # fmt: skip
CLIENT_SCRIPT_FIELDS = ("script", "dt", "view")


def _number(value):
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _minutes(hhmm):
	"""`HH:MM` (System Settings session expiry) -> minutes, or None."""
	try:
		hours, minutes = str(hhmm).strip().split(":")[:2]
		return int(hours) * 60 + int(minutes)
	except (AttributeError, TypeError, ValueError):
		return None


def _truthy(value):
	return bool(_number(value)) if _number(value) is not None else bool(value)


def weakened(before, after):
	"""Rows `{field, label, old, new}` for every System Settings change that lowers a defence.

	`before` is None for a first save: nothing is compared then.
	"""
	if not before:
		return []
	rows = []
	for field, label, rule in SETTINGS_RULES:
		old, new = before.get(field), after.get(field)
		if old == new:
			continue
		if rule == "off":
			worse = _truthy(old) and not _truthy(new)
		elif rule == "on":
			worse = not _truthy(old) and _truthy(new)
		elif rule == "longer":
			o, n = _minutes(old), _minutes(new)
			worse = o is not None and n is not None and n > o
		else:
			o, n = _number(old), _number(new)
			if o is None or n is None:
				continue
			# 0 failed attempts before lockout means no lockout at all
			worse = (n < o) if rule == "lower" else (n > o or (n == 0 and o > 0))
		if worse:
			rows.append({"field": field, "label": label, "old": old, "new": new})
	return rows


def script_change(before, after, fields):
	"""'created', 'enabled', 'changed' or None for a Server/Client Script save.

	A disabled script runs nothing: only an enabled one (or one being enabled) counts.
	"""
	disabled_field = "disabled" if "disabled" in after else None
	enabled_now = not _truthy(after.get(disabled_field)) if disabled_field else _truthy(after.get("enabled"))
	if not enabled_now:
		return None
	if not before:
		return "created"
	enabled_before = not _truthy(before.get(disabled_field)) if disabled_field else _truthy(before.get("enabled"))
	if not enabled_before:
		return "enabled"
	if any((before.get(f) or "") != (after.get(f) or "") for f in fields):
		return "changed"
	return None


def url_domain(url):
	try:
		host = urlsplit(str(url or "").strip()).hostname
	except ValueError:
		return None
	return host.lower() if host else None


def new_api_secret(value):
	"""True when a save wrote a fresh API secret (not the asterisks Desk sends back)."""
	text = str(value or "")
	return bool(text) and set(text) != {"*"}


def excerpt(code, limit=300):
	"""The start of a script, for the event: enough to recognise it, not the whole program."""
	text = str(code or "")
	return text if len(text) <= limit else text[:limit] + "…"
