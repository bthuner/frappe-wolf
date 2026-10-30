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

	Only lasting conditions raise a problem: a paused scheduler, a Redis blip or a burst of
	pushes during a deployment is normal for a minute or two.
	"""
	if not isinstance(health, dict):
		return []
	return [p for p in (staleness(health, stale_minutes), ingest_backlog(health, stale_minutes)) if p]


def _limit(stale_minutes):
	return max(float(stale_minutes or STALE_MINUTES), 0.1) * 60


def staleness(health, stale_minutes=STALE_MINUTES):
	"""Rules (or scheduled jobs) not run for `stale_minutes`, with context; None if fresh."""
	if not isinstance(health, dict):
		return None
	limit = _limit(stale_minutes)
	rules_age = (health.get("rules") or {}).get("age_seconds")
	job_age = (health.get("scheduler") or {}).get("age_seconds")
	what, age = ("alert rules", rules_age) if rules_age is not None else ("scheduled jobs", job_age)
	if age is None:
		return None

	if age >= limit:
		problem = f"{what} not evaluated for {int(age // 60)} min"
	elif age <= -limit:
		# A time-zone change leaves stored times in the future: the scheduler then
		# considers nothing due until that time is reached.
		problem = f"last run of {what} is {int(-age // 60)} min in the future (time zone changed?)"
	else:
		return None
	notes = _context(health)
	return problem + (f"; {', '.join(notes)}" if notes else "")


def ingest_backlog(health, stale_minutes=STALE_MINUTES):
	"""Queued pushes waiting `stale_minutes` for a worker; None otherwise.

	Agents get their answer before a queued push is stored, so their heartbeat stays fresh
	while the telemetry waits in Redis, and rules see none of it.
	"""
	if not isinstance(health, dict):
		return None
	queues = health.get("queues") or {}
	age = queues.get("ingest_backlog_seconds")
	if not isinstance(age, (int, float)) or age < _limit(stale_minutes):
		return None
	problem = f"pushes waiting {int(age // 60)} min in the {queues.get('ingest_queue') or 'ingest'} queue, not stored yet"
	if queues.get("workers") == 0:
		problem += "; no background worker running"
	return problem
