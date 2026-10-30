"""Wolf's own health, returned to agents with every push (docs/protocol.md §4).

A site cannot alert about its own dead scheduler: the rules that would fire run on
that scheduler. So the site only reports facts here, and the agent watchdog, which
does not depend on the site, raises the live alert through its fallback channel.
"""

from datetime import datetime, timezone

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from frappe_wolf import __version__
from frappe_wolf.wolf import events, heartbeat
from frappe_wolf.wolf.schema import fingerprint

# Queued pushes are stored by a worker of this queue (frappe_wolf.api.ingest).
INGEST_QUEUE = "short"

# Same as the "Scheduler stalled" preset window: while stalled, one event is bumped.
STALL_DEDUPE_MINUTES = 15


def _max(sql):
	value = frappe.db.sql(sql)[0][0]
	return get_datetime(value) if value else None


def scheduler_inactive_reason():
	"""Why the scheduler skips this site, or None (mirrors frappe.utils.scheduler.is_scheduler_inactive)."""
	if frappe.local.conf.get("maintenance_mode"):
		return "maintenance_mode"
	if frappe.local.conf.get("pause_scheduler"):
		return "pause_scheduler"
	if frappe.conf.get("disable_scheduler"):
		return "disable_scheduler"
	if not cint(frappe.db.get_single_value("System Settings", "enable_scheduler")):
		return "enable_scheduler unset in System Settings"
	return None


def queue_health():
	try:
		from frappe.utils.background_jobs import get_queue, get_queue_list, get_workers
	except ImportError:
		return {}  # helpers renamed in this Frappe version: unknown, not an error
	try:
		return {
			"lengths": {q: get_queue(q).count for q in get_queue_list()},
			"workers": len(get_workers()),
			"ingest_queue": INGEST_QUEUE,
			"ingest_backlog_seconds": _oldest_job_age(get_queue(INGEST_QUEUE)),
		}
	except Exception as e:
		return {"error": str(e)[:200] or type(e).__name__}


def _oldest_job_age(queue):
	"""Seconds the oldest job of an RQ queue has waited, or None if the queue is empty."""
	job_ids = queue.get_job_ids(0, 1)
	job = queue.fetch_job(job_ids[0]) if job_ids else None
	if not job or not job.enqueued_at:
		return None
	enqueued = job.enqueued_at
	if enqueued.tzinfo is None:  # RQ stores UTC, naive in older versions
		enqueued = enqueued.replace(tzinfo=timezone.utc)
	return max(int((datetime.now(timezone.utc) - enqueued).total_seconds()), 0)


def get_health():
	now = now_datetime()
	tz = events.system_tz()
	rules_at = _max("select max(last_evaluated) from `tabWolf Alert Rule` where enabled = 1")
	# last_execution is stamped when a worker starts the job, so it tracks jobs actually running
	job_at = _max("select max(last_execution) from `tabScheduled Job Type` where stopped = 0")
	reason = scheduler_inactive_reason()
	health = {
		"checked_at": heartbeat.to_utc_iso(now, tz),
		"server_version": __version__,
		"rules": {
			"last_evaluated": heartbeat.to_utc_iso(rules_at, tz),
			"age_seconds": heartbeat.age_seconds(rules_at, now),
		},
		"scheduler": {
			"enabled": reason is None,
			"reason": reason,
			"last_job_run": heartbeat.to_utc_iso(job_at, tz),
			"age_seconds": heartbeat.age_seconds(job_at, now),
		},
		"queues": queue_health(),
	}
	health["problems"] = heartbeat.assess(health)
	health["ok"] = not health["problems"]
	return health


def safe_health():
	"""get_health() that never breaks the request it is attached to."""
	try:
		return get_health()
	except Exception:
		frappe.db.rollback()
		frappe.logger("frappe_wolf").exception("could not compute Wolf health")
		return None


def report_stall(health):
	"""Record a `scheduler_stalled` event while rules are stale.

	Pushes reach the web workers even when the scheduler is dead, so this is where
	the site can still write the stall down. The "Scheduler stalled" preset turns it
	into an incident once the scheduler runs again, which leaves a timeline record;
	the live alert is the agent watchdog's job.
	"""
	stalled = heartbeat.staleness(health)
	if not stalled:
		return  # an ingest backlog alone is the watchdog's to report
	try:
		events.emit(
			"host_metric_alert",
			f"Wolf scheduler stalled: {stalled}"[:255],
			severity="critical",
			category="scheduler_stalled",
			source="wolf",
			payload={k: health.get(k) for k in ("problems", "rules", "scheduler", "queues")},
			fingerprint=fingerprint("scheduler_stalled", frappe.local.site),
			dedupe_minutes=STALL_DEDUPE_MINUTES,
		)
	except Exception:
		frappe.db.rollback()
		frappe.logger("frappe_wolf").exception("could not record scheduler stall")
