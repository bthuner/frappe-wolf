import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import catalog
from frappe_wolf.wolf.presets import PRESETS

NOW = datetime(2026, 10, 30, 12, 0)


def series(metric="disk_used_percent", ago=timedelta(seconds=20), **scope):
	return {"metric_name": metric, "last_at": NOW - ago if ago is not None else None, "host": "h1", "bench": None, "site": None, **scope}


class TestCatalog(unittest.TestCase):
	def test_every_metric_rule_preset_is_in_the_catalog(self):
		for p in PRESETS:
			if p.get("signal_type") == "metric":
				self.assertIn(p["metric_name"], catalog.CATALOG, p["rule_name"])

	def test_kinds(self):
		self.assertEqual(catalog.CATALOG["rq_failed_jobs_added"]["kind"], "interval_count")
		self.assertEqual(catalog.CATALOG["rq_failed_jobs"]["kind"], "gauge")  # registry size, not "failed lately"
		self.assertIn("part of 5xx", catalog.CATALOG["http_502"]["label"])

	def test_stale_after_follows_cadence(self):
		self.assertEqual(catalog.stale_after("cpu_usage"), timedelta(minutes=5))
		self.assertEqual(catalog.stale_after("tls_days_left"), timedelta(hours=3))


class TestState(unittest.TestCase):
	RULE = {"name": "R1", "metric_name": "disk_used_percent", "filter_host": None, "filter_bench": None, "filter_site": None}

	def test_missing_stale_and_unwatched_are_different(self):
		self.assertEqual(catalog.state(series(ago=None), NOW), "missing")
		self.assertEqual(catalog.state(series(ago=timedelta(minutes=6)), NOW), "stale")
		self.assertEqual(catalog.state(series(), NOW), "unwatched")
		# a slow signal is not stale after 6 minutes
		self.assertEqual(catalog.state(series("backup_age_hours", timedelta(minutes=12), site="a"), NOW), "unwatched")

	def test_watched_and_firing(self):
		self.assertEqual(catalog.state(series(), NOW, [self.RULE]), "watched")
		incident = {"alert_rule": "R1", "host": "h1", "bench": None, "site": None}
		self.assertEqual(catalog.state(series(), NOW, [self.RULE], [incident]), "firing")
		other_host = {**incident, "host": "h2"}
		self.assertEqual(catalog.state(series(), NOW, [self.RULE], [other_host]), "watched")

	def test_rule_filters_limit_where_it_applies(self):
		only_h2 = {**self.RULE, "filter_host": "h2"}
		self.assertFalse(catalog.rule_applies(only_h2, series()))
		self.assertEqual(catalog.state(series(), NOW, [only_h2]), "unwatched")
		self.assertFalse(catalog.rule_applies({**self.RULE, "metric_name": "cpu_usage"}, series()))

	def test_firing_stays_firing_when_samples_stop(self):
		# a host that went silent mid-incident stays on top, and says its data stopped
		incident = {"alert_rule": "R1", "host": "h1"}
		silent = series(ago=timedelta(hours=1))
		self.assertEqual(catalog.state(silent, NOW, [self.RULE], [incident]), "firing")
		self.assertTrue(catalog.is_stale(silent, NOW))

	def test_no_recent_value_is_stale(self):
		# TLS last seen 2.5 h ago: under its 3 h cadence, but no value to show as current
		tls = series("tls_days_left", timedelta(hours=2, minutes=30), site="a", recent=False)
		self.assertEqual(catalog.state(tls, NOW), "stale")

	def test_label_incident_concerns_its_label_only(self):
		incident = {"alert_rule": "R1", "host": "h1", "group_key": "host=h1,label_key=mount=/"}
		root, data = series(label_key="mount=/"), series(label_key="mount=/data")
		self.assertEqual(catalog.state(root, NOW, [self.RULE], [incident]), "firing")
		self.assertEqual(catalog.state(data, NOW, [self.RULE], [incident]), "watched")
		host_wide = {"alert_rule": "R1", "host": "h1", "group_key": "host=h1"}
		self.assertEqual(catalog.state(data, NOW, [self.RULE], [host_wide]), "firing")


if __name__ == "__main__":
	unittest.main()
