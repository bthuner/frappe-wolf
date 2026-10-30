"""Metric rollups (raw -> 1 minute -> 1 hour), retention per resolution, and reads across them.

The rollup job runs every minute. Each resolution keeps a watermark in Wolf Rollup State
(`done_until`): every bucket before it is rolled. A run recomputes buckets from the watermark
up to the last closed bucket, one chunk per transaction holding the watermark row lock: delete
the chunk's rollup rows, insert them again from the source, move the watermark. Re-running a
chunk therefore never double counts. Samples that arrive late (agent spool replay) move the watermarks back so
their buckets are recomputed. All SQL is plain enough for MariaDB and Postgres; bucketing is
done in Python (wolf/storage.py).
"""

import time
from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from frappe_wolf.wolf import storage
from frappe_wolf.wolf.storage import HOUR, MINUTE, RAW

RAW_TABLE = "tabWolf Metric Sample"
ROLLUP_DOCTYPE = "Wolf Metric Rollup"
STATE_DOCTYPE = "Wolf Rollup State"
STATE_NAMES = {MINUTE: "1m", HOUR: "1h"}

# A minute is rolled 2 minutes after it closed, so normal pushes (every 30 s, then a queued
# insert) are in. Anything later moves the watermark back.
GRACE = timedelta(minutes=2)
CHUNK = {MINUTE: timedelta(minutes=15), HOUR: timedelta(hours=2)}
RUN_BUDGET_SECONDS = 45

ROLLUP_FIELDS = [
	"name", "creation", "modified", "owner", "modified_by", "docstatus",
	"metric_name", "resolution", "bucket_start", "host", "bench", "site", "label_key",
	"value_count", "value_sum", "value_min", "value_max", "value_last", "last_ts",
]  # fmt: skip


# --- settings and watermarks -------------------------------------------------------------


def retention():
	"""(raw_hours, minute_days, hour_days), with defaults for unset values."""
	values = frappe.db.sql(
		"select field, value from `tabSingles` where doctype = 'Wolf Settings' and field in %(fields)s",
		{"fields": ("raw_retention_hours", "minute_retention_days", "hour_retention_days")},
	)
	stored = {field: cint(value) for field, value in values}
	return (
		stored.get("raw_retention_hours") or storage.DEFAULT_RAW_HOURS,
		stored.get("minute_retention_days") or storage.DEFAULT_MINUTE_DAYS,
		stored.get("hour_retention_days") or storage.DEFAULT_HOUR_DAYS,
	)


def watermarks():
	"""{MINUTE: datetime|None, HOUR: datetime|None}"""
	rows = dict(frappe.db.sql(f"select name, done_until from `tab{STATE_DOCTYPE}`"))
	return {res: get_datetime(rows[name]) if rows.get(name) else None for res, name in STATE_NAMES.items()}


def _ensure_state(res):
	name = STATE_NAMES[res]
	if not frappe.db.exists(STATE_DOCTYPE, name):
		doc = frappe.get_doc({"doctype": STATE_DOCTYPE, "resolution": name})
		doc.flags.ignore_permissions = True
		doc.insert()
	return name


def _lock_state(name):
	"""Lock one watermark row for this transaction and return its value."""
	rows = frappe.db.sql(f"select done_until from `tab{STATE_DOCTYPE}` where name = %(name)s for update", {"name": name})
	return get_datetime(rows[0][0]) if rows and rows[0][0] else None


def note_samples(timestamps, now=None):
	"""Move watermarks back for samples that may be in rolled buckets (called after metric inserts).

	The rollup job holds the watermark row lock while it rolls a chunk, so this update waits for
	it and then sees the moved watermark: a sample is either read by the chunk or re-rolled.
	"""
	now = now or now_datetime()
	recent = now - GRACE + timedelta(seconds=30)
	marks = watermarks()
	behind = [marks[res] for res in (MINUTE, HOUR) if marks[res]]
	if not any(t is not None and (t < recent or any(t < m for m in behind)) for t in timestamps):
		return
	oldest = storage.late_horizon(now, retention()[0])
	for res in (MINUTE, HOUR):
		new = storage.late_restart(timestamps, recent, oldest, res, marks[res])
		if new is None:
			return
		frappe.db.sql(
			f"update `tab{STATE_DOCTYPE}` set done_until = %(new)s where name = %(name)s and done_until > %(new)s",
			{"new": new, "name": STATE_NAMES[res]},
		)


