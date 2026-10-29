"""Pure helpers for alert rule evaluation (no frappe imports, unit tested)."""

import json
import operator
from datetime import time, timedelta

OPERATORS = {
	">": operator.gt,
	">=": operator.ge,
	"<": operator.lt,
	"<=": operator.le,
	"==": operator.eq,
	"!=": operator.ne,
}

AGGREGATIONS = ("avg", "min", "max", "sum", "count", "last")

GROUP_COLUMNS = {
	"none": (),
	"host": ("host",),
	"bench": ("host", "bench"),
	"site": ("site",),
	"host_label": ("host", "label_key"),
}


def compare(value, op, threshold):
	if value is None:
		return False
	try:
		fn = OPERATORS[op]
	except KeyError:
		raise ValueError(f"unknown operator {op!r}") from None
	return fn(float(value), float(threshold or 0))


def to_time(value):
	"""Accept datetime.time, timedelta (MariaDB TIME) or 'HH:MM[:SS]' strings."""
	if value in (None, ""):
		return None
	if isinstance(value, time):
		return value
	if isinstance(value, timedelta):
		seconds = int(value.total_seconds()) % 86400
		return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
	parts = [int(float(p)) for p in str(value).split(":")]
	while len(parts) < 3:
		parts.append(0)
	return time(parts[0] % 24, parts[1], parts[2])


def in_active_window(now_time, active_from, active_to):
	"""True when `now_time` is inside [from, to). Windows may wrap over midnight."""
	start, end = to_time(active_from), to_time(active_to)
	if start is None and end is None:
		return True
	start = start or time(0, 0)
	end = end or time(23, 59, 59, 999999)
	if start <= end:
		return start <= now_time < end
	return now_time >= start or now_time < end


def group_columns(group_by):
	try:
		return GROUP_COLUMNS[group_by or "none"]
	except KeyError:
		raise ValueError(f"unknown group_by {group_by!r}") from None


def group_key(group_by, row):
	cols = group_columns(group_by)
	if not cols:
		return "*"
	return ",".join(f"{c}={row.get(c) or '-'}" for c in cols)


def describe_group(group_by, row):
	"""Human label for an incident title, e.g. 'a.example.com' or 'bench-01 mount=/'."""
	parts = [str(row.get(c)) for c in group_columns(group_by) if row.get(c)]
	return " ".join(parts) or "all"


def is_breached(row, op, threshold, min_samples=1):
	if (row.get("samples") or 0) < max(min_samples or 1, 1):
		return False
	return compare(row.get("value"), op, threshold)


def severity_at_least(severity, minimum):
	order = ("info", "warning", "critical")
	if not minimum:
		return True
	return order.index(severity) >= order.index(minimum)


def severities_at_least(minimum):
	order = ("info", "warning", "critical")
	if not minimum:
		return list(order)
	return list(order[order.index(minimum) :])


def format_value(value):
	if value is None:
		return "n/a"
	value = float(value)
	if value.is_integer():
		return str(int(value))
	return f"{value:.2f}"


# Hysteresis -----------------------------------------------------------------
#
# Per (rule, group) we keep two counters between evaluations: consecutive
# breached evaluations ("b") and consecutive cleared evaluations ("c") while an
# incident is open. Groups with both counters at zero are dropped, so the state
# only holds groups that are about to fire or about to resolve.


def load_state(raw):
	"""Parse the JSON evaluation state of a rule, tolerating junk."""
	try:
		data = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
	except ValueError:
		return {}
	if not isinstance(data, dict):
		return {}
	state = {}
	for key, value in data.items():
		try:
			b, c = (int(value[0]), int(value[1]))
		except (TypeError, ValueError, IndexError, KeyError):
			continue
		if b or c:
			state[str(key)] = [max(b, 0), max(c, 0)]
	return state


def dump_state(state):
	return json.dumps({k: v for k, v in sorted(state.items()) if v and (v[0] or v[1])}, separators=(",", ":"))


