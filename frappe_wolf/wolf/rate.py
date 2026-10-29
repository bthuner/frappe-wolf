"""Per-agent push rate limit: a fixed one-minute window counted in Redis.

An agent pushes every 30 s, plus a burst when it replays its spool after an outage. Over the
limit the push gets HTTP 429, which the agent treats as retryable: the batch stays in its
spool and is replayed after a backoff, so nothing is lost.
"""

import time

DEFAULT_PUSHES_PER_MINUTE = 120
WINDOW_SECONDS = 60


def window_key(agent, now=None):
	return f"wolf_push_rate:{agent}:{int((time.time() if now is None else now) // WINDOW_SECONDS)}"


def check_push_rate(agent, limit):
	"""Count one push for `agent`; raise TooManyRequestsError once `limit` is exceeded this minute.

	`limit` <= 0 turns the check off. Redis trouble lets the push through: telemetry matters
	more than the limit.
	"""
	import frappe
	from frappe import _

	if limit <= 0:
		return None
	try:
		cache = frappe.cache()
		key = cache.make_key(window_key(agent))
		pipe = cache.pipeline()
		pipe.incr(key)
		pipe.expire(key, WINDOW_SECONDS * 2)
		count = int(pipe.execute()[0])
	except Exception:
		frappe.logger("frappe_wolf").warning("push rate limit skipped: Redis unavailable", exc_info=True)
		return None
	if count > limit:
		frappe.throw(
			_("Too many pushes from agent {0}: limit is {1} per minute, retry later").format(agent, limit),
			frappe.TooManyRequestsError,
		)
	return count
