"""MariaDB health through the `mariadb` / `mysql` command-line client.

The stdlib has no MySQL protocol client, so the agent runs the CLI that every Frappe host
already has. Credentials come from the bench itself (a site's `db_name` / `db_password`,
`db_host` / `db_port` from common_site_config.json): `SHOW GLOBAL STATUS` and
`SHOW GLOBAL VARIABLES` work for any user. Replication status needs the REPLICATION CLIENT
(or SLAVE MONITOR) privilege, which a site user usually lacks; it is skipped then, unless
the `mariadb` config section names a user that has it. Postgres benches are skipped.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile

from .util import metric, utc_iso

log = logging.getLogger(__name__)

STATUS_QUERY = (
	"SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected', 'Slow_queries');"
	" SHOW GLOBAL VARIABLES WHERE Variable_name = 'max_connections';"
)
REPLICA_QUERY = "SHOW SLAVE STATUS"
CLIENTS = ("mariadb", "mysql")


def _load_json(path):
	try:
		with open(path) as f:
			data = json.load(f)
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def bench_target(bench):
	"""Connection settings for a bench's database server, from a readable site config."""
	sites_dir = os.path.join(bench["path"], "sites")
	common = _load_json(os.path.join(sites_dir, "common_site_config.json"))
	for site in bench.get("sites") or []:
		conf = dict(common)
		conf.update(_load_json(os.path.join(sites_dir, site, "site_config.json")))
		if (conf.get("db_type") or "mariadb") != "mariadb":
			return None
		if conf.get("db_name") and conf.get("db_password"):
			return {
				"host": conf.get("db_host") or "127.0.0.1",
				"port": int(conf.get("db_port") or 3306),
				"user": conf.get("db_user") or conf["db_name"],
				"password": conf["db_password"],
			}
	return None


def parse_rows(text):
	"""Tab-separated `name\\tvalue` lines (`-B -N`) → {name: value}."""
	out = {}
	for line in (text or "").splitlines():
		name, sep, value = line.partition("\t")
		if sep:
			out[name.strip()] = value.strip()
	return out


def parse_vertical(text):
	"""`-E` (vertical) output → list of {column: value} rows."""
	rows, row = [], None
	for line in (text or "").splitlines():
		if line.startswith("*****"):
			row = {}
			rows.append(row)
			continue
		key, sep, value = line.partition(":")
		if sep and row is not None:
			row[key.strip()] = value.strip()
	return rows


def _int(value):
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _option(value):
	return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class MariaDBProbe:
	"""One database server: threads, connection use, slow queries, replication."""

	def __init__(self, target, host, bench=None, cli=None, run=subprocess.run, timeout=10):
		self.target = target
		self.host = host
		self.bench = bench
		self.cli = cli
		self.run = run
		self.timeout = timeout
		self.prev_slow = None
		self.replica_allowed = True
		self.label = f"{target.get('host') or 'localhost'}:{target.get('port') or 3306}"

	def _query(self, sql, vertical=False):
		fd, path = None, None
		args = [self.cli]
		if self.target.get("defaults_file"):
			args.append("--defaults-extra-file=" + self.target["defaults_file"])
		else:
			# the password goes through a private option file, never the command line
			fd, path = tempfile.mkstemp(prefix="wolf-db-", suffix=".cnf")
			with os.fdopen(fd, "w") as f:
				f.write("[client]\n")
				if self.target.get("user"):
					f.write(f"user={_option(self.target['user'])}\n")
				if self.target.get("password"):
					f.write(f"password={_option(self.target['password'])}\n")
			args.append("--defaults-extra-file=" + path)
		args += ["--connect-timeout=3", "-B", "-E" if vertical else "-N"]
		if self.target.get("host"):
			args += ["-h", str(self.target["host"])]
		if self.target.get("port"):
			args += ["-P", str(self.target["port"])]
		args += ["-e", sql]
		try:
			proc = self.run(args, capture_output=True, text=True, timeout=self.timeout)
		finally:
			if path:
				os.unlink(path)
		if proc.returncode != 0:
			raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:300] or f"exit {proc.returncode}")
		return proc.stdout

	def collect(self, now=None):
		ts = utc_iso(now)
		out = []
		labels = {"db": self.label}

		def add(name, value):
			out.append(metric(name, value, ts, self.host, bench=self.bench, labels=labels))

		values = parse_rows(self._query(STATUS_QUERY))
		threads = _int(values.get("Threads_connected"))
		max_conn = _int(values.get("max_connections"))
		if threads is not None:
			add("db_threads_connected", threads)
			if max_conn:
				add("db_connections_used_percent", round(100.0 * threads / max_conn, 2))
		slow = _int(values.get("Slow_queries"))
		if slow is not None:
			if self.prev_slow is not None:
				add("db_slow_queries", slow - self.prev_slow if slow >= self.prev_slow else slow)
			self.prev_slow = slow

		if self.replica_allowed:
			try:
				rows = parse_vertical(self._query(REPLICA_QUERY, vertical=True))
			except RuntimeError as e:
				if "denied" in str(e).lower() or "privilege" in str(e).lower():
					log.info("replication status of %s needs REPLICATION CLIENT; skipped (%s)", self.label, e)
					self.replica_allowed = False
					return out
				raise
			for row in rows[:1]:  # not a replica → no row, no metric
				running = row.get("Slave_IO_Running") == "Yes" and row.get("Slave_SQL_Running") == "Yes"
				add("db_replication_running", 1 if running else 0)
				lag = _int(row.get("Seconds_Behind_Master"))
				if lag is not None:
					add("db_replication_lag_seconds", lag)
		return out


def find_cli(configured=None, which=shutil.which):
	if configured:
		return configured if os.path.isabs(configured) else which(configured)
	for name in CLIENTS:
		path = which(name)
		if path:
			return path
	return None


def build_probes(benches, host, settings, which=shutil.which, run=subprocess.run):
	"""One probe per distinct database server. Returns [] (with a log line) when impossible."""
	cli = find_cli(settings.get("cli"), which=which)
	if not cli:
		log.info("no mariadb/mysql client on this host; database health is not collected")
		return []
	explicit = {k: settings.get(k) for k in ("host", "port", "user", "password", "defaults_file") if settings.get(k)}
	if explicit.get("user") or explicit.get("defaults_file"):
		return [MariaDBProbe(explicit, host, cli=cli, run=run)]
	by_server = {}
	for bench in benches:
		target = bench_target(bench)
		if target:
			by_server.setdefault((target["host"], target["port"]), (target, []))[1].append(bench["name"])
	probes = []
	for target, names in by_server.values():
		probes.append(MariaDBProbe(target, host, bench=names[0] if len(names) == 1 else None, cli=cli, run=run))
	return probes
