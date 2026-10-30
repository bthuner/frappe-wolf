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


class TestExplore(unittest.TestCase):
	def test_aggregations_follow_the_kind(self):
		self.assertEqual(catalog.validate("disk_used_percent", None, None), ("avg", "none"))
		self.assertEqual(catalog.validate("http_5xx", None, None), ("sum", "none"))
		with self.assertRaises(ValueError):
			catalog.validate("disk_used_percent", "sum", None)  # a sum of disk levels means nothing
		with self.assertRaises(ValueError):
			catalog.validate("http_5xx", "last", None)  # one push's count is not "the" count

	def test_grouping_follows_the_level_and_label(self):
		self.assertIn("host_label", catalog.describe("disk_used_percent")["group_bys"])
		self.assertEqual(catalog.describe("disk_used_percent")["label_key"], "mount")
		with self.assertRaises(ValueError):
			catalog.validate("cpu_usage", "avg", "site")  # a host metric has no site
		self.assertEqual(catalog.validate("http_requests", "sum", "site"), ("sum", "site"))

	def test_custom_metric_is_read_as_a_level(self):
		d = catalog.describe("my_custom_gauge")
		self.assertEqual((d["kind"], d["group"], d["default_aggregation"]), ("unknown", "custom", "avg"))
		with self.assertRaises(ValueError):
			catalog.validate("my_custom_gauge", "sum", None)

	def test_every_metric_the_agent_sends_is_catalogued(self):
		import pathlib
		import re

		src = "".join(p.read_text() for p in pathlib.Path(__file__).parents[1].joinpath("agent/wolf_agent").rglob("*.py"))
		sent = set(re.findall(r'(?:metric|add)\("([a-z0-9_]+)"', src)) | {"memory_used_percent", "swap_used_percent"}
		self.assertEqual(sent - set(catalog.CATALOG), set())

	def test_every_catalog_metric_describes(self):
		for m in catalog.CATALOG:
			d = catalog.describe(m)
			self.assertIn(d["default_aggregation"], d["aggregations"], m)


if __name__ == "__main__":
	unittest.main()
