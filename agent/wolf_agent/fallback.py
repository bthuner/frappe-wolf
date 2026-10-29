"""Fallback alert channels that do not go through the Wolf site: webhook and SMTP."""

import json
import logging
import smtplib
import socket
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import __version__

log = logging.getLogger(__name__)


class FallbackError(Exception):
	"""Every configured fallback channel failed."""


def channels(wd_cfg):
	"""Names of the fallback channels configured in the `watchdog` config section."""
	wd_cfg = wd_cfg or {}
	names = []
	if wd_cfg.get("webhook_url"):
		names.append("webhook")
	if (wd_cfg.get("smtp") or {}).get("host"):
		names.append("smtp")
	return names


def send_webhook(url, text, verify_tls=True, timeout=10):
	"""POST `{"text": ...}` (Slack, Mattermost, Rocket.Chat and most chat webhooks)."""
	req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST")
	req.add_header("Content-Type", "application/json")
	req.add_header("User-Agent", f"wolf-agent/{__version__}")
	context = ssl._create_unverified_context() if url.startswith("https") and not verify_tls else None
	try:
		with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
			resp.read()
	except urllib.error.HTTPError as e:
		e.close()
		raise FallbackError(f"webhook HTTP {e.code}") from e
	except (urllib.error.URLError, OSError) as e:
		raise FallbackError(f"webhook: {e}") from e


def _recipients(value):
	if isinstance(value, str):
		value = value.split(",")
	return [r.strip() for r in value or [] if r and r.strip()]


def send_smtp(smtp_cfg, subject, text, timeout=15):
	"""Send a plain-text mail. Keys: host, port, from, to (list or comma string),
	starttls (default: port 587), ssl (implicit TLS, port 465), user, password, verify_tls."""
	host = smtp_cfg.get("host")
	to = _recipients(smtp_cfg.get("to"))
	if not host or not to:
		raise FallbackError("smtp: host and to are required")
	use_ssl = bool(smtp_cfg.get("ssl"))
	port = int(smtp_cfg.get("port") or (465 if use_ssl else 587 if smtp_cfg.get("starttls") else 25))
	starttls = smtp_cfg.get("starttls", port == 587) and not use_ssl
	sender = smtp_cfg.get("from") or f"wolf-agent@{socket.getfqdn()}"
	context = ssl.create_default_context()
	if smtp_cfg.get("verify_tls") is False:
		context = ssl._create_unverified_context()

	msg = EmailMessage()
	msg["Subject"] = subject
	msg["From"] = sender
	msg["To"] = ", ".join(to)
	msg["Date"] = formatdate(localtime=True)
	msg["Message-ID"] = make_msgid(domain="wolf-agent")
	msg.set_content(text)
	try:
		if use_ssl:
			client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
		else:
			client = smtplib.SMTP(host, port, timeout=timeout)
		with client:
			if starttls:
				client.starttls(context=context)
			if smtp_cfg.get("user"):
				client.login(smtp_cfg["user"], smtp_cfg.get("password") or "")
			client.send_message(msg, from_addr=sender, to_addrs=to)
	except (smtplib.SMTPException, OSError) as e:
		raise FallbackError(f"smtp {host}:{port}: {e}") from e


def send(wd_cfg, subject, text):
	"""Send through every configured channel; return the names that delivered.

	Returns [] when no channel is configured. Raises FallbackError when channels are
	configured but none of them delivered.
	"""
	wd_cfg = wd_cfg or {}
	delivered, errors = [], []
	for name in channels(wd_cfg):
		try:
			if name == "webhook":
				send_webhook(wd_cfg["webhook_url"], f"*{subject}*\n{text}", wd_cfg.get("verify_tls", True))
			else:
				send_smtp(wd_cfg["smtp"], subject, text)
			delivered.append(name)
		except FallbackError as e:
			errors.append(str(e))
		except Exception as e:  # a broken channel must never take the agent down
			errors.append(f"{name}: {type(e).__name__}: {e}")
	if errors and not delivered:
		raise FallbackError("; ".join(errors))
	for error in errors:
		log.warning("fallback channel failed: %s", error)
	return delivered