# --- the rollup job ------------------------------------------------------------------------


def _first(sql, params=None):
	value = frappe.db.sql(sql, params or {})[0][0]
	return get_datetime(value) if value else None


def _source_rows(res, start, end):
	if res == MINUTE:
		return frappe.db.sql(
			f"""select metric_name, host, bench, site, label_key, `value`, `timestamp` from `{RAW_TABLE}`
			where `timestamp` >= %(start)s and `timestamp` < %(end)s""",
			{"start": start, "end": end},
		)
	return frappe.db.sql(
		f"""select metric_name, host, bench, site, label_key, bucket_start,
			value_count, value_sum, value_min, value_max, value_last, last_ts
		from `tab{ROLLUP_DOCTYPE}`
		where resolution = %(res)s and bucket_start >= %(start)s and bucket_start < %(end)s""",
		{"res": MINUTE, "start": start, "end": end},
	)


def _next_source_time(res, after=None):
	"""Time of the first source data at or after `after`, to skip empty stretches."""
	params = {"res": MINUTE, "t": after}
	if res == MINUTE:
		cond = "where `timestamp` >= %(t)s" if after else ""
		return _first(f"select min(`timestamp`) from `{RAW_TABLE}` {cond}", params)
	cond = "and bucket_start >= %(t)s" if after else ""
	return _first(f"select min(bucket_start) from `tab{ROLLUP_DOCTYPE}` where resolution = %(res)s {cond}", params)


def _delete_buckets(res, start, end):
	frappe.db.sql(
		f"""delete from `tab{ROLLUP_DOCTYPE}`
		where resolution = %(res)s and bucket_start >= %(start)s and bucket_start < %(end)s""",
		{"res": res, "start": start, "end": end},
	)


def replace_buckets(res, start, end, partials):
	"""Replace the rollup rows of [start, end) at `res` by `partials` (storage.rollup_* output)."""
	_delete_buckets(res, start, end)
	now = now_datetime()
	clamped = sorted({key[0] for key, p in partials.items() if storage.clamp_sum(p)})
	if clamped:
		frappe.log_error(
			title=f"[Wolf] {STATE_NAMES[res]} rollup sum out of range",
			message=f"Sums clamped to ±{storage.MAX_STORED} in [{start}, {end}), so avg and sum are wrong there: "
			+ ", ".join(clamped),
		)
	values = [
		(
			storage.rollup_name(res, key), now, now, "Administrator", "Administrator", 0,
			key[0], res, key[5], key[1], key[2], key[3], key[4],
			p["count"], p["sum"], p["min"], p["max"], p["last"], p["last_ts"],
		)  # fmt: skip
		for key, p in partials.items()
	]
	insert_rows(f"tab{ROLLUP_DOCTYPE}", ROLLUP_FIELDS, values)
	return len(values)


def insert_rows(table, fields, rows, chunk=1000):
	"""Multi-row INSERT with plain placeholders (MariaDB and Postgres).

	frappe.db.bulk_insert builds its query with the query builder, which costs seconds per
	10,000 rows; a minute of 100 hosts is about 1,500 rollup rows, a 15-minute backfill
	chunk 20,000.
	"""
	cols = ", ".join(f"`{f}`" for f in fields)
	row_ph = "(" + ", ".join(["%s"] * len(fields)) + ")"
	for i in range(0, len(rows), chunk):
		part = rows[i : i + chunk]
		frappe.db.sql(
			f"insert into `{table}` ({cols}) values {', '.join([row_ph] * len(part))}",
			[v for row in part for v in row],
		)