def is_still_breached(row, op, threshold, resolve_threshold=None, min_samples=1):
	"""Whether an open incident should stay open: the resolve threshold, if any, replaces the fire threshold."""
	return is_breached(row or {}, op, threshold if resolve_threshold is None else resolve_threshold, min_samples)


def hysteresis_step(state, breached, still_breached, has_open, fire_after=1, resolve_after=1, auto_resolve=True):
	"""One evaluation for one group. Returns (new_state, action).

	- breached: the fire condition holds right now.
	- still_breached: the (possibly looser) resolve condition still holds.
	- has_open: an Open/Acknowledged incident exists for the group.

	action is one of:
	- "fire": open (or reopen) an incident, `fire_after` consecutive breaches reached
	- "update": the incident is open and the fire condition still holds
	- "resolve": `resolve_after` consecutive clear evaluations reached
	- "pending": counting towards fire or resolve, nothing to do yet
	- None: nothing to do
	new_state is [breaches, clears] or None when both are zero.
	"""
	b, c = (int(state[0]), int(state[1])) if state else (0, 0)
	fire_after = max(int(fire_after or 1), 1)
	resolve_after = max(int(resolve_after or 1), 1)

	if has_open:
		if breached:
			return None, "update"
		if still_breached or not auto_resolve:
			return None, None
		c += 1
		if c >= resolve_after:
			return None, "resolve"
		return [0, c], "pending"

	if not breached:
		return None, None
	b += 1
	if b >= fire_after:
		return None, "fire"
	return [b, 0], "pending"


def resolve_threshold_error(op, threshold, resolve_threshold):
	"""None when the resolve threshold is on the safe side of the fire threshold, else a message."""
	threshold, resolve_threshold = float(threshold or 0), float(resolve_threshold or 0)
	if op in (">", ">=") and resolve_threshold > threshold:
		return "For > and >= rules the resolve threshold must be at most the threshold"
	if op in ("<", "<=") and resolve_threshold < threshold:
		return "For < and <= rules the resolve threshold must be at least the threshold"
	if op in ("==", "!="):
		return "A resolve threshold only makes sense with <, <=, > or >="
	return None


# Silences, escalation and digests --------------------------------------------


def silence_matches(silence, scope, rule=None):
	"""Every field set on the silence must equal the incident's. An empty silence matches all."""
	if silence.get("alert_rule") and silence.get("alert_rule") != rule:
		return False
	for field in ("host", "bench", "site"):
		want = silence.get(field)
		if want and want != (scope.get(field) or None):
			return False
	return True


def escalation_due(notified_at, escalate_after_minutes, now):
	if not notified_at or not escalate_after_minutes or int(escalate_after_minutes) <= 0:
		return False
	return now - notified_at >= timedelta(minutes=int(escalate_after_minutes))


DIGEST_KINDS = ("fired", "reopened", "resolved")


def should_digest(channel_digest, severity, kind):
	"""Critical alerts, escalations and tests always go out immediately."""
	return (channel_digest or "none") == "hourly" and severity != "critical" and kind in DIGEST_KINDS


def channel_destination(channel):
	"""Stable identity of a channel's target, used to merge digests across rules."""
	ctype = channel.get("channel_type")
	if ctype == "Email":
		raw = (channel.get("recipients") or "").replace(",", "\n").splitlines()
		return "\n".join(sorted({r.strip().lower() for r in raw if r.strip()}))
	if ctype == "Webhook":
		return (channel.get("webhook_url") or "").strip()
	if ctype == "Raven":
		return (channel.get("raven_channel") or "").strip()
	return ""


def group_digest(items):
	"""{(channel_type, destination): [items in order]} for queued digest items."""
	groups = {}
	for item in items:
		key = (item.get("channel_type"), item.get("destination") or "")
		if not key[1]:
			continue
		groups.setdefault(key, []).append(item)
	return groups
