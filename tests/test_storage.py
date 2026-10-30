import random
import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import storage
from frappe_wolf.wolf.series import bucket_series
from frappe_wolf.wolf.storage import HOUR, MINUTE, RAW

T0 = datetime(2026, 10, 29, 2, 0, 0)


def raw_aggregate(values, aggregation):
	"""What a SQL aggregate over raw samples gives: values is [(ts, value), ...]."""
	vals = [v for _, v in values]
	if aggregation == "avg":
		return sum(vals) / len(vals)
	if aggregation == "count":
		return len(vals)
	if aggregation == "sum":
		return sum(vals)
	if aggregation == "min":
		return min(vals)
	if aggregation == "max":
		return max(vals)
	return max(values)[1]  # last: latest timestamp, ties broken by the larger value


class TestBuckets(unittest.TestCase):
	def test_floor_and_ceil(self):
		ts = datetime(2026, 10, 29, 2, 7, 59, 500000)
		self.assertEqual(storage.floor(ts, MINUTE), datetime(2026, 10, 29, 2, 7))
		self.assertEqual(storage.floor(ts, HOUR), datetime(2026, 10, 29, 2, 0))
		self.assertEqual(storage.ceil(ts, MINUTE), datetime(2026, 10, 29, 2, 8))
		self.assertEqual(storage.ceil(T0, HOUR), T0)

	def test_rollup_name_is_deterministic(self):
		key = ("cpu_usage", "h1", None, None, None, T0)
		self.assertEqual(storage.rollup_name(MINUTE, key), storage.rollup_name(MINUTE, key))
		self.assertNotEqual(storage.rollup_name(MINUTE, key), storage.rollup_name(HOUR, key))
		# NULL and empty bench are different groups in SQL, so they need different names
		self.assertNotEqual(storage.rollup_name(MINUTE, key), storage.rollup_name(MINUTE, ("cpu_usage", "h1", "", None, None, T0)))
		self.assertEqual(len(storage.rollup_name(MINUTE, key)), 32)


class TestRollupExactness(unittest.TestCase):
	"""Raw -> 1m -> 1h must answer every aggregation like the raw samples do."""

	def setUp(self):
		rng = random.Random(7)
		self.samples = []  # (metric, host, bench, site, label, value, ts)
		for label in ("mount=/", "mount=/data"):
			for i in range(3 * 120):  # 3 hours, every 30 s, with jitter
				ts = T0 + timedelta(seconds=30 * i + rng.randint(0, 29))
				self.samples.append(("disk_used_percent", "h1", None, None, label, round(rng.uniform(0, 100), 3), ts))
		rng.shuffle(self.samples)

	def series(self, label, start, end):
		return [(s[6], s[5]) for s in self.samples if s[4] == label and start <= s[6] < end]

	def test_minute_and_hour_rollups_match_raw(self):
		minutes = storage.rollup_samples(self.samples, MINUTE)
		rows = [(*key[:5], key[5], p["count"], p["sum"], p["min"], p["max"], p["last"], p["last_ts"]) for key, p in minutes.items()]
		hours = storage.rollup_rollups(rows, HOUR)
		self.assertEqual(len(hours), 2 * 3)
		for agg in ("avg", "min", "max", "sum", "count", "last"):
			for label in ("mount=/", "mount=/data"):
				expected = raw_aggregate(self.series(label, T0, T0 + timedelta(hours=3)), agg)
				for rollup in (minutes, hours):
					partial = None
					for key, p in rollup.items():
						if key[4] == label:
							partial = storage.merge(partial, p)
					self.assertAlmostEqual(storage.finalize(partial, agg), expected, places=6, msg=(agg, label))

	def test_one_bucket(self):
		minutes = storage.rollup_samples(self.samples, MINUTE)
		start = T0 + timedelta(minutes=17)
		p = minutes[("disk_used_percent", "h1", None, None, "mount=/", start)]
		values = self.series("mount=/", start, start + timedelta(minutes=1))
		self.assertEqual(p["count"], len(values))
		self.assertEqual(p["last_ts"], max(values)[0])
		self.assertEqual(p["last"], max(values)[1])

	def test_rolling_twice_gives_the_same_rows(self):
		# idempotent: a chunk recomputed from the same source is identical (no double count)
		self.assertEqual(storage.rollup_samples(self.samples, MINUTE), storage.rollup_samples(list(reversed(self.samples)), MINUTE))

	def test_merge_last_prefers_latest(self):
		a = storage.new_partial(5.0, T0 + timedelta(seconds=10))
		b = storage.new_partial(1.0, T0 + timedelta(seconds=50))
		self.assertEqual(storage.merge(a, b)["last"], 1.0)
		self.assertEqual(storage.merge(b, a)["last"], 1.0)
		self.assertEqual(storage.merge(None, a), a)

	def test_finalize(self):
		self.assertIsNone(storage.finalize(None, "avg"))
		with self.assertRaises(ValueError):
			storage.finalize(storage.new_partial(1.0, T0), "p95")

	def test_clamp_sum(self):
		# two values that fit the column, whose sum does not
		big = storage.merge(storage.new_partial(6e11, T0), storage.new_partial(6e11, T0 + timedelta(seconds=30)))
		self.assertTrue(storage.clamp_sum(big))
		self.assertEqual(big["sum"], storage.MAX_STORED)
		self.assertEqual(big["max"], 6e11)
		low = storage.merge(storage.new_partial(-6e11, T0), storage.new_partial(-6e11, T0))
		self.assertTrue(storage.clamp_sum(low))
		self.assertEqual(low["sum"], -storage.MAX_STORED)
		small = storage.new_partial(91.5, T0)
		self.assertFalse(storage.clamp_sum(small))
		self.assertEqual(small["sum"], 91.5)


