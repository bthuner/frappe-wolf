import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wolf_agent import config, discovery  # noqa: E402


def make_bench(root, name="frappe-bench", sites=("a.example.com", "b.example.com"), redis="redis://127.0.0.1:11000"):
	path = os.path.join(root, name)
	os.makedirs(os.path.join(path, "apps", "frappe"))
	os.makedirs(os.path.join(path, "logs"))
	os.makedirs(os.path.join(path, "sites", "assets"))  # not a site: no site_config.json
	for site in sites:
		os.makedirs(os.path.join(path, "sites", site))
		with open(os.path.join(path, "sites", site, "site_config.json"), "w") as f:
			json.dump({"db_name": "x"}, f)
	with open(os.path.join(path, "sites", "common_site_config.json"), "w") as f:
		json.dump({"redis_queue": redis} if redis else {}, f)
	return path


class TestDiscovery(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.root = self.tmp.name

	def tearDown(self):
		self.tmp.cleanup()

	def test_plain_bench(self):
		path = make_bench(self.root)
		benches = discovery.discover_benches([path], nginx_default=None)
		self.assertEqual(len(benches), 1)
		b = benches[0]
		self.assertEqual(b["name"], "frappe-bench")
		self.assertEqual(b["sites"], ["a.example.com", "b.example.com"])
		self.assertEqual(b["logs_dir"], os.path.join(path, "logs"))
		self.assertEqual(b["redis_queue"], "redis://127.0.0.1:11000")
		self.assertIsNone(b["nginx_access_log"])
		self.assertFalse(b["bench_toml"])

	def test_glob_search_and_non_bench_ignored(self):
		make_bench(os.path.join(self.root, "home", "frappe"))
		os.makedirs(os.path.join(self.root, "home", "frappe", "not-a-bench"))
		globs = (os.path.join(self.root, "home", "*", "frappe-bench"), os.path.join(self.root, "home", "frappe", "*"))
		benches = discovery.discover_benches(search_globs=globs, nginx_default=None)
		self.assertEqual([b["name"] for b in benches], ["frappe-bench"])  # deduplicated across globs

	def test_nginx_default_when_present(self):
		path = make_bench(self.root)
		nginx_log = os.path.join(self.root, "access.log")
		open(nginx_log, "w").close()
		self.assertEqual(discovery.describe_bench(path, nginx_default=nginx_log)["nginx_access_log"], nginx_log)

	@unittest.skipIf(discovery._toml is None, "no TOML parser available")
	def test_bench_toml(self):
		path = make_bench(self.root, sites=("a.example.com",), redis=None)
		with open(os.path.join(path, "bench.toml"), "w") as f:
			f.write(
				'[bench]\nname = "production"\nlogs_dir = "var/logs"\n\n'
				'[nginx]\naccess_log = "/var/log/nginx/prod.access.log"\n\n'
				'[redis.queue]\nhost = "10.0.0.2"\nport = 11000\n\n'
				'[[sites]]\nname = "c.example.com"\n'
			)
		b = discovery.describe_bench(path, nginx_default=None)
		self.assertEqual(b["name"], "production")
		self.assertEqual(b["logs_dir"], os.path.join(path, "var", "logs"))
		self.assertEqual(b["nginx_access_log"], "/var/log/nginx/prod.access.log")
		self.assertEqual(b["redis_queue"], "redis://10.0.0.2:11000")
		self.assertEqual(b["sites"], ["c.example.com", "a.example.com"])
		self.assertTrue(b["bench_toml"])

	def test_bench_toml_only_dir_counts_as_bench(self):
		path = os.path.join(self.root, "pilot-bench")
		os.makedirs(path)
		with open(os.path.join(path, "bench.toml"), "w") as f:
			f.write('name = "pilot"\nsites = ["x.example.com"]\n')
		benches = discovery.discover_benches([path], nginx_default=None)
		self.assertEqual(len(benches), 1)
		if discovery._toml is not None:
			self.assertEqual(benches[0]["name"], "pilot")
			self.assertEqual(benches[0]["sites"], ["x.example.com"])

	def test_broken_bench_toml_falls_back(self):
		path = make_bench(self.root, sites=("a.example.com",))
		with open(os.path.join(path, "bench.toml"), "w") as f:
			f.write("this is = = not toml")
		b = discovery.describe_bench(path, nginx_default=None)
		self.assertEqual(b["name"], "frappe-bench")
		self.assertEqual(b["sites"], ["a.example.com"])

	def test_no_toml_parser(self):
		path = make_bench(self.root, sites=("a.example.com",))
		with open(os.path.join(path, "bench.toml"), "w") as f:
			f.write('name = "ignored"\n')
		with mock.patch.object(discovery, "_toml", None):
			self.assertEqual(discovery.describe_bench(path, nginx_default=None)["name"], "frappe-bench")


class TestConfig(unittest.TestCase):
	def test_save_is_private_and_load_merges_defaults(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "etc", "config.json")
			config.save_config(path, {"url": "https://wolf", "agent_id": "WAG-0001", "agent_secret": "s"})
			self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
			cfg = config.load_config(path)
			self.assertEqual(cfg["agent_id"], "WAG-0001")
			self.assertEqual(cfg["push_interval"], 30)
			self.assertTrue(cfg["hostname"])

	def test_config_path_precedence(self):
		with mock.patch.dict(os.environ, {"WOLF_AGENT_CONFIG": "/env.json"}):
			self.assertEqual(config.config_path("/cli.json"), "/cli.json")
			self.assertEqual(config.config_path(None), "/env.json")
		with mock.patch.dict(os.environ, {}, clear=True):
			self.assertEqual(config.config_path(None), config.DEFAULT_CONFIG_PATH)

	def test_resolve_auth_log(self):
		self.assertIsNone(config.resolve_auth_log(False, True))
		self.assertEqual(config.resolve_auth_log("/x/auth.log", True), "/x/auth.log")
		with mock.patch.object(config, "AUTH_LOG_CANDIDATES", ("/nonexistent/auth.log",)):
			self.assertEqual(config.resolve_auth_log(None, True), "journal")
			self.assertIsNone(config.resolve_auth_log(None, False))


if __name__ == "__main__":
	unittest.main()
