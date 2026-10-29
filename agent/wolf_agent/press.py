"""Frappe Press (Frappe Cloud) server support: bench discovery and deploy correlation.

Everything here reads what the Press server agent (github.com/frappe/agent)
leaves on disk; no Press credentials are needed. See docs/press.md for the
source files and lines each assumption comes from.

Layout, as set up by the Press agent:

- `/home/frappe/agent/config.json` holds `benches_directory` (default
  `/home/frappe/benches`) and `nginx_directory`.
- Each bench is `<benches_directory>/<bench-name>/` with `config.json` (bench
  config: `docker_image`, `web_port`, `rq_port`, ...), `sites/`, `logs/` and
  `config/`; the apps live inside the bench's Docker image, not on the host.
- The agent's job history is the SQLite database `jobs.sqlite3` in the agent
  directory (peewee tables `jobmodel` and `stepmodel`).
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .util import event, fingerprint, utc_iso

log = logging.getLogger(__name__)

DEFAULT_AGENT_DIR = "/home/frappe/agent"
DEFAULT_BENCHES_DIR = "/home/frappe/benches"
DEFAULT_NGINX_ACCESS_LOG = "/var/log/nginx/access.log"
JOBS_DB = "jobs.sqlite3"

# Press agent job name → (category, positional parameter names of the job function).
# Parameter names come from the @job-decorated methods in frappe/agent
# (agent/server.py, agent/bench.py, agent/site.py). `name` is the site (or the
# bench for New Bench), `target` / `bench` the bench the site ends up on.
DEPLOY_JOBS = {
	"New Bench": ("new_bench", ("bench",)),
	"Update Site Pull": ("update", ("site", "source", "bench")),
	"Update Site Migrate": ("migrate", ("site", "source", "bench")),
	"Recover Failed Site Migrate": ("recover", ("site", "source", "bench")),
	"Recover Failed Site Pull": ("recover", ("site", "source", "bench")),
	"Recover Failed Site Update": ("recover", ("site", "bench")),
	"Move Site to Bench": ("update", ("site", "source", "bench")),
	"Update Bench In Place": ("update", ("sites",)),
	"Patch App": ("patch", ()),
	"Migrate Site": ("migrate", ()),
	"Install App on Site": ("app_install", ()),
	"Uninstall App on Site": ("app_uninstall", ()),
	"Bench Restart": ("restart", ()),
	"Rebuild Bench Assets": ("rebuild", ()),
}
TERMINAL = ("Success", "Failure")
# A job whose bench cannot be read yet (Bench/Site methods carry no bench name in
# their arguments) waits this long for its steps to name it, then ships host-scoped.
SCOPE_WAIT = timedelta(minutes=2)
MAX_JOBS_PER_POLL = 500
# Jobs stuck Pending/Running longer than this are given up on.
STALE_AFTER = timedelta(hours=24)

_SITE_FLAG_RE = re.compile(r"--site\s+([A-Za-z0-9._-]+)")


def read_agent_config(agent_dir=DEFAULT_AGENT_DIR):
	"""The Press agent's config.json, or None when this is not a Press server."""
	try:
		with open(os.path.join(agent_dir, "config.json")) as f:
			cfg = json.load(f)
	except (OSError, ValueError):
		return None
	if not isinstance(cfg, dict) or not cfg.get("benches_directory"):
		return None
	return cfg


def detect(agent_dir=DEFAULT_AGENT_DIR):
	return read_agent_config(agent_dir) is not None


def benches_directory(agent_dir=DEFAULT_AGENT_DIR):
	cfg = read_agent_config(agent_dir) or {}
	return cfg.get("benches_directory") or DEFAULT_BENCHES_DIR


def _load_json(path):
	try:
		with open(path) as f:
			data = json.load(f)
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def is_press_bench(path):
	"""Press bench: `config.json` with a `docker_image`, plus `sites/` (no apps/ on the host)."""
	if not os.path.isdir(os.path.join(path, "sites")):
		return False
	return bool(_load_json(os.path.join(path, "config.json")).get("docker_image"))


def _sites(path):
	sites_dir = os.path.join(path, "sites")
	try:
		names = sorted(os.listdir(sites_dir))
	except OSError:
		return []
	return [n for n in names if os.path.isfile(os.path.join(sites_dir, n, "site_config.json"))]


