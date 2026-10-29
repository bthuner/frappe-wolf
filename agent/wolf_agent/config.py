"""Agent configuration (JSON, default /etc/wolf-agent/config.json)."""

import json
import os
import socket

DEFAULT_CONFIG_PATH = "/etc/wolf-agent/config.json"
AUTH_LOG_CANDIDATES = ("/var/log/auth.log", "/var/log/secure")

DEFAULTS = {
	"url": None,
	"agent_id": None,
	"agent_secret": None,
	"hostname": None,
	"push_interval": 30,
	"spool_dir": "/var/lib/wolf-agent/spool",
	"verify_tls": True,
	"benches": [],
	# [{"path": "/var/log/foo.log", "parser": "traceback", "bench": null, "site": null}]
	"extra_logs": [],
	# null = auto-detect, a path, "journal", or false to disable
	"auth_log": None,
	"collect_host_metrics": True,
	# null = auto-detect (Press agent config.json present → "press"), "press" or "generic"
	"platform": None,
	# Press servers only (docs/press.md)
	"press": {
		"agent_dir": "/home/frappe/agent",
		"nginx_access_log": "/var/log/nginx/access.log",
		"rediscover_interval": 60,
		"deployments": True,
	},
	# Frappe Pilot benches only (docs/pilot.md). Admin API token: `token`, or one is
	# minted from `jwt_secret` / the bench.toml [admin] jwt_secret when readable.
	"pilot": {
		"deployments": True,
		"api_url": None,
		"token": None,
		"jwt_secret": None,
	},
	# alerts when the Wolf site itself stops working, sent without going through it
	# smtp: {"host", "port", "from", "to", "starttls", "ssl", "user", "password", "verify_tls"}
	# PII/secret scrubbing of event titles and payloads before they are shipped (scrub.py).
	# `ips` is off because the SSH brute-force / new-IP signals need the addresses.
	"scrub": {"enabled": True, "ips": False, "extra_patterns": [], "keep": []},
	"watchdog": {"enabled": True, "stale_minutes": 5, "webhook_url": None, "smtp": None},
}


def pilot_settings(cfg):
	merged = dict(DEFAULTS["pilot"])
	merged.update(cfg.get("pilot") or {})
	return merged


def press_settings(cfg):
	"""`press` section merged over the defaults (a partial section in the file is fine)."""
	merged = dict(DEFAULTS["press"])
	merged.update(cfg.get("press") or {})
	return merged


def config_path(cli_value=None):
	return cli_value or os.environ.get("WOLF_AGENT_CONFIG") or DEFAULT_CONFIG_PATH


def load_config(path):
	cfg = dict(DEFAULTS)
	with open(path) as f:
		cfg.update(json.load(f))
	cfg["hostname"] = cfg.get("hostname") or socket.gethostname()
	return cfg


def save_config(path, cfg):
	"""Write atomically with mode 0600: the file holds the agent secret."""
	os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
	tmp = path + ".tmp"
	fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w") as f:
		json.dump(cfg, f, indent=2, sort_keys=True)
		f.write("\n")
	os.chmod(tmp, 0o600)
	os.replace(tmp, path)


def resolve_auth_log(value, journal_available):
	"""Return a file path, "journal", or None for the configured auth log."""
	if value is False:
		return None
	if value:
		return value
	for candidate in AUTH_LOG_CANDIDATES:
		if os.path.exists(candidate):
			return candidate
	return "journal" if journal_available else None
