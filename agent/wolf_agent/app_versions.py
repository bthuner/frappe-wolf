"""Versions of the apps in each bench, for the Wolf site to compare with security advisories.

Each app's `apps/<app>/<app>/__init__.py` carries `__version__ = "15.100.0"`; the code on disk
is what every site of the bench runs, installed on it or not. One `app_version` sample per
bench and app, labelled `app`, its value the version as a number (major * 1e6 + minor * 1e3
+ patch: 15.100.0 is 15100000), so a chart shows upgrades. Development versions
("16.0.0-dev", "15.x.x-develop") have no release to compare and send nothing.
"""

import logging
import os
import re

from .util import metric, utc_iso

log = logging.getLogger(__name__)

_ASSIGN = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
_RELEASE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)\s*$")
MAX_INIT_BYTES = 64 * 1024


def read_version(app_dir, app):
	"""`__version__` of the app's package, as written; None when there is none."""
	try:
		with open(os.path.join(app_dir, app, "__init__.py"), encoding="utf-8", errors="replace") as f:
			text = f.read(MAX_INIT_BYTES)
	except OSError:
		return None
	m = _ASSIGN.search(text)
	return m.group(1) if m else None


def encode(version):
	"""15100000 for "15.100.0"; None for anything that is not a release version."""
	m = _RELEASE.match(version or "")
	if not m:
		return None
	major, minor, patch = (int(p) for p in m.groups())
	if minor >= 1000 or patch >= 1000:
		return None
	return major * 1_000_000 + minor * 1_000 + patch


def bench_apps(bench_path):
	"""{app: version string} of the apps in `apps/` that have a `__version__`."""
	apps_dir = os.path.join(bench_path, "apps")
	try:
		names = sorted(os.listdir(apps_dir))
	except OSError:
		return {}
	out = {}
	for app in names:
		version = read_version(os.path.join(apps_dir, app), app)
		if version:
			out[app] = version
	return out


def version_metrics(benches, host, now=None):
	ts = utc_iso(now)
	out = []
	for bench in benches:
		for app, version in bench_apps(bench["path"]).items():
			value = encode(version)
			if value is None:
				log.debug("bench %s: %s %s is not a release version, not sent", bench["name"], app, version)
				continue
			out.append(metric("app_version", value, ts, host, bench=bench["name"], labels={"app": app}))
	return out
