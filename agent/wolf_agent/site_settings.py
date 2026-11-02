"""System Settings of each site, read from its database, for the Wolf site's posture score.

The Wolf site can read its own System Settings; for every other site the agent reads them with
the site's own credentials (site_config.json, as Frappe does): five rows of `tabSingles` and the
System Manager role's two-factor flag, nothing else. MariaDB through the `mariadb` / `mysql`
client, Postgres through `psql`; the password goes through a private file, never the command
line or the environment.

One `site_setting` sample per site and setting (`labels.setting`), every `site_settings.interval`
(1 h). A setting never saved has no row and is sent as 0, as Frappe reads it; a value that is not
a number is not sent. When a database server does not answer, its other sites are skipped until
the next round.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

from .database import MariaDBProbe, find_cli, parse_rows
from .util import metric, utc_iso

log = logging.getLogger(__name__)

FIELDS = ("enable_two_factor_auth", "enable_password_policy", "minimum_password_score",
          "allow_consecutive_login_attempts", "allow_guests_to_upload_files")  # fmt: skip
ROLE_2FA = "system_manager_2fa"
UNREACHABLE = ("can't connect", "could not connect", "connection refused", "timed out", "timeout expired")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_IN = ", ".join(f"'{f}'" for f in FIELDS)
MARIADB_SQL = (
	"SELECT field, value FROM `{db}`.`tabSingles` WHERE doctype = 'System Settings' AND field IN ({fields})"
	" UNION ALL SELECT '{role}', CAST(two_factor_auth AS CHAR) FROM `{db}`.`tabRole` WHERE name = 'System Manager'"
)
POSTGRES_SQL = (
	"SELECT field, value FROM \"tabSingles\" WHERE doctype = 'System Settings' AND field IN ({fields})"
	" UNION ALL SELECT '{role}', two_factor_auth::text FROM \"tabRole\" WHERE name = 'System Manager'"
)


def _load_json(path):
	try:
		with open(path) as f:
			data = json.load(f)
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def site_target(bench, site):
	"""Connection settings of a site's own database; None when its config lacks them."""
	sites_dir = os.path.join(bench["path"], "sites")
	conf = _load_json(os.path.join(sites_dir, "common_site_config.json"))
	conf.update(_load_json(os.path.join(sites_dir, site, "site_config.json")))
	db, password = conf.get("db_name"), conf.get("db_password")
	if not db or not password or not _SAFE_NAME.match(str(db)):
		return None
	kind = conf.get("db_type") or "mariadb"
	if kind not in ("mariadb", "postgres"):
		return None
	return {
		"type": kind, "db": str(db), "user": str(conf.get("db_user") or db), "password": str(password),
		"host": conf.get("db_host") or "127.0.0.1", "port": int(conf.get("db_port") or (5432 if kind == "postgres" else 3306)),
	}  # fmt: skip


def _pgpass_field(value):
	return str(value).replace("\\", "\\\\").replace(":", "\\:")


def query_postgres(cli, target, sql, run=subprocess.run, timeout=10):
	fd, path = tempfile.mkstemp(prefix="wolf-pg-", suffix=".pgpass")
	with os.fdopen(fd, "w") as f:
		f.write(":".join(_pgpass_field(target[k]) for k in ("host", "port", "db", "user", "password")) + "\n")
	env = {**os.environ, "PGPASSFILE": path, "PGCONNECT_TIMEOUT": "3"}
	env.pop("PGPASSWORD", None)
	args = [cli, "-X", "-A", "-t", "-F", "\t", "-h", str(target["host"]), "-p", str(target["port"]),
	        "-U", target["user"], "-d", target["db"], "-w", "-c", sql]  # fmt: skip
	try:
		proc = run(args, capture_output=True, text=True, timeout=timeout, env=env)
	finally:
		os.unlink(path)
	if proc.returncode != 0:
		raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:300] or f"exit {proc.returncode}")
	return proc.stdout


def read_settings(target, clis, run=subprocess.run):
	"""{setting: value text} of one site."""
	if target["type"] == "postgres":
		sql = POSTGRES_SQL.format(fields=_IN, role=ROLE_2FA)
		return parse_rows(query_postgres(clis["postgres"], target, sql, run=run))
	probe = MariaDBProbe({k: target[k] for k in ("host", "port", "user", "password")}, None, cli=clis["mariadb"], run=run)
	return parse_rows(probe._query(MARIADB_SQL.format(db=target["db"], fields=_IN, role=ROLE_2FA)))


def to_samples(values):
	"""{setting: number}: absent rows are 0 (as Frappe reads them), non-numbers are dropped."""
	out = {}
	for name in (*FIELDS, ROLE_2FA):
		raw = values.get(name)
		if raw in (None, "", "NULL", "\\N"):
			out[name] = 0
			continue
		try:
			out[name] = float(raw)
		except ValueError:
			log.debug("System Settings %s=%r is not a number; not sent", name, raw)
	return out


class SiteSettingsReader:
	def __init__(self, host, settings=None, which=shutil.which, run=subprocess.run):
		settings = settings or {}
		self.host = host
		self.run = run
		self.clis = {"mariadb": find_cli(settings.get("cli"), which=which), "postgres": which("psql")}
		self.warned = set()

	def collect(self, benches, now=None):
		ts = utc_iso(now)
		out, down = [], set()
		for bench in benches:
			for site in bench.get("sites") or []:
				target = site_target(bench, site)
				if not target:
					continue
				server = (target["type"], target["host"], target["port"])
				if server in down:
					continue
				if not self.clis[target["type"]]:
					if target["type"] not in self.warned:
						log.info("no %s client on this host; System Settings of %s sites are not read", target["type"],
						         target["type"])  # fmt: skip
						self.warned.add(target["type"])
					continue
				try:
					values = read_settings(target, self.clis, run=self.run)
				except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
					log.warning("System Settings of %s: %s", site, e)
					if not isinstance(e, RuntimeError) or any(m in str(e).lower() for m in UNREACHABLE):
						down.add(server)  # unreachable: do not wait on it for every site
					continue
				for name, value in to_samples(values).items():
					out.append(metric("site_setting", value, ts, self.host, bench=bench["name"], site=site,
					                  labels={"setting": name}))  # fmt: skip
		return out
