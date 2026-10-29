"""Frappe Pilot (github.com/frappe/pilot) bench support.

Pilot keeps each bench under `<pilot install>/benches/<name>/` (by default
`~/pilot/benches/`) with a declarative `bench.toml`. This module reads the real
bench.toml keys, the bench's log layout, and turns Pilot tasks (updates,
migrations, app installs, ...) into Wolf `deployment` events, from the Admin
API or, when Wolf runs as the bench user, from the task files themselves.
See docs/pilot.md for the source files each assumption comes from.
"""

import base64
import glob
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .util import event, fingerprint, utc_iso

log = logging.getLogger(__name__)

PILOT_SEARCH_GLOBS = ("/home/*/pilot/benches/*",)
NGINX_ACCESS_LOG = "nginx-access.log"  # <bench>/logs/, log_format pilot_access

# Pilot task command → Wolf category. Commands and their fields come from
# pilot/tasks/*.py (`command: ClassVar[str]`).
DEPLOY_TASKS = {
	"update": "update",
	"retry-update": "update",
	"complete-migration": "migrate",
	"migrate": "migrate",
	"revert-migration": "revert",
	"revert-apps": "revert",
	"revert-site": "revert",
	"switch-branch": "update",
	"get-and-install-app": "app_install",
	"install-app": "app_install",
	"uninstall-app": "app_uninstall",
	"remove-app": "app_uninstall",
	"build": "build",
	"restart-services": "restart",
	"switch-lite-mode": "restart",
	"setup-nginx": "nginx",
	"restore-site": "restore",
	"reinstall-site": "restore",
}
ACTIVE = ("queued", "running")
STARTED = ("running", "success", "failed", "killed")
STATE_RETENTION = timedelta(days=3)
TOKEN_TTL = 3600


def is_pilot_toml(toml):
	"""A Pilot bench.toml: `[bench]` with the required `name` and `python` (docs/configuration.md)."""
	bench = toml.get("bench") if isinstance(toml, dict) else None
	return isinstance(bench, dict) and bool(bench.get("name")) and bool(bench.get("python"))


def _load_json(path):
	try:
		with open(path) as f:
			data = json.load(f)
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def site_domains(path, sites):
	"""Extra hostnames → site from site_config.json `domains` (strings or {"domain": ...})."""
	aliases = {}
	for site in sites:
		domains = _load_json(os.path.join(path, "sites", site, "site_config.json")).get("domains") or []
		if isinstance(domains, str):
			domains = [domains]
		for domain in domains:
			if isinstance(domain, dict):
				domain = domain.get("domain")
			if isinstance(domain, str) and domain and domain != site:
				aliases[domain] = site
	return aliases


def bench_log_files(logs_dir):
	"""Process logs Pilot's process managers write, by filename → Wolf parsers.

	supervisor: `<name>.log` + `<name>.error.log`; systemd: `<name>.log` +
	`<name>.log.error.log` (pilot/managers/processes/{supervisor,systemd_render}.py).
	Workers are `worker_<queue>_<i>` (pilot/managers/processes/definitions.py).
	"""
	files = {}
	for suffix in (".error.log", ".log.error.log"):
		files["web" + suffix] = ["traceback"]
		files["schedule" + suffix] = ["traceback"]
	for path in sorted(glob.glob(os.path.join(logs_dir, "worker_*.log"))):
		name = os.path.basename(path)
		if name.endswith(".error.log"):
			files[name] = ["worker", "traceback"]
		else:
			files[name] = ["worker"]
			files[name[: -len(".log")] + ".error.log"] = ["worker", "traceback"]
			files[name + ".error.log"] = ["worker", "traceback"]
	return files


def describe(path, toml):
	"""Pilot-specific fields merged into discovery's bench description."""
	logs_dir = os.path.join(path, "logs")
	redis = toml.get("redis") if isinstance(toml.get("redis"), dict) else {}
	admin = toml.get("admin") if isinstance(toml.get("admin"), dict) else {}
	production = toml.get("production") if isinstance(toml.get("production"), dict) else {}
	port = admin.get("port", 7000)
	info = {
		"platform": "pilot",
		"logs_dir": logs_dir,
		"nginx_access_log": os.path.join(logs_dir, NGINX_ACCESS_LOG),
		"log_files": bench_log_files(logs_dir),
		"production": bool(production.get("enabled")),
		# Admin backend gunicorn binds 127.0.0.1:<admin.port + 1> (pilot/config/admin.py internal_port)
		"pilot_api_url": f"http://127.0.0.1:{int(port) + 1}" if str(port).isdigit() else None,
	}
	queue_port = redis.get("queue_port")
	if str(queue_port or "").isdigit():
		info["redis_queue"] = f"redis://127.0.0.1:{int(queue_port)}"
	return info


