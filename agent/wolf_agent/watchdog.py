"""Watchdog: alert through a fallback channel when the Wolf site itself stops working.

Wolf alerts are sent by the site the agent pushes to. If that site, its database or
its scheduler is down, those alerts never leave, so the agent watches the site and
uses its own channel (fallback.py) instead. One alert per outage, one recovery
notice when it ends; the state is kept in spool_dir so restarts do not repeat them.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from . import fallback
from .util import utc_iso

log = logging.getLogger(__name__)

STATE_FILE = "watchdog-state.json"
DEFAULT_STALE_MINUTES = 5
RETRY_SECONDS = 60


def assess(health, stale_minutes=DEFAULT_STALE_MINUTES):
	"""Problems in the push response's `health` (docs/protocol.md §4), empty if healthy.

	Mirrors frappe_wolf/wolf/heartbeat.py:assess; keep the two in sync.
	"""
	if not isinstance(health, dict):
		return []
	limit = max(float(stale_minutes or DEFAULT_STALE_MINUTES), 0.1) * 60
	rules_age = (health.get("rules") or {}).get("age_seconds")
	scheduler = health.get("scheduler") or {}
	what, age = ("alert rules", rules_age) if rules_age is not None else ("scheduled jobs", scheduler.get("age_seconds"))
	if not isinstance(age, (int, float)):
		return []
	if age >= limit:
		problem = f"{what} not evaluated for {int(age // 60)} min"
	elif age <= -limit:
		problem = f"last run of {what} is {int(-age // 60)} min in the future (time zone changed?)"
	else:
		return []

	queues = health.get("queues") or {}
	notes = []
	if scheduler.get("enabled") is False:
		notes.append(f"scheduler disabled ({scheduler.get('reason') or 'unknown reason'})")
	if queues.get("error"):
		notes.append(f"job queue unreachable: {queues['error']}")
	elif queues.get("workers") == 0:
		notes.append("no background worker running")
	return [problem + (f"; {', '.join(notes)}" if notes else "")]


def _fmt(ts):
	return utc_iso(datetime.fromtimestamp(ts, timezone.utc)) if ts else "never"


def _duration(seconds):
	minutes = int(max(seconds, 0) // 60)
	return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


class Watchdog:
	def __init__(self, cfg, state_path=None, clock=time.time, sender=None):
		self.wd = dict(cfg.get("watchdog") or {})
		self.enabled = self.wd.get("enabled", True) is not False
		try:
			minutes = float(self.wd.get("stale_minutes") or DEFAULT_STALE_MINUTES)
		except (TypeError, ValueError):
			minutes = DEFAULT_STALE_MINUTES
		self.stale_minutes = max(minutes, 0.1)
		self.hostname = cfg.get("hostname") or "?"
		self.url = (cfg.get("url") or "?").rstrip("/")
		self.state_path = state_path
		self.clock = clock
		self.started = clock()
		self.sender = sender or (lambda subject, text: fallback.send(self.wd, subject, text))
		self.state = self._load()

	# -- state ------------------------------------------------------------

	def _fresh_state(self):
		return {
			"status": "ok",  # ok | down
			"since": None,  # start of the current outage
			"problems": [],
			"alerted": False,  # outage alert delivered (or logged when no channel)
			"pending": None,  # "alert" | "recovery" still to deliver
			"next_attempt": 0,
			"last_error": None,
			"last_push_ok": None,
			"last_outage": None,  # {"since", "until", "problems"}
			"health": None,
		}

	def _load(self):
		state = self._fresh_state()
		if not self.state_path:
			return state
		try:
			with open(self.state_path) as f:
				saved = json.load(f)
			if isinstance(saved, dict):
				state.update(saved)
		except FileNotFoundError:
			pass
		except (OSError, ValueError) as e:
			log.warning("watchdog state %s unreadable, starting fresh: %s", self.state_path, e)
		return state

	def _save(self):
		if not self.state_path:
			return
		try:
			os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
			tmp = self.state_path + ".tmp"
			with open(tmp, "w") as f:
				json.dump(self.state, f, indent=1, sort_keys=True)
			os.replace(tmp, self.state_path)
		except OSError as e:
			log.warning("could not save watchdog state: %s", e)

	# -- observation ------------------------------------------------------

	def observe(self, push_ok, health=None):
		"""Feed the outcome of one push cycle. Never raises."""
		if not self.enabled:
			return
		try:
			before = json.dumps(self.state, sort_keys=True)
			self._observe(push_ok, health)
			self._flush()
			if json.dumps(self.state, sort_keys=True) != before:
				self._save()
		except Exception:
			log.exception("watchdog error (ignored)")

	def _observe(self, push_ok, health):
		now = self.clock()
		s = self.state
		if push_ok:
			s["last_push_ok"] = now
			s["health"] = health if isinstance(health, dict) else None
			problems = assess(health, self.stale_minutes)
		else:
			# count from our own start too: a push that last worked days ago, before this
			# process started, must not turn the first failure after a restart into an alert
			baseline = max(s.get("last_push_ok") or 0, self.started)
			if now - baseline < self.stale_minutes * 60:
				return  # a few failed pushes are normal: wait for the threshold
			problems = [f"pushes to {self.url} failing, last success {_fmt(s.get('last_push_ok'))}"]
		started = now if push_ok else max(s.get("last_push_ok") or 0, self.started)

		if problems and s["status"] == "ok":
			s.update(status="down", since=started, problems=problems, alerted=False, pending="alert", next_attempt=0)
			log.error("Wolf site %s is not working: %s", self.url, "; ".join(problems))
		elif problems:
			s["problems"] = problems
		elif s["status"] == "down":
			s["last_outage"] = {"since": s["since"], "until": now, "problems": s["problems"]}
			# no recovery notice for an outage nobody was told about
			s.update(status="ok", since=None, problems=[], pending="recovery" if s["alerted"] else None, next_attempt=0)
			log.warning("Wolf site %s recovered after %s", self.url, _duration(now - s["last_outage"]["since"]))

	def fatal(self, reason):
		"""Best-effort alert right before the agent exits (rejected credentials)."""
		if not self.enabled:
			return
		try:
			subject = f"[Wolf watchdog] {self.hostname}: agent stopped"
			text = f"wolf-agent on {self.hostname} stopped: {reason}\nWolf at {self.url} receives nothing from this host."
			self._deliver(subject, text)
		except Exception as e:
			log.error("could not send watchdog alert: %s", e)

	# -- delivery ---------------------------------------------------------

	def message(self, kind):
		s = self.state
		if kind == "alert":
			subject = f"[Wolf watchdog] {self.hostname}: Wolf at {self.url} is not working"
			lines = [f"Wolf watchdog on {self.hostname}: {self.url} is not working since {_fmt(s['since'])}."]
			lines += [f"- {p}" for p in s["problems"]]
			lines.append("Wolf alerts from this site may not be delivered until it is fixed.")
		elif kind == "recovery":
			outage = s.get("last_outage") or {}
			subject = f"[Wolf watchdog] {self.hostname}: Wolf at {self.url} recovered"
			lines = [
				f"Wolf watchdog on {self.hostname}: {self.url} works again since {_fmt(outage.get('until'))} "
				f"(outage of {_duration((outage.get('until') or 0) - (outage.get('since') or 0))}).",
			]
			lines += [f"- was: {p}" for p in outage.get("problems") or []]
		else:
			subject = f"[Wolf watchdog] {self.hostname}: test message"
			lines = [f"Test message from wolf-agent on {self.hostname}, watching {self.url}."]
		return subject, "\n".join(lines)

	def _deliver(self, subject, text):
		delivered = self.sender(subject, text)
		if not delivered:
			log.error("no watchdog fallback channel configured: %s\n%s", subject, text)
		return delivered

	def _flush(self):
		s = self.state
		now = self.clock()
		if not s.get("pending") or now < (s.get("next_attempt") or 0):
			return
		kind = s["pending"]
		try:
			delivered = self._deliver(*self.message(kind))
		except Exception as e:
			s["next_attempt"] = now + RETRY_SECONDS
			s["last_error"] = str(e)[:500]
			log.error("watchdog %s not delivered, retrying in %ss: %s", kind, RETRY_SECONDS, e)
			return
		if delivered:
			log.info("watchdog %s sent via %s", kind, ", ".join(delivered))
		if kind == "alert":
			s["alerted"] = True
		s.update(pending=None, next_attempt=0, last_error=None)

	def test(self):
		"""Send a test message; returns delivered channel names, raises FallbackError."""
		return self.sender(*self.message("test"))

	def summary(self):
		"""Human-readable lines for `wolf-agent check`."""
		s = self.state
		lines = [
			f"watchdog: {'enabled' if self.enabled else 'disabled'}, stale after {self.stale_minutes:g} min, "
			f"fallback: {', '.join(fallback.channels(self.wd)) or 'none (log only)'}",
			f"state: {s['status']}" + (f" since {_fmt(s['since'])}" if s["status"] == "down" else ""),
			f"last successful push: {_fmt(s.get('last_push_ok'))}",
		]
		lines += [f"problem: {p}" for p in s.get("problems") or []]
		if s.get("pending"):
			lines.append(f"pending {s['pending']} (last error: {s.get('last_error') or '-'})")
		if s.get("last_outage"):
			o = s["last_outage"]
			lines.append(f"last outage: {_fmt(o.get('since'))} → {_fmt(o.get('until'))}")
		return lines