def roll(res, now=None, deadline=None):
	"""Roll closed buckets of `res` (MINUTE from raw, HOUR from MINUTE). Returns rows written.

	One chunk per transaction, holding the watermark row lock: delete the chunk's rollups,
	insert them again from the source, move the watermark to the chunk end.
	"""
	now = now or now_datetime()
	name = _ensure_state(res)
	frappe.db.commit()
	written = 0
	while deadline is None or time.monotonic() < deadline:
		start = _lock_state(name)
		if res == MINUTE:
			limit = storage.floor(now - GRACE, MINUTE)
		else:
			minute_until = watermarks()[MINUTE]
			limit = storage.floor(minute_until, HOUR) if minute_until else None
		if start is None and limit is not None:
			first = _next_source_time(res)
			start = storage.floor(first, res) if first else None
		if start is None or limit is None or start >= limit:
			frappe.db.rollback()
			break
		end = min(start + CHUNK[res], limit)
		rows = _source_rows(res, start, end)
		partials = storage.rollup_samples(rows, res) if res == MINUTE else storage.rollup_rollups(rows, res)
		written += replace_buckets(res, start, end, partials)
		if not rows:
			# empty chunk: jump to the bucket of the next data (clearing stale rollups on the way)
			nxt = _next_source_time(res, end)
			skip_to = min(storage.floor(nxt, res), limit) if nxt else limit
			if skip_to > end:
				_delete_buckets(res, end, skip_to)
				end = skip_to
		frappe.db.sql(
			f"update `tab{STATE_DOCTYPE}` set done_until = %(end)s where name = %(name)s", {"end": end, "name": name}
		)
		frappe.db.commit()
	return written


def run(now=None):
	"""Scheduler entry point: minute rollups, then hour rollups, within a time budget."""
	started = time.monotonic()
	deadline = started + RUN_BUDGET_SECONDS
	for res in (MINUTE, HOUR):
		t0 = time.monotonic()
		try:
			rows = roll(res, now=now, deadline=deadline)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"[Wolf] {STATE_NAMES[res]} metric rollup failed")
			continue
		frappe.db.set_value(
			STATE_DOCTYPE,
			STATE_NAMES[res],
			{"last_run_at": now_datetime(), "last_run_rows": rows, "last_run_seconds": round(time.monotonic() - t0, 3)},
			update_modified=False,
		)
		frappe.db.commit()


# --- retention -----------------------------------------------------------------------------


def _delete_before(table, column, cutoff, step, extra="", params=None):
	"""Delete rows with `column` < cutoff, oldest first, one transaction per `step`."""
	params = dict(params or {})
	oldest = _first(f"select min({column}) from `{table}` where 1=1 {extra}", params)
	deleted_until = None
	while oldest is not None and oldest < cutoff:
		upper = min(oldest + step, cutoff)
		frappe.db.sql(f"delete from `{table}` where {column} < %(upper)s {extra}", {**params, "upper": upper})
		frappe.db.commit()
		deleted_until = oldest = upper
	return deleted_until


def kept_from(now=None, rolled=True):
	"""{RAW, MINUTE, HOUR: time} before which purge deletes that resolution's data.

	Never data that is not rolled up yet: raw samples stay until the 1-minute watermark (up to
	the 1-minute retention if rollups never ran), minute rollups until the hour watermark.
	`rolled` is False where rollups do not run (satellites): raw retention then applies as is.
	"""
	now = now or now_datetime()
	raw_hours, minute_days, hour_days = retention()
	marks = watermarks()
	raw = now - timedelta(hours=raw_hours)
	if rolled:
		raw = min(raw, marks[MINUTE]) if marks[MINUTE] else now - timedelta(days=minute_days)
	minute = now - timedelta(days=minute_days)
	minute = min(minute, marks[HOUR]) if marks[HOUR] else now - timedelta(days=hour_days)
	return {RAW: raw, MINUTE: minute, HOUR: now - timedelta(days=hour_days)}


def purge(now=None, rolled=True):
	"""Apply raw / 1-minute / 1-hour retention (kept_from)."""
	cut = kept_from(now, rolled)
	raw_cutoff, minute_cutoff, hour_cutoff = cut[RAW], cut[MINUTE], cut[HOUR]

	_delete_before(RAW_TABLE, "`timestamp`", raw_cutoff, timedelta(hours=1))
	rollup_table = f"tab{ROLLUP_DOCTYPE}"
	_delete_before(rollup_table, "bucket_start", minute_cutoff, timedelta(days=1), "and resolution = %(res)s", {"res": MINUTE})
	_delete_before(rollup_table, "bucket_start", hour_cutoff, timedelta(days=30), "and resolution = %(res)s", {"res": HOUR})
	return {"raw": raw_cutoff, "1m": minute_cutoff, "1h": hour_cutoff}


