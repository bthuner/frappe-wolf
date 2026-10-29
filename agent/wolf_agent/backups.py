"""Age of each site's newest database backup, read from the bench's backup directories.

`bench --site <site> backup` (and the scheduled backups Frappe runs) write
`sites/<site>/private/backups/<YYYYMMDD_HHMMSS>-<site>-database.sql.gz`. Wolf takes the
newest database dump's mtime; files, private files and site config archives are ignored.
"""

import glob
import os
import time

from .util import metric, utc_iso

DATABASE_PATTERNS = ("*-database.sql*", "*-database-enc.sql*")


def backup_dir(bench_path, site):
	return os.path.join(bench_path, "sites", site, "private", "backups")


def newest_backup(directory):
	"""`(path, mtime)` of the newest database dump in `directory`, or None."""
	newest = None
	for pattern in DATABASE_PATTERNS:
		for path in glob.glob(os.path.join(directory, pattern)):
			try:
				mtime = os.stat(path).st_mtime
			except OSError:
				continue
			if newest is None or mtime > newest[1]:
				newest = (path, mtime)
	return newest


def backup_metrics(benches, host, now=None, wall=time.time, overrides=None):
	"""`backup_age_hours` per site that has at least one database backup.

	`overrides` maps a site to another backup directory (offsite mounts, custom scripts).
	Sites that never had a backup report nothing (see agent/README.md).
	"""
	ts = utc_iso(now)
	current = wall()
	overrides = overrides or {}
	out = []
	for bench in benches:
		for site in bench.get("sites") or []:
			found = newest_backup(overrides.get(site) or backup_dir(bench["path"], site))
			if not found:
				continue
			age = max(current - found[1], 0) / 3600.0
			out.append(
				metric("backup_age_hours", round(age, 2), ts, host, bench=bench["name"], site=site)
			)
	return out
