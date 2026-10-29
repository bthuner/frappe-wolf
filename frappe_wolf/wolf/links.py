"""Signed, expiring incident action links (pure, no frappe imports, unit tested).

A link carries the incident name, the action, an expiry (unix seconds) and an
optional actor (the email address the alert was sent to). All four are covered
by an HMAC-SHA256 signature keyed with a per-site secret.
"""

import hashlib
import hmac
import time
from urllib.parse import urlencode

ACTIONS = ("ack", "resolve")
ACTION_LABEL = {"ack": "Acknowledge", "resolve": "Resolve"}


def _message(incident, action, expires, actor):
	return "\n".join(("wolf-incident-action", str(incident), str(action), str(int(expires)), actor or "")).encode()


def sign(secret, incident, action, expires, actor=None):
	if action not in ACTIONS:
		raise ValueError(f"unknown action {action!r}")
	key = secret.encode() if isinstance(secret, str) else secret
	return hmac.new(key, _message(incident, action, expires, actor), hashlib.sha256).hexdigest()


def verify(secret, incident, action, expires, signature, actor=None, now=None):
	"""Returns (ok, reason). reason is None, "invalid" or "expired"."""
	if not secret or not incident or action not in ACTIONS or not signature:
		return False, "invalid"
	try:
		expires = int(expires)
	except (TypeError, ValueError):
		return False, "invalid"
	expected = sign(secret, incident, action, expires, actor)
	if not hmac.compare_digest(expected, str(signature)):
		return False, "invalid"
	if (time.time() if now is None else now) > expires:
		return False, "expired"
	return True, None


def build_params(secret, incident, action, ttl_seconds, actor=None, now=None):
	expires = int((time.time() if now is None else now) + ttl_seconds)
	params = {"incident": incident, "action": action, "expires": expires}
	if actor:
		params["actor"] = actor
	params["sig"] = sign(secret, incident, action, expires, actor)
	return params


def build_url(base, secret, incident, action, ttl_seconds, actor=None, now=None):
	return f"{base}?{urlencode(build_params(secret, incident, action, ttl_seconds, actor, now))}"
