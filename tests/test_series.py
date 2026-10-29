import unittest
from datetime import datetime

from frappe_wolf.wolf.series import bucket_series, bucket_start


def row(name, ts, value, label=None):
	return {"metric_name": name, "label_key": label, "timestamp": datetime.fromisoformat(ts), "value": value}


class TestSeries(unittest.TestCase):
	def test_bucket_start(self):
		self.assertEqual(bucket_start(datetime(2026, 10, 29, 2, 7, 59), 300), datetime(2026, 10, 29, 2, 5))
		self.assertEqual(bucket_start(datetime(2026, 10, 29, 2, 0, 0), 300), datetime(2026, 10, 29, 2, 0))

	def test_gauges_average_counters_sum(self):
		rows = [
			row("cpu_usage", "2026-10-29 02:00:10", 10),
			row("cpu_usage", "2026-10-29 02:00:40", 30),
			row("cpu_usage", "2026-10-29 02:01:05", 50),
			row("http_502", "2026-10-29 02:00:10", 4),
			row("http_502", "2026-10-29 02:00:40", 6),
		]
		out = bucket_series(rows, 60)
		self.assertEqual(out["cpu_usage"], [["2026-10-29 02:00:00", 20.0], ["2026-10-29 02:01:00", 50.0]])
		self.assertEqual(out["http_502"], [["2026-10-29 02:00:00", 10.0]])

	def test_labels_split_series_and_sorting(self):
		rows = [
			row("disk_used_percent", "2026-10-29 02:02:00", 91.5, "mount=/"),
			row("disk_used_percent", "2026-10-29 02:00:00", 90.0, "mount=/"),
			row("disk_used_percent", "2026-10-29 02:00:00", 40.0, "mount=/data"),
		]
		out = bucket_series(rows, 60)
		self.assertEqual([p[0] for p in out["disk_used_percent [mount=/]"]], ["2026-10-29 02:00:00", "2026-10-29 02:02:00"])
		self.assertEqual(out["disk_used_percent [mount=/data]"], [["2026-10-29 02:00:00", 40.0]])


if __name__ == "__main__":
	unittest.main()
