import unittest
from datetime import datetime

from frappe_wolf.wolf import compliance

AWARE = datetime(2026, 11, 1, 9, 0)


def by_id(rows):
	return {r["id"]: r for r in rows}


class TestDeadlines(unittest.TestCase):
	def test_nothing_without_a_flag(self):
		self.assertEqual(compliance.deadlines({"nis2_aware_at": AWARE, "gdpr_aware_at": AWARE}, AWARE), [])

	def test_nis2_clock(self):
		rows = by_id(compliance.deadlines({"nis2_significant": 1, "nis2_aware_at": AWARE}, datetime(2026, 11, 1, 12, 0)))
		self.assertEqual(rows["nis2_early_warning"]["due_at"], datetime(2026, 11, 2, 9, 0))
		self.assertEqual(rows["nis2_notification"]["due_at"], datetime(2026, 11, 4, 9, 0))
		final = rows["nis2_final_report"]
		self.assertEqual(final["due_at"], datetime(2026, 12, 4, 9, 0))  # from the notification's deadline
		self.assertTrue(final["estimated"])
		self.assertEqual({r["status"] for r in rows.values()}, {"open"})

	def test_final_report_counts_from_the_notification_sent(self):
		inc = {"nis2_significant": 1, "nis2_aware_at": AWARE, "nis2_notification_at": "2026-11-02T15:30:00"}
		final = by_id(compliance.deadlines(inc, AWARE))["nis2_final_report"]
		self.assertEqual(final["due_at"], datetime(2026, 12, 2, 15, 30))
		self.assertFalse(final["estimated"])

	def test_statuses(self):
		inc = {"nis2_significant": 1, "nis2_aware_at": AWARE, "nis2_early_warning_at": datetime(2026, 11, 2, 10, 0)}
		rows = by_id(compliance.deadlines(inc, datetime(2026, 11, 3, 20, 0)))
		self.assertEqual(rows["nis2_early_warning"]["status"], "done_late")  # sent after 24 h
		self.assertEqual(rows["nis2_notification"]["status"], "open")  # 13 h left
		rows = by_id(compliance.deadlines(inc, datetime(2026, 11, 4, 5, 0)))
		self.assertEqual(rows["nis2_notification"]["status"], "due_soon")  # 4 h left
		rows = by_id(compliance.deadlines(inc, datetime(2026, 11, 4, 10, 0)))
		self.assertEqual(rows["nis2_notification"]["status"], "overdue")

	def test_gdpr(self):
		rows = compliance.deadlines({"gdpr_breach": 1, "gdpr_aware_at": AWARE, "gdpr_notified_at": datetime(2026, 11, 3, 9, 0)},
		                            datetime(2026, 11, 5, 0, 0))  # fmt: skip
		self.assertEqual([(r["id"], r["status"]) for r in rows], [("gdpr_notification", "done")])

	def test_alerts(self):
		inc = {"gdpr_breach": 1, "gdpr_aware_at": AWARE}
		self.assertEqual(compliance.alerts(inc, datetime(2026, 11, 3, 9, 0)), [])
		((row, stage),) = compliance.alerts(inc, datetime(2026, 11, 4, 4, 0))
		self.assertEqual((row["id"], stage), ("gdpr_notification", "due"))
		((row, stage),) = compliance.alerts(inc, datetime(2026, 11, 4, 9, 1))
		self.assertEqual(stage, "overdue")

	def test_add_months_clamps(self):
		self.assertEqual(compliance.add_months(datetime(2027, 1, 31, 8, 0)), datetime(2027, 2, 28, 8, 0))
		self.assertEqual(compliance.add_months(datetime(2026, 12, 15)), datetime(2027, 1, 15))


class TestReport(unittest.TestCase):
	def test_markdown(self):
		inc = {"name": "WINC-00042", "title": "Ransomware on bench-01", "severity": "critical", "status": "Resolved",
		       "site": "erp.example.com", "started_at": AWARE, "nis2_significant": 1, "nis2_aware_at": AWARE,
		       "regulatory_reference": "CERT-FR 2026-123", "summary": "Encrypted files on the host."}  # fmt: skip
		rows = compliance.deadlines(inc, datetime(2026, 11, 1, 10, 0))
		pm = {"impact": "3 h of downtime", "action_items": [{"description": "Rotate keys", "status": "Done", "assigned_to": "ops@x"}]}
		text = compliance.report_markdown(inc, rows, [(AWARE, "Incident started")], pm, "Europe/Paris")
		self.assertIn("# Incident report: Ransomware on bench-01", text)
		self.assertIn("- Authority reference: CERT-FR 2026-123", text)
		self.assertIn("| Early warning to the CSIRT / authority (24 h) | 2026-11-02 09:00 | — | open |", text)
		self.assertIn("2026-12-04 09:00 (estimated)", text)
		self.assertIn("## Impact\n\n3 h of downtime", text)
		self.assertIn("- Rotate keys (Done, owner ops@x)", text)
		self.assertIn("- 2026-11-01 09:00 Incident started", text)
		self.assertIn("Times in Europe/Paris", text)

	def test_markdown_without_flags(self):
		text = compliance.report_markdown({"name": "WINC-1"}, [])
		self.assertIn("No regulatory flag is set on this incident.", text)


if __name__ == "__main__":
	unittest.main()
