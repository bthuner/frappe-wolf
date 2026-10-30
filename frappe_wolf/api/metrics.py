"""The metric explorer of the Wolf UI (docs/ui-api.md).

The catalog (wolf/catalog.py) decides how a metric may be read: a gauge is averaged or taken at
its extremes, an interval count is summed. A query reads whole buckets at the resolution of its
window, exactly across raw samples and rollups, keeps empty buckets as gaps, and says what it
read: bucket size, resolution, where retention cut the window, which groups it left out and how
fresh each group is.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from frappe_wolf.api.scope import _scope, _window
from frappe_wolf.api.ui import READ_ROLES
from frappe_wolf.wolf import access, catalog, conditions, events, rollups, storage, window

MAX_GROUPS = 20
MAX_LABELS = 100
SEEN_DAYS = 7


def _allowed(table="Wolf Metric Rollup"):
	allowed, params = access.sql_condition(table)
	return allowed, params


@frappe.whitelist()
def get_metric_catalog():
	"""Every catalogued metric, and the custom ones seen lately, with what may be done with each
	and whether the caller has data for it in the last 7 days."""
	frappe.only_for(READ_ROLES)
	allowed, params = _allowed()
	seen = set(
		frappe.db.sql_list(
			f"""select distinct metric_name from `tabWolf Metric Rollup`
			where resolution = 3600 and bucket_start >= %(since)s{allowed}
			union select distinct metric_name from `tabWolf Metric Rollup`
			where resolution = 60 and bucket_start >= %(recent)s{allowed}""",
			{**params, "since": now_datetime() - timedelta(days=SEEN_DAYS), "recent": now_datetime() - timedelta(hours=2)},
		)
	)
	metrics = [{**catalog.describe(m), "seen": m in seen} for m in catalog.CATALOG]
	metrics += [{**catalog.describe(m), "seen": True} for m in sorted(seen - set(catalog.CATALOG))]
	return {"metrics": metrics, "seen_days": SEEN_DAYS, "max_groups": MAX_GROUPS}


@frappe.whitelist()
def get_labels(metric, site=None, bench=None, host=None):
	"""The label values (mount, queue, domain...) a metric has in scope, last 7 days."""
	frappe.only_for(READ_ROLES)
	scope = _scope(site, bench, host)
	allowed, params = _allowed()
	where = "".join(f" and `{k}` = %({k})s" for k in scope)
	values = frappe.db.sql_list(
		f"""select distinct label_key from `tabWolf Metric Rollup`
		where resolution = 3600 and metric_name = %(metric)s and bucket_start >= %(since)s
			and coalesce(label_key, '') != ''{where}{allowed}
		order by label_key limit %(limit)s""",
		{**params, **scope, "metric": metric, "since": now_datetime() - timedelta(days=SEEN_DAYS), "limit": MAX_LABELS + 1},
	)
	return {"metric": metric, "labels": values[:MAX_LABELS], "truncated": len(values) > MAX_LABELS}


def _retention_start(resolution, now):
	raw_hours, minute_days, hour_days = rollups.retention()
	return {
		storage.RAW: now - timedelta(hours=raw_hours),
		storage.MINUTE: now - timedelta(days=minute_days),
		storage.HOUR: now - timedelta(days=hour_days),
	}[resolution]


@frappe.whitelist()
def query_metric(metric, aggregation=None, group_by=None, label=None, range=None, since=None, until=None,
				 site=None, bench=None, host=None):  # fmt: skip
	"""One metric over a window, per bucket and group (docs/ui-api.md)."""
	frappe.only_for(READ_ROLES)
	if not metric:
		frappe.throw(_("Choose a metric"), frappe.ValidationError)
	try:
		aggregation, group_by = catalog.validate(metric, aggregation, group_by)
	except ValueError as e:
		frappe.throw(str(e), frappe.ValidationError)
	win, scope = _window(range, since, until), _scope(site, bench, host)
	tz = events.system_tz()
	now = now_datetime()
	seconds = (win.until - win.since).total_seconds()
	resolution = rollups.resolution_for(seconds)
	bucket = storage.chart_bucket(seconds, resolution)
	# whole buckets: the one still filling would draw as a drop, a cut first one as a dip
	first, end = storage.floor(win.since, bucket), storage.floor(win.until, bucket)

	where = ["metric_name = %(metric)s", *(f"`{k}` = %({k})s" for k in scope)]
	params = {"metric": metric, **scope}
	if label:
		where.append("label_key = %(label)s")
		params["label"] = str(label)[:140]
	allowed, allowed_params = _allowed()
	if allowed:
		where.append(allowed.removeprefix(" and "))
		params.update(allowed_params)
	group_cols = list(conditions.group_columns(group_by))

	rows = rollups.bucket_rows(where, params, group_cols, resolution, first, end)
	cells = storage.bucket_partials(rows, bucket)
	# per group: the window-wide figure (to rank groups) and the newest sample (freshness)
	whole = {}
	for (group, _start), part in cells.items():
		whole[group] = storage.merge(whole.get(group), part)
	reverse = aggregation != "min"  # the highest values first, the lowest for min
	ranked = sorted(whole, key=lambda g: (storage.finalize(whole[g], aggregation) or 0, str(g)), reverse=reverse)
	shown = ranked[:MAX_GROUPS]

	# freshness means something only when the window reaches now; in a past window an old last
	# sample is just where the window ends
	live = win.until >= now - timedelta(minutes=1)
	starts, t = [], first
	while t < end:
		starts.append(t)
		t += timedelta(seconds=bucket)
	series = []
	for group in shown:
		last = whole[group]["last_ts"]
		series.append({
			"group": dict(zip(group_cols, group, strict=True)),
			"name": " · ".join(str(v) for v in group if v) or metric,
			"points": [
				[window.to_utc(s, tz), storage.finalize(cells[(group, s)], aggregation) if (group, s) in cells else None]
				for s in starts
			],
			"value": storage.finalize(whole[group], aggregation),
			"samples": whole[group]["count"],
			"last_sample": window.to_utc(last, tz),
			"stale": live and now - last > catalog.stale_after(metric),
		})  # fmt: skip

	kept_from = _retention_start(resolution, now)
	return {
		"metric": catalog.describe(metric),
		"aggregation": aggregation,
		"group_by": group_by,
		"label": label or None,
		"scope": scope,
		"window": window.describe(window.Window(first, end, win.relative, win.time_zone)),
		"requested_window": window.describe(win),
		"resolution": storage.LABELS[resolution],
		"bucket_seconds": bucket,
		"series": series,
		"groups": {"shown": len(shown), "total": len(ranked), "ranked_by": f"{aggregation} over the window, {'highest' if reverse else 'lowest'} first"},
		# data older than this is gone at this resolution: those buckets are gaps, not zeros
		"retention": {"kept_from": window.to_utc(kept_from, tz), "clipped": first < kept_from},
		"live": live,
		"time_zone": tz,
	}