def _site_domains(path, sites):
	"""Custom domains → site, from each site_config.json `domains` (what the bench nginx maps)."""
	aliases = {}
	for site in sites:
		domains = _load_json(os.path.join(path, "sites", site, "site_config.json")).get("domains") or []
		if isinstance(domains, str):
			domains = [domains]
		for domain in domains:
			if isinstance(domain, dict):
				domain = domain.get("domain") or domain.get("name")
			if isinstance(domain, str) and domain and domain != site:
				aliases[domain] = site
	return aliases


def describe_press_bench(path, nginx_access_log=DEFAULT_NGINX_ACCESS_LOG):
	path = os.path.abspath(path)
	bench_config = _load_json(os.path.join(path, "config.json"))
	sites = _sites(path)
	rq_port = bench_config.get("rq_port")
	nginx = nginx_access_log if nginx_access_log and os.path.exists(nginx_access_log) else None
	return {
		"name": os.path.basename(path),
		"path": path,
		"sites": sites,
		"logs_dir": os.path.join(path, "logs"),
		"nginx_access_log": nginx,
		# rq_port is published on the host (docker run -p 0.0.0.0:<rq_port>:11000);
		# common_site_config's redis_queue is the in-container address.
		"redis_queue": f"redis://127.0.0.1:{int(rq_port)}" if str(rq_port or "").isdigit() else None,
		"site_domains": _site_domains(path, sites),
		"platform": "press",
		"docker_image": bench_config.get("docker_image"),
		"bench_toml": False,
	}


def discover_press_benches(agent_dir=DEFAULT_AGENT_DIR, nginx_access_log=DEFAULT_NGINX_ACCESS_LOG):
	root = benches_directory(agent_dir)
	try:
		names = sorted(os.listdir(root))
	except OSError as e:
		log.warning("cannot list Press benches directory %s: %s", root, e)
		return []
	benches = []
	for name in names:
		path = os.path.join(root, name)
		if os.path.isdir(path) and is_press_bench(path):
			benches.append(describe_press_bench(path, nginx_access_log=nginx_access_log))
	return benches


def _parse_dt(value):
	"""Peewee stores naive `datetime.now()` (server local time) as text."""
	if not value:
		return None
	if isinstance(value, datetime):
		return value
	text = str(value).strip().replace("T", " ")
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	return None


def _json(text):
	try:
		return json.loads(text) if text else None
	except (TypeError, ValueError):
		return None


class JobsDBError(Exception):
	pass


def open_jobs_db(path):
	"""Open the Press agent's jobs.sqlite3 strictly read-only.

	The database runs in WAL mode; `mode=ro` works while the agent keeps the
	-wal/-shm files around. If that fails (e.g. -shm not readable), fall back to
	`immutable=1`, which skips locking and may miss the very latest writes.
	"""
	if not os.path.exists(path):
		raise JobsDBError(f"{path} does not exist")
	errors = []
	for params in ("mode=ro", "mode=ro&immutable=1"):
		try:
			conn = sqlite3.connect(f"file:{path}?{params}", uri=True, timeout=5)
			conn.execute("select 1 from jobmodel limit 1").fetchall()
			return conn
		except sqlite3.Error as e:
			errors.append(f"{params}: {e}")
	raise JobsDBError("; ".join(errors))


