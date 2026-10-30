"""Time windows for the Wolf UI APIs: relative (`24h`) or explicit (`since`/`until`), no frappe imports.

Stored times are naive, in the site time zone. A window is parsed into that form for SQL and
described back in UTC (`describe`), so the UI never guesses which zone a time is in.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from frappe_wolf.wolf.schema import PayloadError, parse_timestamp

UNITS = {"m": "minutes", "h": "hours", "d": "days"}
MAX_SPAN = timedelta(days=400)  # longer than the 1-hour rollup retention (365 days)
DEFAULT_RANGE = "24h"


@dataclass(frozen=True)
class Window:
	since: datetime  # naive, site time zone
	until: datetime  # naive, site time zone
	relative: str | None  # "24h" for a relative window, None for an explicit one
	time_zone: str


def parse_range(value):
	"""'15m', '24h', '7d' -> timedelta."""
	match = re.fullmatch(r"\s*(\d{1,4})\s*([mhd])\s*", str(value or ""))
	if not match or int(match.group(1)) == 0:
		raise PayloadError(f"invalid range {value!r}: use a number and m, h or d, such as 24h")
	return timedelta(**{UNITS[match.group(2)]: int(match.group(1))})


def parse(now, time_zone, range=None, since=None, until=None):
	"""Window from request arguments. `now` is naive, in `time_zone`.

	Explicit `since` (and optional `until`, default now) win over `range`. Explicit times are
	ISO 8601; without an offset they are taken as UTC, like the agent protocol.
	"""
	if since:
		start = parse_timestamp(since, time_zone)
		end = parse_timestamp(until, time_zone) if until else now
		relative = None
	else:
		if until:
			raise PayloadError("until needs since")
		relative = str(range or DEFAULT_RANGE).strip()
		# subtract in UTC: across a DST change, 24h of wall clock is 23 or 25 real hours
		end = now
		start = from_utc(as_utc(now, time_zone) - parse_range(relative), time_zone)
	if start >= end:
		raise PayloadError("since must be before until")
	if end - start > MAX_SPAN:
		raise PayloadError(f"window longer than {MAX_SPAN.days} days")
	return Window(start, end, relative, time_zone)


def as_utc(dt, time_zone):
	return dt.replace(tzinfo=ZoneInfo(time_zone)).astimezone(timezone.utc)


def from_utc(dt, time_zone):
	return dt.astimezone(ZoneInfo(time_zone)).replace(tzinfo=None)


def to_utc(dt, time_zone):
	if dt is None:
		return None
	return as_utc(dt, time_zone).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(window):
	"""The `window` block of every response: what was actually read, in UTC."""
	return {
		"since": to_utc(window.since, window.time_zone),
		"until": to_utc(window.until, window.time_zone),
		"relative": window.relative,
		"time_zone": window.time_zone,
	}


def scope_filters(site=None, bench=None, host=None):
	"""{column: value} for the scope arguments. A bench is named per host, so it needs one."""
	scope = {k: str(v).strip() for k, v in (("site", site), ("bench", bench), ("host", host)) if v and str(v).strip()}
	if "bench" in scope and "host" not in scope:
		raise PayloadError("bench needs host: bench names repeat across hosts")
	return scope