class TestResolution(unittest.TestCase):
	def test_pick_resolution(self):
		self.assertEqual(storage.pick_resolution(5 * 60), RAW)  # rule windows
		self.assertEqual(storage.pick_resolution(3600), RAW)  # 1 h chart
		self.assertEqual(storage.pick_resolution(3 * 3600), RAW)
		self.assertEqual(storage.pick_resolution(6 * 3600), MINUTE)
		self.assertEqual(storage.pick_resolution(24 * 3600), MINUTE)
		self.assertEqual(storage.pick_resolution(2 * 86400), MINUTE)
		self.assertEqual(storage.pick_resolution(7 * 86400), HOUR)

	def test_pick_resolution_respects_retention(self):
		self.assertEqual(storage.pick_resolution(2 * 3600, raw_hours=1), MINUTE)
		self.assertEqual(storage.pick_resolution(36 * 3600, minute_days=1), HOUR)
		self.assertEqual(storage.pick_resolution(3600, raw_hours=0), RAW)  # unset = default

	def test_chart_bucket(self):
		self.assertEqual(storage.chart_bucket(3600, RAW), 60)
		self.assertEqual(storage.chart_bucket(6 * 3600, MINUTE), 180)
		self.assertEqual(storage.chart_bucket(24 * 3600, MINUTE), 720)
		self.assertEqual(storage.chart_bucket(7 * 86400, HOUR), 7200)  # 5040 s rounded to whole hours


class TestPlanSegments(unittest.TestCase):
	now = datetime(2026, 10, 29, 12, 34, 56)

	def test_raw_reads_raw_only(self):
		since = self.now - timedelta(minutes=5)
		self.assertEqual(storage.plan_segments(RAW, since, self.now, datetime(2026, 10, 29, 12, 32), T0), [(RAW, since, self.now)])

	def test_minute_then_raw_tail(self):
		since = self.now - timedelta(hours=24)
		minute_until = datetime(2026, 10, 29, 12, 32)
		segs = storage.plan_segments(MINUTE, since, self.now, minute_until, datetime(2026, 10, 29, 12))
		self.assertEqual(segs, [(MINUTE, datetime(2026, 10, 28, 12, 35), minute_until), (RAW, minute_until, self.now)])

	def test_hour_then_minute_then_raw(self):
		since = self.now - timedelta(days=7)
		minute_until, hour_until = datetime(2026, 10, 29, 12, 32), datetime(2026, 10, 29, 12)
		segs = storage.plan_segments(HOUR, since, self.now, minute_until, hour_until)
		self.assertEqual(
			segs,
			[
				(HOUR, datetime(2026, 10, 22, 13), hour_until),
				(MINUTE, hour_until, minute_until),
				(RAW, minute_until, self.now),
			],
		)

	def test_segments_are_contiguous(self):
		since = self.now - timedelta(days=3, minutes=17)
		segs = storage.plan_segments(HOUR, since, self.now, datetime(2026, 10, 29, 12, 30), datetime(2026, 10, 29, 9))
		for (_, _, end), (_, start, _) in zip(segs, segs[1:], strict=False):
			self.assertEqual(end, start)
		self.assertEqual(segs[-1], (RAW, datetime(2026, 10, 29, 12, 30), self.now))

	def test_nothing_rolled_yet_falls_back_to_raw(self):
		since = self.now - timedelta(days=7)
		self.assertEqual(storage.plan_segments(HOUR, since, self.now, None, None), [(RAW, since, self.now)])

	def test_hour_not_rolled_uses_minutes(self):
		since = self.now - timedelta(days=7)
		minute_until = datetime(2026, 10, 29, 12, 32)
		segs = storage.plan_segments(HOUR, since, self.now, minute_until, None)
		self.assertEqual(segs[0], (MINUTE, datetime(2026, 10, 22, 12, 35), minute_until))

	def test_watermark_before_window(self):
		since = self.now - timedelta(hours=24)
		old = datetime(2026, 10, 1)
		self.assertEqual(storage.plan_segments(MINUTE, since, self.now, old, old), [(RAW, since, self.now)])


