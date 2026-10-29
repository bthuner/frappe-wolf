import unittest
from datetime import datetime

from frappe_wolf.wolf import public_status as ps

T0 = datetime(2026, 10, 29, 2, 0, 0)


def row(**kw):
	base = {"public_title": None, "severity": "warning", "status": "Open", "started_at": T0, "resolved_at": None}
	return {**base, **kw}


class TestSlug(unittest.TestCase):
	def test_normalize(self):
		self.assertEqual(ps.normalize_slug("My Site.example.com"), "my-site-example-com")
		self.assertEqual(ps.normalize_slug("  --ACME__status--  "), "acme-status")
		self.assertEqual(ps.normalize_slug("../../etc/passwd"), "etc-passwd")
		self.assertEqual(ps.normalize_slug("<script>"), "script")
		self.assertEqual(ps.normalize_slug("!!!"), "")
		self.assertEqual(ps.normalize_slug(None), "")
		self.assertEqual(len(ps.normalize_slug("a" * 200)), 64)

	def test_history_days(self):
		self.assertEqual(ps.history_days(None), 7)
		self.assertEqual(ps.history_days("14"), 14)
		self.assertEqual(ps.history_days(-3), 0)
		self.assertEqual(ps.history_days(10000), ps.MAX_HISTORY_DAYS)


class TestPublicIncident(unittest.TestCase):
	def test_only_public_fields(self):
		r = row(public_title="Checkout is slow", host="db-1", bench="b1", summary="traceback", title="disk / on db-1")
		item = ps.public_incident(r)
		self.assertEqual(set(item), {"title", "severity", "status", "started_at", "resolved_at"})
		self.assertEqual(item["title"], "Checkout is slow")
		self.assertNotIn("db-1", repr(item))

	def test_generic_title_without_public_title(self):
		self.assertEqual(ps.public_incident(row(severity="critical"))["title"], "Service disruption")
		self.assertEqual(ps.public_incident(row(public_title="   "))["title"], "Degraded performance")
		self.assertEqual(ps.public_incident(row(severity="bogus"))["severity"], "warning")

	def test_status_labels_and_times(self):
		self.assertEqual(ps.public_incident(row())["status"], "Investigating")
		self.assertEqual(ps.public_incident(row(status="Acknowledged"))["status"], "Identified")
		done = ps.public_incident(row(status="Resolved", resolved_at=datetime(2026, 10, 29, 3, 15)))
		self.assertEqual((done["status"], done["started_at"], done["resolved_at"]), ("Resolved", "2026-10-29 02:00", "2026-10-29 03:15"))
		# a resolved_at left on a re-opened incident is not shown
		self.assertEqual(ps.public_incident(row(resolved_at=T0))["resolved_at"], "")


class TestView(unittest.TestCase):
	def test_operational(self):
		v = ps.build_view({"title": "ACME", "show_history_days": 7}, [], [])
		self.assertEqual((v["overall_state"], v["current"], v["history"]), ("operational", [], []))
		self.assertEqual(v["status_title"], "ACME")

	def test_worst_open_incident_sets_banner_and_min_severity_filters(self):
		open_rows = [row(severity="info"), row(severity="critical"), row(severity="warning")]
		v = ps.build_view({"title": "ACME"}, open_rows, [])
		self.assertEqual(v["overall_state"], "outage")
		self.assertEqual([i["severity"] for i in v["current"]], ["critical", "warning"])
		v = ps.build_view({"title": "ACME", "min_severity": "info"}, [row(severity="info")], [])
		self.assertEqual((v["overall_state"], len(v["current"])), ("operational", 1))
		v = ps.build_view({"title": "ACME", "min_severity": "critical"}, [row(severity="warning")], [])
		self.assertEqual(v["overall_state"], "operational")

	def test_history_grouped_by_day_newest_first(self):
		hist = [
			row(status="Resolved", started_at=datetime(2026, 10, 27, 9, 0)),
			row(status="Resolved", started_at=datetime(2026, 10, 28, 9, 0)),
			row(status="Resolved", started_at=datetime(2026, 10, 28, 7, 0), severity="info"),
		]
		v = ps.build_view({"title": "x", "show_history_days": 3}, [], hist)
		self.assertEqual([d["day"] for d in v["history"]], ["2026-10-28", "2026-10-27"])
		self.assertEqual(len(v["history"][0]["incidents"]), 1)
		self.assertEqual(v["history_days"], 3)


if __name__ == "__main__":
	unittest.main()