class PressJobWatcher:
	"""Turns Press agent jobs (deploys, updates, migrations) into Wolf events.

	One `deployment` event per job when it starts (timestamp = job start), and
	one `error` event if it ends in Failure. Emitted job ids are persisted in
	`state_path` so restarts never emit twice; on first run only jobs that are
	still pending/running are picked up (no history backfill).
	"""

	def __init__(self, host, agent_dir=DEFAULT_AGENT_DIR, state_path=None, known_benches=None):
		self.host = host
		self.agent_dir = agent_dir
		self.db_path = os.path.join(agent_dir, JOBS_DB)
		self.state_path = state_path
		self.known_benches = known_benches or (lambda: set())
		self.state = self._load_state()
		self._warned = False

	def _load_state(self):
		state = {"floor": None, "started": [], "finished": []}
		if self.state_path:
			try:
				with open(self.state_path) as f:
					state.update(json.load(f))
			except (OSError, ValueError):
				pass
		return state

	def _save_state(self):
		if not self.state_path:
			return
		tmp = self.state_path + ".tmp"
		try:
			os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
			with open(tmp, "w") as f:
				json.dump(self.state, f)
			os.replace(tmp, self.state_path)
		except OSError as e:
			log.warning("cannot save Press job state %s: %s", self.state_path, e)

	def _rows(self, conn, floor):
		names = list(DEPLOY_JOBS)
		marks = ",".join("?" * len(names))
		cur = conn.execute(
			f"select id, name, status, agent_job_id, data, enqueue, start, end from jobmodel "
			f"where id > ? and name in ({marks}) order by id limit {MAX_JOBS_PER_POLL}",
			[floor, *names],
		)
		cols = [c[0] for c in cur.description]
		return [dict(zip(cols, row)) for row in cur.fetchall()]

	def _steps(self, conn, job_id):
		try:
			cur = conn.execute("select name, status, data from stepmodel where job_id = ? order by id", [job_id])
		except sqlite3.Error:
			return []
		return [{"name": n, "status": s, "data": d} for n, s, d in cur.fetchall()]

	def _scope(self, row, steps):
		"""(bench, sites) for a job, from its arguments, then from its steps' commands."""
		_, params = DEPLOY_JOBS[row["name"]]
		bench, sites = None, []
		data = _json(row.get("data"))
		if isinstance(data, dict) and "function" in data:
			args = data.get("args") or []
			kwargs = data.get("kwargs") or {}
			values = dict(zip(params, args))
			# keyword names in frappe/agent: name/target/bench/sites
			for key, role in (("name", params[0] if params else None), ("target", "bench"), ("bench", "bench"), ("sites", "sites")):
				if role and key in kwargs and role not in values:
					values[role] = kwargs[key]
			bench = values.get("bench") if isinstance(values.get("bench"), str) else None
			if isinstance(values.get("site"), str):
				sites.append(values["site"])
			if isinstance(values.get("sites"), list):
				sites += [s for s in values["sites"] if isinstance(s, str)]

		known = self.known_benches()
		texts = [row.get("data") or ""] + [s.get("data") or "" for s in steps]
		if not bench and known:
			for text in texts:
				found = next((b for b in sorted(known, key=len, reverse=True) if b in text), None)
				if found:
					bench = found
					break
		if not sites:
			for text in texts:
				for site in _SITE_FLAG_RE.findall(text):
					if site not in sites:
						sites.append(site)
		return bench, sites

	def _deployment_event(self, row, bench, sites, start):
		category, _ = DEPLOY_JOBS[row["name"]]
		site = sites[0] if len(sites) == 1 else None
		target = site or bench or self.host
		return event(
			"deployment",
			"press",
			"info",
			utc_iso(start),
			self.host,
			f"Press: {row['name']} on {target}"[:140],
			fingerprint("press_job", self.host, row["id"]),
			{
				"note": row["name"],
				"source": "press",
				"press_job": row["name"],
				"press_category": category,
				"agent_job_id": row.get("agent_job_id"),
				"local_job_id": row["id"],
				"sites": sites,
			},
			source="press-agent",
			bench=bench,
			site=site,
		)

	def _failure_event(self, row, bench, sites, end, steps):
		site = sites[0] if len(sites) == 1 else None
		failed = [s["name"] for s in steps if s.get("status") == "Failure"]
		data = _json(row.get("data"))
		tb = data.get("traceback") if isinstance(data, dict) else None
		output = data.get("output") if isinstance(data, dict) else None
		return event(
			"error",
			"press_job_failed",
			"warning",
			utc_iso(end),
			self.host,
			f"Press job failed: {row['name']}" + (f" ({failed[-1]})" if failed else ""),
			fingerprint("press_job_failed", self.host, row["id"]),
			{
				"press_job": row["name"],
				"agent_job_id": row.get("agent_job_id"),
				"local_job_id": row["id"],
				"failed_steps": failed,
				"sites": sites,
				"traceback": (tb or "")[-4000:] or None,
				"output": (str(output) if output else "")[-2000:] or None,
			},
			source="press-agent",
			bench=bench,
			site=site,
		)

	def poll(self, now=None):
		"""Events since the last poll, or None when jobs.sqlite3 cannot be read."""
		now = now or datetime.now(timezone.utc)
		try:
			conn = open_jobs_db(self.db_path)
		except JobsDBError as e:
			if not self._warned:
				log.warning("cannot read Press agent jobs (%s); falling back to bench/site changes on disk", e)
				self._warned = True
			return None
		try:
			return self._poll(conn, now)
		except sqlite3.Error as e:
			log.warning("reading %s: %s", self.db_path, e)
			return []
		finally:
			conn.close()

	def _poll(self, conn, now):
		floor = self.state.get("floor")
		started, finished = set(self.state["started"]), set(self.state["finished"])
		if floor is None:
			floor = self._baseline(conn, started, finished)
		rows = self._rows(conn, floor)

		events, pending = [], []
		local_now = now.astimezone().replace(tzinfo=None)
		for row in rows:
			jid = row["id"]
			if jid in finished:
				continue
			terminal = row["status"] in TERMINAL
			start = _parse_dt(row.get("start")) or (_parse_dt(row.get("enqueue")) if terminal else None)
			if not terminal:
				seen_at = start or _parse_dt(row.get("enqueue"))
				if seen_at and local_now - seen_at > STALE_AFTER:
					# left Pending/Running by a crashed worker: stop waiting for it
					finished.add(jid)
					continue
			steps = self._steps(conn, jid)
			bench, sites = self._scope(row, steps)
			if jid not in started and start:
				if bench or terminal or local_now - start >= SCOPE_WAIT:
					events.append(self._deployment_event(row, bench, sites, start))
					started.add(jid)
			if terminal and jid in started:
				if row["status"] == "Failure":
					end = _parse_dt(row.get("end")) or start
					events.append(self._failure_event(row, bench, sites, end, steps))
				finished.add(jid)
			else:
				pending.append(jid)

		if pending:
			floor = min(pending) - 1
		elif rows:
			floor = rows[-1]["id"]
		self.state["floor"] = floor
		self.state["started"] = sorted(j for j in started if j > floor)
		self.state["finished"] = sorted(j for j in finished if j > floor)
		self._save_state()
		return events

	def _baseline(self, conn, started, finished):
		"""First run: skip history. Returns the floor; old finished jobs above it are marked done."""
		row = conn.execute("select max(id) from jobmodel").fetchone()
		max_id = row[0] if row and row[0] is not None else 0
		names = list(DEPLOY_JOBS)
		marks = ",".join("?" * len(names))
		open_ids = [
			r[0]
			for r in conn.execute(
				f"select id from jobmodel where name in ({marks}) and status not in ('Success', 'Failure')",
				names,
			).fetchall()
		]
		if not open_ids:
			return max_id
		floor = min(open_ids) - 1
		for (jid,) in conn.execute(
			f"select id from jobmodel where id > ? and name in ({marks}) and status in ('Success', 'Failure')",
			[floor, *names],
		).fetchall():
			finished.add(jid)
		return floor


