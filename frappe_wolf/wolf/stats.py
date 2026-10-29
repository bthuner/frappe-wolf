"""MTTA / MTTR statistics, computed in Python so the query stays portable (pure, unit tested).

MTTA: acknowledged_at - started_at, over incidents that were acknowledged.
MTTR: resolved_at - started_at, over resolved incidents.
Durations are minutes. Negative durations (clock trouble, hand edits) are ignored.
"""

from frappe_wolf.wolf.incident_ops import minutes_between

GROUP_FIELDS = ("site", "bench", "host", "alert_rule", "severity")
NONE_LABEL = "(none)"


def percentile(values, p):
	"""Linear-interpolated percentile (numpy's default method). None for no values."""
	vals = sorted(v for v in values if v is not None)
	if not vals:
		return None
	if len(vals) == 1:
		return float(vals[0])
	rank = (len(vals) - 1) * (p / 100.0)
	low = int(rank)
	high = min(low + 1, len(vals) - 1)
	return float(vals[low] + (vals[high] - vals[low]) * (rank - low))


def summarize(values):
	vals = [v for v in values if v is not None]
	if not vals:
		return {"count": 0, "avg": None, "p50": None, "p90": None}
	return {
		"count": len(vals),
		"avg": round(sum(vals) / len(vals), 1),
		"p50": round(percentile(vals, 50), 1),
		"p90": round(percentile(vals, 90), 1),
	}


def durations(row):
	"""(time to acknowledge, time to resolve) in minutes, None when not applicable."""
	return (
		minutes_between(row.get("started_at"), row.get("acknowledged_at")),
		minutes_between(row.get("started_at"), row.get("resolved_at")) if row.get("status") == "Resolved" else None,
	)


def group_metrics(rows, group_by="site"):
	"""Rows of Wolf Incident (started_at, acknowledged_at, resolved_at, status, group field).

	Returns one dict per group, most incidents first: group, incidents, open, acknowledged,
	resolved and mtta/mttr avg, p50, p90 (minutes).
	"""
	if group_by not in GROUP_FIELDS:
		raise ValueError(f"cannot group by {group_by!r}")
	groups = {}
	for row in rows:
		key = row.get(group_by) or NONE_LABEL
		groups.setdefault(key, []).append(row)
	out = []
	for key, items in groups.items():
		pairs = [durations(i) for i in items]
		a, r = summarize(p[0] for p in pairs), summarize(p[1] for p in pairs)
		out.append(
			{
				"group": key,
				"incidents": len(items),
				"open": sum(1 for i in items if i.get("status") != "Resolved"),
				"acknowledged": a["count"],
				"resolved": r["count"],
				"mtta": a["avg"],
				"mtta_p50": a["p50"],
				"mtta_p90": a["p90"],
				"mttr": r["avg"],
				"mttr_p50": r["p50"],
				"mttr_p90": r["p90"],
			}
		)
	out.sort(key=lambda g: (-g["incidents"], str(g["group"])))
	return out


def overall(rows):
	"""Same figures over all rows (the report's total line)."""
	res = group_metrics([{**r, "site": "all"} for r in rows], "site")
	if not res:
		return None
	res[0]["group"] = "Total"
	return res[0]