def _b64(data):
	return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_token(secret, ttl=TOKEN_TTL, now=None):
	"""A bench-scoped Admin session JWT, the shape Pilot's Session._encode signs (HS256)."""
	now = int(now if now is not None else time.time())
	header = {"alg": "HS256", "typ": "JWT"}
	payload = {"sub": "admin", "iat": now, "exp": now + ttl, "scope": "bench", "jti": "wolf-" + secrets.token_urlsafe(8)}
	signing_input = _b64(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64(
		json.dumps(payload, separators=(",", ":")).encode()
	)
	sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
	return signing_input + "." + _b64(sig)


def _parse_iso(value):
	if not value:
		return None
	try:
		dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
	except ValueError:
		return None
	return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class PilotUnavailable(Exception):
	pass


class PilotTaskWatcher:
	"""Pilot tasks of one bench → `deployment` events (at start) + `error` events (on failure).

	Sources, in order: the Admin API (`GET /api/v1/tasks`, Bearer token: the
	configured `token`, or one minted from the bench's `[admin] jwt_secret`), then
	the task files `<bench>/tasks/<id>/meta.json` + `status` when readable.
	Task ids already reported are kept in `state_path`; the first poll only
	picks up tasks still queued/running (no history backfill).
	"""

	def __init__(self, host, bench, state_path=None, api_url=None, token=None, jwt_secret=None, toml_reader=None, timeout=10):
		self.host = host
		self.bench = bench["name"]
		self.path = bench["path"]
		self.api_url = (api_url or bench.get("pilot_api_url") or "").rstrip("/") or None
		self.token = token
		self.jwt_secret = jwt_secret
		self.toml_reader = toml_reader
		self.timeout = timeout
		self.state_path = state_path
		self._minted = None
		self._warned = set()
		self.state = self._load()

	# -- state -------------------------------------------------------------
	def _load(self):
		state = {"baseline": None, "started": {}, "finished": {}}
		if self.state_path:
			try:
				with open(self.state_path) as f:
					state.update(json.load(f).get(self.bench) or {})
			except (OSError, ValueError, AttributeError):
				pass
		return state

	def _save(self):
		if not self.state_path:
			return
		try:
			with open(self.state_path) as f:
				data = json.load(f)
			if not isinstance(data, dict):
				data = {}
		except (OSError, ValueError):
			data = {}
		data[self.bench] = self.state
		tmp = self.state_path + ".tmp"
		try:
			os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
			with open(tmp, "w") as f:
				json.dump(data, f)
			os.replace(tmp, self.state_path)
		except OSError as e:
			log.warning("cannot save Pilot task state %s: %s", self.state_path, e)

	# -- sources -----------------------------------------------------------
	def _bearer(self):
		if self.token:
			return self.token
		secret = self.jwt_secret
		if not secret and self.toml_reader:
			admin = (self.toml_reader(self.path) or {}).get("admin") or {}
			secret = admin.get("jwt_secret") if isinstance(admin, dict) else None
		if not secret:
			return None
		now = time.time()
		if not self._minted or self._minted[1] - now < 300:
			self._minted = (mint_token(secret, now=now), now + TOKEN_TTL)
		return self._minted[0]

	def _from_api(self):
		if not self.api_url:
			raise PilotUnavailable("no Admin API URL")
		bearer = self._bearer()
		if not bearer:
			raise PilotUnavailable("no Admin API token (set pilot.token, or make bench.toml [admin] jwt_secret readable)")
		req = urllib.request.Request(
			f"{self.api_url}/api/v1/tasks?limit=100",
			headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
		)
		try:
			with urllib.request.urlopen(req, timeout=self.timeout) as resp:
				data = json.load(resp)
		except urllib.error.HTTPError as e:
			if e.code in (401, 403):
				self._minted = None
			raise PilotUnavailable(f"Admin API HTTP {e.code}") from e
		except (OSError, ValueError) as e:
			raise PilotUnavailable(f"Admin API: {e}") from e
		if not isinstance(data, list):
			raise PilotUnavailable("Admin API returned no task list")
		return data

	def _from_files(self):
		root = os.path.join(self.path, "tasks")
		try:
			names = os.listdir(root)
		except OSError as e:
			raise PilotUnavailable(f"cannot read {root}: {e}") from e
		tasks = []
		for name in names:
			meta = _load_json(os.path.join(root, name, "meta.json"))
			if not meta.get("command"):
				continue
			try:
				with open(os.path.join(root, name, "status")) as f:
					status = f.read().strip()
			except OSError:
				status = "running"  # pilot/managers/task/reader.py: no status file means running
			meta = dict(meta, status=status)
			meta.setdefault("task_id", name)
			tasks.append(meta)
		return tasks

	def fetch(self):
		errors = []
		for source in (self._from_api, self._from_files):
			try:
				return source()
			except PilotUnavailable as e:
				errors.append(str(e))
		key = "; ".join(errors)
		if key not in self._warned:
			log.warning("Pilot tasks for bench %s unavailable: %s", self.bench, key)
			self._warned.add(key)
		return None

	# -- events ------------------------------------------------------------
	def _sites(self, args):
		sites = []
		if isinstance(args.get("site"), str) and args["site"]:
			sites.append(args["site"])
		for site in args.get("sites") or []:
			if isinstance(site, str) and site not in sites:
				sites.append(site)
		return sites

	def _deployment(self, task, start, sites):
		command = task["command"]
		site = sites[0] if len(sites) == 1 else None
		return event(
			"deployment",
			"pilot",
			"info",
			utc_iso(start),
			self.host,
			f"Pilot: {command} on {site or self.bench}"[:140],
			fingerprint("pilot_task", self.host, self.bench, task["task_id"]),
			{
				"note": command,
				"source": "pilot",
				"task": task["task_id"],
				"pilot_command": command,
				"pilot_category": DEPLOY_TASKS[command],
				"sites": sites,
			},
			source="pilot",
			bench=self.bench,
			site=site,
		)

	def _failure(self, task, end, sites):
		failure = task.get("failure") if isinstance(task.get("failure"), dict) else {}
		site = sites[0] if len(sites) == 1 else None
		return event(
			"error",
			"pilot_task_failed",
			"warning",
			utc_iso(end),
			self.host,
			f"Pilot task failed: {task['command']}"[:140],
			fingerprint("pilot_task_failed", self.host, self.bench, task["task_id"]),
			{
				"task": task["task_id"],
				"pilot_command": task["command"],
				"exit_code": task.get("exit_code"),
				"failure_code": failure.get("code"),
				"failure_message": (failure.get("message") or "")[:2000] or None,
				"sites": sites,
			},
			source="pilot",
			bench=self.bench,
			site=site,
		)

	def poll(self, now=None):
		now = now or datetime.now(timezone.utc)
		tasks = self.fetch()
		if tasks is None:
			return None
		started, finished = self.state["started"], self.state["finished"]
		baseline = _parse_iso(self.state.get("baseline"))
		first = baseline is None
		if first:
			baseline = now
			self.state["baseline"] = utc_iso(now)

		events = []
		for task in sorted(tasks, key=lambda t: str(t.get("task_id"))):
			command, tid = task.get("command"), task.get("task_id")
			if command not in DEPLOY_TASKS or not tid:
				continue
			status = str(task.get("status") or "")
			start = _parse_iso(task.get("started_at"))
			end = _parse_iso(task.get("finished_at")) or now
			if first and status not in ACTIVE:
				finished[tid] = utc_iso(end)  # history: never report it
				continue
			if not first and tid not in started and start and status not in ACTIVE:
				if start < baseline or start < now - STATE_RETENTION:
					continue  # finished before Wolf first looked, or already forgotten
			sites = self._sites(task.get("args") or {})
			if tid not in started and status in STARTED and start:
				events.append(self._deployment(task, start, sites))
				started[tid] = utc_iso(start)
			if status not in ACTIVE and tid not in finished:
				if status == "failed" and tid in started:
					events.append(self._failure(task, end, sites))
				finished[tid] = utc_iso(end)

		cutoff = now - STATE_RETENTION
		for bucket in (started, finished):
			for tid in [t for t, ts in bucket.items() if (_parse_iso(ts) or now) < cutoff]:
				del bucket[tid]
		self._save()
		return events
