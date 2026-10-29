"""Pure helpers for alert rule evaluation (no frappe imports, unit tested)."""

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
