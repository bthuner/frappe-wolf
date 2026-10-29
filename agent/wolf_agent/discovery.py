"""Bench discovery from the filesystem, Pilot's bench.toml, or a Press server's layout.

A bench.toml written by Frappe Pilot (github.com/frappe/pilot, `[bench] name`)
is read with its real schema (see pilot.py and docs/pilot.md). Other bench.toml
files are still read defensively from a few plausible key locations; anything
missing falls back to what the bench directory itself shows (sites/, logs/,
common_site_config.json).
"""

import glob
import json
import logging
import os

from . import pilot, press

log = logging.getLogger(__name__)

DEFAULT_SEARCH_GLOBS = ("/home/*/frappe-bench", "/home/frappe/*") + pilot.PILOT_SEARCH_GLOBS
DEFAULT_NGINX_ACCESS_LOG = "/var/log/nginx/access.log"

try:
	import tomllib as _toml
except ImportError:  # Python < 3.11
	try:
		import tomli as _toml
	except ImportError:
		_toml = None


def is_bench(path):
	if os.path.isfile(os.path.join(path, "bench.toml")):
		return True
	return os.path.isdir(os.path.join(path, "sites")) and os.path.isdir(os.path.join(path, "apps", "frappe"))


def read_bench_toml(path):
	toml_path = os.path.join(path, "bench.toml")
	if not os.path.isfile(toml_path):
		return {}
	if _toml is None:
		log.warning("found %s but no TOML parser (Python < 3.11 without tomli); ignoring it", toml_path)
		return {}
	try:
		with open(toml_path, "rb") as f:
			return _toml.load(f)
	except (OSError, ValueError) as e:
		log.warning("cannot parse %s: %s", toml_path, e)
		return {}


def _dig(data, *paths):
	"""First non-empty value found at any of the dotted `paths`."""
	for path in paths:
		node = data
		for key in path.split("."):
			if not isinstance(node, dict) or key not in node:
				node = None
				break
			node = node[key]
		if node not in (None, "", [], {}):
			return node
	return None


def _toml_sites(data):
	raw = _dig(data, "sites", "bench.sites")
	if isinstance(raw, dict):
		return list(raw)
	sites = []
	for item in raw or []:
		if isinstance(item, str):
			sites.append(item)
		elif isinstance(item, dict) and (item.get("name") or item.get("domain")):
			sites.append(item.get("name") or item.get("domain"))
	return sites


def _fs_sites(path):
	sites_dir = os.path.join(path, "sites")
	try:
		names = sorted(os.listdir(sites_dir))
	except OSError:
		return []
	return [n for n in names if os.path.isfile(os.path.join(sites_dir, n, "site_config.json"))]


def _common_site_config(path):
	try:
		with open(os.path.join(path, "sites", "common_site_config.json")) as f:
			return json.load(f)
	except (OSError, ValueError):
		return {}


def _resolve(base, value):
	return value if os.path.isabs(value) else os.path.join(base, value)


def describe_bench(path, nginx_default=DEFAULT_NGINX_ACCESS_LOG):
	path = os.path.abspath(path)
	toml = read_bench_toml(path)
	common = _common_site_config(path)

	sites = _toml_sites(toml)
	for site in _fs_sites(path):
		if site not in sites:
			sites.append(site)

	logs_dir = _dig(toml, "bench.logs_dir", "logs.dir", "logs.path", "paths.logs")
	nginx = _dig(toml, "nginx.access_log", "nginx.access_log_path", "logs.nginx_access", "nginx.log")
	if not nginx and nginx_default and os.path.exists(nginx_default):
		nginx = nginx_default

	redis_queue = common.get("redis_queue") or _dig(toml, "redis.queue", "redis.queue_url", "redis.redis_queue")
	if isinstance(redis_queue, dict):  # e.g. [redis.queue] host/port table
		host = redis_queue.get("host", "127.0.0.1")
		port = redis_queue.get("port", 11000)
		redis_queue = f"redis://{host}:{port}"

	desc = {
		"name": str(_dig(toml, "bench.name", "name") or os.path.basename(path)),
		"path": path,
		"sites": sites,
		"logs_dir": _resolve(path, logs_dir) if logs_dir else os.path.join(path, "logs"),
		"nginx_access_log": nginx,
		"redis_queue": redis_queue,
		"bench_toml": bool(toml),
	}
	if pilot.is_pilot_toml(toml):
		# Pilot has no [nginx]/logs keys: its layout is fixed (logs/, logs/nginx-access.log)
		extra = pilot.describe(path, toml)
		for key, explicit in (("redis_queue", redis_queue), ("logs_dir", logs_dir), ("nginx_access_log", _dig(toml, "nginx.access_log"))):
			if explicit:
				extra.pop(key, None)
		desc.update(extra)
		desc["site_domains"] = pilot.site_domains(path, sites)
	return desc


def resolve_platform(platform=None, press_agent_dir=press.DEFAULT_AGENT_DIR):
	"""`press` when configured so or when the Press agent's config.json is present, else `generic`."""
	if platform in ("press", "generic"):
		return platform
	return "press" if press.detect(press_agent_dir) else "generic"


def discover_benches(
	paths=None,
	search_globs=DEFAULT_SEARCH_GLOBS,
	nginx_default=DEFAULT_NGINX_ACCESS_LOG,
	platform="generic",
	press_agent_dir=press.DEFAULT_AGENT_DIR,
):
	"""Benches on this host. `platform` is `generic`, `press`, or None to auto-detect."""
	platform = resolve_platform(platform, press_agent_dir)
	benches, seen = [], set()
	if platform == "press" and not paths:
		for bench in press.discover_press_benches(press_agent_dir, nginx_access_log=nginx_default):
			seen.add(os.path.realpath(bench["path"]))
			benches.append(bench)
	if paths:
		candidates = list(paths)
	else:
		candidates = []
		for pattern in search_globs:
			candidates += sorted(glob.glob(pattern))
	for candidate in candidates:
		real = os.path.realpath(candidate)
		if real in seen or not os.path.isdir(real):
			continue
		if is_bench(real):
			seen.add(real)
			benches.append(describe_bench(real, nginx_default=nginx_default))
		elif press.is_press_bench(real):
			seen.add(real)
			benches.append(press.describe_press_bench(real, nginx_access_log=nginx_default))
		elif paths:
			log.warning("%s does not look like a bench (no bench.toml, sites/ or apps/frappe)", candidate)
	return benches
