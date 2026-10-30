"""The metric explorer of the Wolf UI (docs/ui-api.md).

The catalog (wolf/catalog.py) decides how a metric may be read: a gauge is averaged or taken at
its extremes, an interval count is summed. A query reads whole buckets at a resolution that
fits the window's length and still reaches back to its start, exactly across raw samples and
rollups, keeps empty buckets as gaps, and says what it read: bucket size, resolution, where
retention cut the window, which groups it left out and how fresh each group is.
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
RECENT = timedelta(hours=2)  # freshness reads the newest sample here, whatever the window's buckets


def _recently_seen(select, extra, params):
	"""Values from hourly rollups of the last 7 days and minute rollups of the last 2 hours: a
	metric or label that appeared in the last hour is there before its first hourly rollup."""
	allowed, allowed_params = access.sql_condition("Wolf Metric Rollup")
	now = now_datetime()
	return frappe.db.sql_list(
		f"""select distinct {select} from `tabWolf Metric Rollup`
		where resolution = 3600 and bucket_start >= %(since)s{extra}{allowed}
		union select distinct {select} from `tabWolf Metric Rollup`
		where resolution = 60 and bucket_start >= %(recent)s{extra}{allowed}""",
		{**params, **allowed_params, "since": now - timedelta(days=SEEN_DAYS), "recent": now - RECENT},
	)


@frappe.whitelist()
def get_metric_catalog():
	"""Every catalogued metric, and the custom ones seen lately, with what may be done with each
	and whether the caller has data for it in the last 7 days."""
	frappe.only_for(READ_ROLES)
	seen = set(_recently_seen("metric_name", "", {}))
	metrics = [{**catalog.describe(m), "seen": m in seen} for m in catalog.CATALOG]
	metrics += [{**catalog.describe(m), "seen": True} for m in sorted(seen - set(catalog.CATALOG))]
	return {"metrics": metrics, "seen_days": SEEN_DAYS, "max_groups": MAX_GROUPS}


@frappe.whitelist()
def get_labels(metric, site=None, bench=None, host=None):
	"""The label values (mount, queue, domain...) a metric has in scope, recently."""
	frappe.only_for(READ_ROLES)
	scope = _scope(site, bench, host)
	extra = " and metric_name = %(metric)s and coalesce(label_key, '') != ''" + "".join(f" and `{k}` = %({k})s" for k in scope)
	values = sorted(_recently_seen("label_key", extra, {**scope, "metric": metric}))
	return {"metric": metric, "labels": values[:MAX_LABELS], "truncated": len(values) > MAX_LABELS}


def _group_condition(group_cols, groups, params):
	"""SQL selecting the rows of these groups only (NULL-safe on both databases)."""
	parts = []
	for i, group in enumerate(groups):
		cond = []
		for c, v in zip(group_cols, group, strict=True):
			params[f"g{i}_{c}"] = v or ""
			cond.append(f"coalesce(`{c}`, '') = %(g{i}_{c})s")
		parts.append("(" + " and ".join(cond) + ")")
	return "(" + " or ".join(parts) + ")"


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
	kept = rollups.kept_from(now)
	# fine enough for the window's length, and still reaching back to its start
	resolution = storage.resolution_covering(rollups.resolution_for(seconds), win.since, kept)
	bucket = storage.chart_bucket(seconds, resolution)
	first, end, starts = storage.whole_buckets(win.since, win.until, bucket)

	where = ["metric_name = %(metric)s", *(f"`{k}` = %({k})s" for k in scope)]
	params = {"metric": metric, **scope}
	if label:
		where.append("label_key = %(label)s")
		params["label"] = str(label)[:140]
	allowed, allowed_params = access.sql_condition("Wolf Metric Rollup")  # same columns as samples
	if allowed:
		where.append(allowed.removeprefix(" and "))
		params.update(allowed_params)
	group_cols = list(conditions.group_columns(group_by))

	# rank the groups in SQL over the whole window, then read bucket rows of the shown ones only
	totals = rollups.aggregate(where, params, group_cols, [], aggregation, resolution, first, end)
	reverse = aggregation != "min"  # the highest values first, the lowest for min
	missing = float("-inf") if reverse else float("inf")
	totals.sort(key=lambda r: (r.value if r.value is not None else missing, str([r.get(c) for c in group_cols])), reverse=reverse)
	shown = totals[:MAX_GROUPS]
	keys = [tuple(r.get(c) for c in group_cols) for r in shown]
	only_shown = [*where, _group_condition(group_cols, keys, params)] if group_cols and keys else where
	cells = {}
	if keys:
		cells = storage.bucket_partials(rollups.bucket_rows(only_shown, params, group_cols, resolution, first, end), bucket)

	# freshness only means something when the window reaches now; it is read from the newest
	# sample, not from the last whole bucket (which lags up to a bucket behind)
	live = win.until >= now - timedelta(minutes=1)
	newest = {}
	if live and keys:
		for r in rollups.aggregate(only_shown, params, group_cols, [], "last", storage.MINUTE, now - RECENT, now):
			newest[tuple(r.get(c) for c in group_cols)] = r.last_at

	series = []
	for row, group in zip(shown, keys, strict=True):
		last = newest.get(group) or row.last_at
		series.append({
			"group": dict(zip(group_cols, group, strict=True)),
			"name": " · ".join(str(v) for v in group if v) or metric,
			"points": [
				[window.to_utc(s, tz), storage.finalize(cells[(group, s)], aggregation) if (group, s) in cells else None]
				for s in starts
			],
			"value": row.value,
			"samples": row.samples,
			"last_sample": window.to_utc(last, tz),
			"stale": live and now - last > catalog.stale_after(metric),
		})  # fmt: skip

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
		"groups": {"shown": len(shown), "total": len(totals), "ranked_by": f"{aggregation} over the window, {'highest' if reverse else 'lowest'} first"},
		# data older than this is deleted at this resolution (rollups.purge): those buckets are gaps
		"retention": {"kept_from": window.to_utc(kept[resolution], tz), "clipped": first < kept[resolution]},
		"live": live,
		"time_zone": tz,
	}