# --- reads -----------------------------------------------------------------------------------


def resolution_for(window_seconds):
	raw_hours, minute_days, _ = retention()
	return storage.pick_resolution(window_seconds, raw_hours, minute_days)


def segments(resolution, since, until):
	marks = watermarks()
	return storage.plan_segments(resolution, since, until, marks[MINUTE], marks[HOUR])


def _segment_sql(res, start, end):
	"""(table, time column, where clause, params) of one read segment."""
	if res == RAW:
		return RAW_TABLE, "`timestamp`", "`timestamp` between %(seg_start)s and %(seg_end)s", {"seg_start": start, "seg_end": end}
	return (
		f"tab{ROLLUP_DOCTYPE}",
		"bucket_start",
		"resolution = %(seg_res)s and bucket_start >= %(seg_start)s and bucket_start < %(seg_end)s",
		{"seg_res": res, "seg_start": start, "seg_end": end},
	)


def aggregate(where, params, group_cols, hint_fields, aggregation, resolution, since, until):
	"""Aggregate metric samples per group over [since, until], reading the given resolution.

	`where` is a list of SQL conditions on columns shared by samples and rollups (metric_name,
	host, bench, site, label_key). Returns frappe._dict rows like a SQL group-by would:
	group columns, `<field>_hint` for each hint field, value, samples, first_at. For `last`,
	also `label_key` of the latest sample when the groups do not split by label (which disk).
	"""
	merged = {}
	for res, start, end in segments(resolution, since, until):
		table, ts_col, seg_where, seg_params = _segment_sql(res, start, end)
		if res == RAW:
			cols = "count(*) as c, sum(`value`) as s, min(`value`) as mn, max(`value`) as mx, max(`timestamp`) as lt"
			value_col, last_col = "`value`", "`timestamp`"
		else:
			cols = (
				"sum(value_count) as c, sum(value_sum) as s, min(value_min) as mn, max(value_max) as mx, max(last_ts) as lt"
			)
			value_col, last_col = "value_last", "last_ts"
		select = [f"`{c}`" for c in group_cols] + [f"max(`{f}`) as {f}_hint" for f in hint_fields]
		group = f"group by {', '.join(f'`{c}`' for c in group_cols)}" if group_cols else ""
		conds = " and ".join([*where, seg_where])
		rows = frappe.db.sql(
			f"select {', '.join([*select, cols, f'min({ts_col}) as first_at'])} from `{table}` where {conds} {group}",
			{**params, **seg_params},
			as_dict=True,
		)
		rows = [r for r in rows if r.c]
		if not rows:
			continue
		lasts = {}
		if aggregation == "last":
			# value at max(last time) per group: only rows from the oldest group's last time on
			lasts = _last_values(table, value_col, last_col, conds, {**params, **seg_params}, group_cols, min(r.lt for r in rows))
		for r in rows:
			key = tuple(r.get(c) for c in group_cols)
			last, last_label = lasts.get(key, (0.0, None))
			part = {
				"count": int(r.c),
				"sum": float(r.s or 0),
				"min": float(r.mn),
				"max": float(r.mx),
				"last": last,
				"last_ts": get_datetime(r.lt),
				"last_label": last_label,
			}
			entry = merged.get(key)
			if entry is None:
				entry = merged[key] = frappe._dict({c: r.get(c) for c in group_cols})
				entry.partial, entry.first_at = None, get_datetime(r.first_at)
				for f in hint_fields:
					entry[f"{f}_hint"] = None
			entry.partial = storage.merge(entry.partial, part)
			entry.first_at = min(entry.first_at, get_datetime(r.first_at))
			for f in hint_fields:
				hint = r.get(f"{f}_hint")
				if hint is not None and (entry[f"{f}_hint"] is None or hint > entry[f"{f}_hint"]):
					entry[f"{f}_hint"] = hint

	out = []
	for entry in merged.values():
		partial = entry.pop("partial")
		entry.value = storage.finalize(partial, aggregation)
		entry.samples = partial["count"]
		entry.last_at = partial["last_ts"]  # time of the newest sample read
		if aggregation == "last" and "label_key" not in group_cols:
			entry.label_key = partial.get("last_label")
		out.append(entry)
	return out