class PressBenchWatcher:
	"""Fallback when jobs.sqlite3 is unreadable: deployments from the filesystem.

	A Press deploy creates a new bench directory and moves sites into it
	(Server.move_site in frappe/agent), so a new bench or a site that changed
	bench is reported as a `deployment`. Snapshot persisted in `state_path`.
	"""

	def __init__(self, host, state_path=None):
		self.host = host
		self.state_path = state_path
		self.snapshot = None
		if state_path:
			try:
				with open(state_path) as f:
					self.snapshot = json.load(f)
			except (OSError, ValueError):
				self.snapshot = None

	def poll(self, benches, now=None):
		ts = utc_iso(now)
		current = {"benches": sorted(b["name"] for b in benches), "sites": {}}
		for b in benches:
			for site in b.get("sites") or []:
				current["sites"][site] = b["name"]
		events = []
		if self.snapshot is not None:
			old_benches = set(self.snapshot.get("benches") or [])
			old_sites = self.snapshot.get("sites") or {}
			for name in current["benches"]:
				if name not in old_benches:
					events.append(self._event(ts, f"Press: new bench {name}", name, None, {"press_job": "New Bench"}))
			for site, bench in sorted(current["sites"].items()):
				before = old_sites.get(site)
				if before and before != bench:
					events.append(
						self._event(ts, f"Press: {site} moved to {bench}", bench, site, {"from_bench": before})
					)
		if current != self.snapshot:
			self.snapshot = current
			self._save()
		return events

	def _event(self, ts, title, bench, site, payload):
		payload = dict(payload, source="press", detected_by="filesystem")
		return event(
			"deployment",
			"press",
			"info",
			ts,
			self.host,
			title[:140],
			fingerprint("press_fs", self.host, bench, site, ts),
			payload,
			source="press-agent",
			bench=bench,
			site=site,
		)

	def _save(self):
		if not self.state_path:
			return
		tmp = self.state_path + ".tmp"
		try:
			os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
			with open(tmp, "w") as f:
				json.dump(self.snapshot, f)
			os.replace(tmp, self.state_path)
		except OSError as e:
			log.warning("cannot save Press bench snapshot %s: %s", self.state_path, e)
