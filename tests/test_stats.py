import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import stats

T0 = datetime(2026, 10, 1, 2, 0, 0)


def inc(site, ack=None, res=None, status=None, **kw):
	return {
		"site": site,
		"started_at": T0,
		"acknowledged_at": T0 + timedelta(minutes=ack) if ack is not None else None,
		"resolved_at": T0 + timedelta(minutes=res) if res is not None else None,
		"status": status or ("Resolved" if res is not None else "Open"),
		**kw,
	}


class TestPercentile(unittest.TestCase):
	def test_numpy_linear_method(self):
		self.assertIsNone(stats.percentile([], 50))
		self.assertEqual(stats.percentile([7], 90), 7.0)
		self.assertEqual(stats.percentile([1, 2, 3, 4], 50), 2.5)
		self.assertAlmostEqual(stats.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 9.1)
		self.assertEqual(stats.percentile([10, 1, 5], 0), 1.0)
		self.assertEqual(stats.percentile([10, 1, 5], 100), 10.0)
		self.assertEqual(stats.percentile([None, 3, 1], 50), 2.0)

	def test_summarize(self):
		self.assertEqual(stats.summarize([]), {"count": 0, "avg": None, "p50": None, "p90": None})
		self.assertEqual(stats.summarize([10, 20, None]), {"count": 2, "avg": 15.0, "p50": 15.0, "p90": 19.0})


class TestDurations(unittest.TestCase):
	def test_durations(self):
		self.assertEqual(stats.durations(inc("a", ack=5, res=30)), (5.0, 30.0))
		self.assertEqual(stats.durations(inc("a")), (None, None))
		# resolved_at on a re-opened incident does not count
		self.assertEqual(stats.durations(inc("a", res=30, status="Open")), (None, None))
		# negative durations (clock trouble) are ignored
		self.assertEqual(stats.durations(inc("a", ack=-3, res=-1)), (None, None))


class TestGroupMetrics(unittest.TestCase):
	def test_group_by_site(self):
		rows = [
			inc("a", ack=2, res=10),
			inc("a", ack=4, res=30),
			inc("a", res=50),  # never acknowledged
			inc("b", ack=1),  # still open (acknowledged)
			inc(None, res=5),
		]
		out = stats.group_metrics(rows, "site")
		self.assertEqual([g["group"] for g in out], ["a", "(none)", "b"])
		a = out[0]
		self.assertEqual(
			(a["incidents"], a["open"], a["acknowledged"], a["resolved"]), (3, 0, 2, 3)
		)
		self.assertEqual((a["mtta"], a["mtta_p50"], a["mtta_p90"]), (3.0, 3.0, 3.8))
		self.assertEqual((a["mttr"], a["mttr_p50"], a["mttr_p90"]), (30.0, 30.0, 46.0))
		b = out[2]
		self.assertEqual((b["open"], b["mtta"], b["mttr"]), (1, 1.0, None))

	def test_group_by_rule_and_severity(self):
		rows = [inc("a", res=10, alert_rule="r1", severity="critical"), inc("a", res=20, alert_rule="r1", severity="info")]
		self.assertEqual(stats.group_metrics(rows, "alert_rule")[0]["mttr"], 15.0)
		self.assertEqual(sorted(g["group"] for g in stats.group_metrics(rows, "severity")), ["critical", "info"])
		with self.assertRaises(ValueError):
			stats.group_metrics(rows, "payload")

	def test_overall(self):
		rows = [inc("a", res=10), inc("b", res=30)]
		total = stats.overall(rows)
		self.assertEqual((total["group"], total["incidents"], total["mttr"]), ("Total", 2, 20.0))
		self.assertIsNone(stats.overall([]))
		self.assertEqual(rows[0]["site"], "a")  # input untouched


if __name__ == "__main__":
	unittest.main()