def _last_values(table, value_col, last_col, conds, params, group_cols, oldest_last):
	"""Value (and label) at the newest time of each group, picked in SQL: only the rows at each
	group's own newest time come back, however long ago another group stopped."""
	cols = ", ".join(f"`{c}`" for c in group_cols)
	keys = ", ".join(f"coalesce(`{c}`, '') as g{i}" for i, c in enumerate(group_cols))
	join = " and ".join([*(f"coalesce(a.`{c}`, '') = x.g{i}" for i, c in enumerate(group_cols)), "a.t = x.m"])
	rows = frappe.db.sql(
		f"""select {', '.join([*(f'a.`{c}`' for c in group_cols), 'a.v', 'a.t', 'a.l'])}
		from (select {cols + ', ' if cols else ''}{value_col} as v, {last_col} as t, label_key as l
			from `{table}` where {conds} and {last_col} >= %(oldest_last)s) a
		join (select {keys + ', ' if keys else ''}max({last_col}) as m
			from `{table}` where {conds} and {last_col} >= %(oldest_last)s {'group by ' + cols if cols else ''}) x
		on {join}""",
		{**params, "oldest_last": oldest_last},
		as_dict=True,
	)
	best = {}
	for r in rows:
		key = tuple(r.get(c) for c in group_cols)
		cand = (get_datetime(r.t), float(r.v), r.l or "")
		if key not in best or cand > best[key]:
			best[key] = cand
	return {key: (value, label or None) for key, (_, value, label) in best.items()}


def series_rows(where, params, resolution, since, until):
	"""Rows for wolf.series.bucket_series: metric_name, label_key, timestamp, value, count."""
	rows = []
	for res, start, end in segments(resolution, since, until):
		table, ts_col, seg_where, seg_params = _segment_sql(res, start, end)
		value, count = ("`value`", "1") if res == RAW else ("value_sum", "value_count")
		rows += frappe.db.sql(
			f"""select metric_name, label_key, {ts_col} as `timestamp`, {value} as `value`, {count} as `count`
			from `{table}` where {' and '.join([*where, seg_where])}""",
			{**params, **seg_params},
			as_dict=True,
		)
	return rows


def bucket_rows(where, params, group_cols, resolution, since, until):
	"""(group, time, partial) rows over [since, until), for storage.bucket_partials.

	Raw samples become one-sample partials; rollup rows keep their count, sum, min, max and
	last, so any aggregation of a chart bucket stays exact across the raw/rollup boundary.
	"""
	cols = [f"`{c}`" for c in group_cols]
	out = []
	for res, start, end in segments(resolution, since, until):
		table, ts_col, seg_where, seg_params = _segment_sql(res, start, end)
		if res == RAW:
			select = ", ".join([*cols, "`value` as v", "`timestamp` as t"])
		else:
			select = ", ".join([*cols, "bucket_start as t", "value_count as c", "value_sum as s", "value_min as mn",
								"value_max as mx", "value_last as l", "last_ts as lt"])  # fmt: skip
		rows = frappe.db.sql(
			f"select {select} from `{table}` where {' and '.join([*where, seg_where])}",
			{**params, **seg_params},
			as_dict=True,
		)
		for r in rows:
			group = tuple(r.get(c) for c in group_cols)
			at = get_datetime(r.t)
			if res == RAW:
				if at >= until:
					continue  # [since, until): the raw segment's end is inclusive
				part = storage.new_partial(float(r.v), at)
			else:
				part = {"count": int(r.c), "sum": float(r.s), "min": float(r.mn), "max": float(r.mx),
						"last": float(r.l), "last_ts": get_datetime(r.lt)}  # fmt: skip
			out.append((group, at, part))
	return out
