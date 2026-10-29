import unittest

from frappe_wolf.wolf import conditions
from frappe_wolf.wolf.presets import PRESETS

EVENT_TYPES = {"error", "cron_failure", "worker_failure", "security", "host_metric_alert", "deployment"}


class TestPresets(unittest.TestCase):
	def test_every_preset_is_valid(self):
		names = [p["rule_name"] for p in PRESETS]
		self.assertEqual(len(names), len(set(names)), "duplicate preset names")
		for p in PRESETS:
			with self.subTest(p["rule_name"]):
				self.assertLessEqual(len(p["rule_name"]), 140)
				self.assertNotRegex(p["rule_name"], r"[<>%]")  # used as the document name
				self.assertIn(p["operator"], conditions.OPERATORS)
				conditions.group_columns(p["group_by"])
				self.assertGreaterEqual(p["window_minutes"], 1)
				self.assertIn(p["severity"], ("info", "warning", "critical"))
				if p["signal_type"] == "metric":
					self.assertIn(p["aggregation"], conditions.AGGREGATIONS)
					self.assertTrue(p["metric_name"])
				else:
					self.assertIn(p["event_type"], EVENT_TYPES)

	def test_signal_presets(self):
		by_name = {p["rule_name"]: p for p in PRESETS}
		self.assertEqual(by_name["No backup for 26 h"]["threshold"], 26)
		tls = [p for p in PRESETS if p.get("metric_name") == "tls_days_left"]
		self.assertEqual(sorted((p["threshold"], p["severity"]) for p in tls), [(3, "critical"), (14, "warning")])
		# TLS is sampled hourly: "last" over a window longer than the check interval
		self.assertTrue(all(p["aggregation"] == "last" and p["window_minutes"] > 60 for p in tls))
		for metric in ("rq_failed_jobs_added", "rq_long_running_jobs", "rq_workers", "redis_used_memory_percent",
		               "redis_evicted_keys", "backup_age_hours", "db_connections_used_percent", "db_slow_queries",
		               "db_replication_lag_seconds", "db_replication_running", "bench_processes_down"):  # fmt: skip
			self.assertTrue(any(p.get("metric_name") == metric for p in PRESETS), metric)
		for category in ("backup_failed", "email_queue_backlog", "email_queue_error", "admin_login_new_country"):
			self.assertTrue(any(p.get("event_category") == category for p in PRESETS), category)

	def test_low_is_bad_presets_breach_when_value_drops(self):
		workers = next(p for p in PRESETS if p.get("metric_name") == "rq_workers")
		self.assertTrue(conditions.is_breached({"value": 0, "samples": 3}, workers["operator"], workers["threshold"], 3))
		self.assertFalse(conditions.is_breached({"value": 2, "samples": 3}, workers["operator"], workers["threshold"], 3))


if __name__ == "__main__":
	unittest.main()
