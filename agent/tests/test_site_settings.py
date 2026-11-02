"""System Settings of each site, read from its database (site_settings.py)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import collector, site_settings  # noqa: E402

from test_discovery_config import make_bench  # noqa: E402
from test_infra import FakeRun  # noqa: E402

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
ROWS = "enable_two_factor_auth\t1\nminimum_password_score\t3\nallow_consecutive_login_attempts\tabc\nsystem_manager_2fa\t1\n"


def write_conf(path, site, conf):
	with open(os.path.join(path, "sites", site, "site_config.json"), "w") as f:
		json.dump(conf, f)


class TestSiteSettings(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.path = make_bench(self.tmp.name, sites=("a.example.com", "b.example.com", "pg.example.com", "bad.example.com"))
		os.makedirs(os.path.join(self.path, "sites", "pg.example.com"), exist_ok=True)
		write_conf(self.path, "a.example.com", {"db_name": "_a", "db_password": "pa"})
		write_conf(self.path, "b.example.com", {"db_name": "_b", "db_password": "pb"})
		write_conf(self.path, "pg.example.com", {"db_name": "_pg", "db_password": "p:g\\", "db_type": "postgres"})
		write_conf(self.path, "bad.example.com", {"db_name": "x`; drop", "db_password": "p"})
		self.bench = {"name": "main", "path": self.path, "sites": ["a.example.com", "b.example.com", "pg.example.com", "bad.example.com"]}

	def tearDown(self):
		self.tmp.cleanup()

	def test_site_target(self):
		self.assertEqual(site_settings.site_target(self.bench, "a.example.com"),
		                 {"type": "mariadb", "db": "_a", "user": "_a", "password": "pa", "host": "127.0.0.1", "port": 3306})  # fmt: skip
		self.assertEqual(site_settings.site_target(self.bench, "pg.example.com")["port"], 5432)
		self.assertIsNone(site_settings.site_target(self.bench, "bad.example.com"))  # not a plain name: never in SQL
		write_conf(self.path, "b.example.com", {"db_name": "_b"})
		self.assertIsNone(site_settings.site_target(self.bench, "b.example.com"))

	def test_to_samples(self):
		got = site_settings.to_samples(site_settings.parse_rows(ROWS))
		self.assertEqual(got, {"enable_two_factor_auth": 1.0, "enable_password_policy": 0, "minimum_password_score": 3.0,
		                       "allow_guests_to_upload_files": 0, "system_manager_2fa": 1.0})  # fmt: skip

	def run_fake(self, mariadb_answer):
		seen = {}

		def run(args, **kw):
			if args[0].endswith("psql"):
				with open(kw["env"]["PGPASSFILE"]) as f:
					seen["pgpass"] = (f.read(), oct(os.stat(kw["env"]["PGPASSFILE"]).st_mode & 0o777), kw["env"]["PGPASSFILE"])
				seen["psql"] = args
				return SimpleNamespace(returncode=0, stdout="enable_password_policy\t1\nsystem_manager_2fa\t0\n", stderr="")
			return fake(args, **kw)

		fake = FakeRun([(lambda a: True, mariadb_answer)])
		reader = site_settings.SiteSettingsReader("h1", {}, which=lambda n: "/usr/bin/" + n, run=run)
		return reader.collect([self.bench], NOW), fake, seen

	def test_reads_each_site(self):
		out, fake, seen = self.run_fake((0, ROWS, ""))
		by_site = {}
		for m in out:
			self.assertEqual((m["metric_name"], m["bench"], m["host"]), ("site_setting", "main", "h1"))
			by_site.setdefault(m["site"], {})[m["labels"]["setting"]] = m["value"]
		self.assertEqual(sorted(by_site), ["a.example.com", "b.example.com", "pg.example.com"])
		self.assertEqual(by_site["a.example.com"]["minimum_password_score"], 3.0)
		self.assertEqual(by_site["pg.example.com"], {"enable_two_factor_auth": 0, "enable_password_policy": 1.0,
		                                             "minimum_password_score": 0, "allow_consecutive_login_attempts": 0,
		                                             "allow_guests_to_upload_files": 0, "system_manager_2fa": 0.0})  # fmt: skip
		# MariaDB: each site's own credentials in an option file, its database named in the query
		self.assertEqual(len(fake.calls), 2)
		self.assertIn('user="_a"', fake.option_files[0][2])
		self.assertIn("FROM `_a`.`tabSingles`", fake.calls[0][-1])
		self.assertFalse(any("pa" == a for call in fake.calls for a in call))
		# Postgres: the password in a private pgpass file, escaped, removed afterwards
		content, mode, path = seen["pgpass"]
		self.assertEqual((content, mode), ("127.0.0.1:5432:_pg:_pg:p\\:g\\\\\n", "0o600"))
		self.assertFalse(os.path.exists(path))
		self.assertNotIn("p:g\\", seen["psql"])
		self.assertIn("-w", seen["psql"])

	def test_an_unreachable_server_is_asked_once(self):
		with self.assertLogs("wolf_agent.site_settings", "WARNING"):
			out, fake, _ = self.run_fake((1, "", "ERROR 2002 (HY000): Can't connect to server on '127.0.0.1'"))
		self.assertEqual(len(fake.calls), 1)  # b.example.com shares the server: skipped this round
		self.assertEqual({m["site"] for m in out}, {"pg.example.com"})
		with self.assertLogs("wolf_agent.site_settings", "WARNING"):
			out, fake, _ = self.run_fake((1, "", "ERROR 1049 (42000): Unknown database '_a'"))
		self.assertEqual(len(fake.calls), 2)  # one site's problem: the others are still read
		with self.assertLogs("wolf_agent.site_settings", "WARNING"):
			out, fake, _ = self.run_fake(subprocess.TimeoutExpired("mariadb", 10))
		self.assertEqual(len(fake.calls), 1)

	def test_without_a_client(self):
		reader = site_settings.SiteSettingsReader("h1", {}, which=lambda n: None, run=FakeRun([]))
		with self.assertLogs("wolf_agent.site_settings", "INFO") as logs:
			self.assertEqual(reader.collect([self.bench], NOW), [])
			reader.collect([self.bench], NOW)
		self.assertEqual(len(logs.records), 2)  # once per database kind, not per round

	def test_infra_interval(self):
		ticks = [0.0]
		cfg = {"mariadb": False, "processes": False, "backups": False, "tls": False, "exposure": False, "versions": False}
		infra = collector.InfraChecks({**cfg, "site_settings": {"interval": 3600}}, "h1", clock=lambda: ticks[0])
		calls = []
		infra.settings_reader = SimpleNamespace(collect=lambda benches, now: calls.append(now) or [{"metric_name": "site_setting"}])
		self.assertEqual(len(infra.collect([self.bench], NOW)[0]), 1)
		ticks[0] = 1800
		self.assertEqual(infra.collect([self.bench], NOW)[0], [])
		self.assertEqual(len(calls), 1)
		off = collector.InfraChecks({**cfg, "site_settings": False}, "h1", clock=lambda: ticks[0])
		self.assertEqual(off.collect([self.bench], NOW)[0], [])


if __name__ == "__main__":
	unittest.main()