class TestLateRestart(unittest.TestCase):
	now = datetime(2026, 10, 29, 12, 34, 56)

	def test_on_time_samples_do_not_move_watermarks(self):
		recent = self.now - timedelta(seconds=90)
		ts = [self.now - timedelta(seconds=s) for s in (0, 30, 60)]
		self.assertIsNone(storage.late_restart(ts, recent, self.now - timedelta(hours=47), MINUTE))

	def test_late_samples_restart_from_their_bucket(self):
		recent = self.now - timedelta(seconds=90)
		ts = [self.now, datetime(2026, 10, 29, 9, 15, 20), datetime(2026, 10, 29, 10, 1)]
		oldest = self.now - timedelta(hours=47)
		self.assertEqual(storage.late_restart(ts, recent, oldest, MINUTE), datetime(2026, 10, 29, 9, 15))
		self.assertEqual(storage.late_restart(ts, recent, oldest, HOUR), datetime(2026, 10, 29, 9))

	def test_too_old_samples_are_ignored(self):
		recent = self.now - timedelta(seconds=90)
		oldest = self.now - timedelta(hours=47)
		self.assertIsNone(storage.late_restart([self.now - timedelta(days=3)], recent, oldest, MINUTE))

	def test_samples_behind_the_watermark_are_late(self):
		# DST fall-back (Europe/Paris, 03:00 -> 02:00): the watermark is at 02:58 and the clock
		# reads 02:05 again; fresh samples of the repeated hour land in rolled buckets
		now = datetime(2026, 10, 25, 2, 5, 10)
		recent = now - timedelta(seconds=90)
		oldest = now - timedelta(hours=47)
		ts = [now - timedelta(seconds=10)]
		self.assertIsNone(storage.late_restart(ts, recent, oldest, MINUTE))  # what `recent` alone sees
		watermark = datetime(2026, 10, 25, 2, 58)
		self.assertEqual(storage.late_restart(ts, recent, oldest, MINUTE, watermark), datetime(2026, 10, 25, 2, 5))
		self.assertEqual(storage.late_restart(ts, recent, oldest, HOUR, datetime(2026, 10, 25, 2)), None)
		self.assertEqual(storage.late_restart(ts, recent, oldest, HOUR, datetime(2026, 10, 25, 3)), datetime(2026, 10, 25, 2))

	def test_late_horizon(self):
		self.assertEqual(storage.late_horizon(self.now, 48), self.now - timedelta(hours=47))
		# a short raw retention keeps a window for late samples (was: none at 1 h)
		self.assertEqual(storage.late_horizon(self.now, 1), self.now - timedelta(minutes=45))
		recent = self.now - timedelta(seconds=90)
		late = [self.now - timedelta(minutes=5)]
		self.assertIsNotNone(storage.late_restart(late, recent, storage.late_horizon(self.now, 1), MINUTE))


class TestLastLabel(unittest.TestCase):
	def test_merge_keeps_the_label_of_the_latest_value(self):
		a = dict(storage.new_partial(91.0, T0), last_label="mount=/data")
		b = dict(storage.new_partial(40.0, T0 - timedelta(minutes=1)), last_label="mount=/")
		self.assertEqual(storage.merge(a, b)["last_label"], "mount=/data")
		self.assertEqual(storage.merge(b, a)["last_label"], "mount=/data")
		self.assertNotIn("last_label", storage.merge(storage.new_partial(1.0, T0), storage.new_partial(2.0, T0)))


class TestBucketPartials(unittest.TestCase):
	def test_raw_and_rollup_rows_merge_exactly(self):
		g = ("h1",)
		raw = [(g, T0 + timedelta(minutes=5, seconds=s), storage.new_partial(v, T0 + timedelta(minutes=5, seconds=s)))
			   for s, v in ((10, 4.0), (40, 8.0))]  # fmt: skip
		rollup = [(g, T0, {"count": 2, "sum": 10.0, "min": 1.0, "max": 9.0, "last": 9.0, "last_ts": T0 + timedelta(seconds=50)})]
		cells = storage.bucket_partials(raw + rollup, 600)
		(key, part), = cells.items()
		self.assertEqual(key, (g, T0))
		self.assertEqual((part["count"], part["sum"], part["min"], part["max"], part["last"]), (4, 22.0, 1.0, 9.0, 8.0))

	def test_groups_and_buckets_stay_apart(self):
		rows = [(("a",), T0, storage.new_partial(1.0, T0)), (("b",), T0, storage.new_partial(2.0, T0)),
				(("a",), T0 + timedelta(minutes=10), storage.new_partial(3.0, T0 + timedelta(minutes=10)))]  # fmt: skip
		self.assertEqual(len(storage.bucket_partials(rows, 600)), 3)


class TestWeightedSeries(unittest.TestCase):
	def test_rollup_rows_average_by_samples(self):
		rows = [
			{"metric_name": "cpu_usage", "label_key": None, "timestamp": T0, "value": 30.0, "count": 3},  # 3 samples, avg 10
			{"metric_name": "cpu_usage", "label_key": None, "timestamp": T0 + timedelta(seconds=30), "value": 50.0},  # 1 raw
			{"metric_name": "http_502", "label_key": None, "timestamp": T0, "value": 12.0, "count": 4},
		]
		out = bucket_series(rows, 60)
		self.assertEqual(out["cpu_usage"], [["2026-10-29 02:00:00", 20.0]])
		self.assertEqual(out["http_502"], [["2026-10-29 02:00:00", 12.0]])


if __name__ == "__main__":
	unittest.main()
