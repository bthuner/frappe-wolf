"""Assessment of Wolf's own heartbeat (docs/protocol.md §4).

Kept free of frappe imports so it can be unit tested without a bench. The agent
watchdog (agent/wolf_agent/watchdog.py) mirrors `assess`; keep the two in sync.
"""

from datetime import timezone
from zoneinfo import ZoneInfo

STALE_MINUTES = 5


def to_utc_iso(dt, tz_name):
	"""Naive datetime in the site time zone → `YYYY-MM-DDTHH:MM:SSZ`, or None."""
	if dt is None:
		return None
	return dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_seconds(then, now):
	"""Signed age in seconds: negative when `then` lies in the future."""
	if then is None:
		return None
	return int((now - then).total_seconds())


def _context(health):
	scheduler = health.get("scheduler") or {}
	queues = health.get("queues") or {}
	notes = []
	if scheduler.get("enabled") is False:
		notes.append(f"scheduler disabled ({scheduler.get('reason') or 'unknown reason'})")
	if queues.get("error"):
		notes.append(f"job queue unreachable: {queues['error']}")
	elif queues.get("workers") == 0:
		notes.append("no background worker running")
	return notes


def assess(health, stale_minutes=STALE_MINUTES):
	"""Return the problems found in a health dict, as short sentences (empty = healthy).

	Only staleness raises a problem: a paused scheduler or a Redis blip during a
	deployment is normal for a minute or two, and shows up as staleness if it lasts.
	"""
	if not isinstance(health, dict):
		return []
	limit = max(float(stale_minutes or STALE_MINUTES), 0.1) * 60
	rules_age = (health.get("rules") or {}).get("age_seconds")
	job_age = (health.get("scheduler") or {}).get("age_seconds")
	what, age = ("alert rules", rules_age) if rules_age is not None else ("scheduled jobs", job_age)
	if age is None:
		return []

	if age >= limit:
		problem = f"{what} not evaluated for {int(age // 60)} min"
	elif age <= -limit:
		# A time-zone change leaves stored times in the future: the scheduler then
		# considers nothing due until that time is reached.
		problem = f"last run of {what} is {int(-age // 60)} min in the future (time zone changed?)"
	else:
		return []
	notes = _context(health)
	return [problem + (f"; {', '.join(notes)}" if notes else "")]
