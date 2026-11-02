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

	def test_nis2_intermediate_on_request(self):
		inc = {"nis2_significant": 1, "nis2_aware_at": AWARE}
		self.assertNotIn("nis2_intermediate", by_id(compliance.deadlines(inc, AWARE)))  # only when asked
		inc["nis2_intermediate_requested_at"] = datetime(2026, 11, 10, 9, 0)
		row = by_id(compliance.deadlines(inc, datetime(2026, 11, 20)))["nis2_intermediate"]
		self.assertEqual((row["due_at"], row["status"]), (None, "open"))  # no deadline given: nothing overdue
		self.assertNotIn("nis2_intermediate", [r["id"] for r, _ in compliance.alerts(inc, datetime(2026, 11, 20))])
		inc["nis2_intermediate_due_at"] = datetime(2026, 11, 12, 18, 0)
		row = by_id(compliance.deadlines(inc, datetime(2026, 11, 12, 13, 0)))["nis2_intermediate"]
		self.assertEqual((row["due_at"], row["status"]), (datetime(2026, 11, 12, 18, 0), "due_soon"))
		inc["nis2_intermediate_at"] = datetime(2026, 11, 12, 17, 0)
		self.assertEqual(by_id(compliance.deadlines(inc, datetime(2026, 11, 13)))["nis2_intermediate"]["status"], "done")

	def test_dora_initial_notification(self):
		aware = AWARE
		self.assertEqual(compliance.initial_due(aware, aware), datetime(2026, 11, 1, 13, 0))  # 4 h after classification
		self.assertEqual(compliance.initial_due(aware, datetime(2026, 11, 2, 7, 0)), datetime(2026, 11, 2, 9, 0))  # capped at 24 h
		self.assertEqual(compliance.initial_due(aware, datetime(2026, 11, 3, 9, 0)), datetime(2026, 11, 3, 13, 0))  # classified late
		self.assertEqual(compliance.initial_due(aware, datetime(2026, 11, 2, 9, 0)), datetime(2026, 11, 2, 9, 0))

	def test_dora_chain(self):
		inc = {"dora_major": 1, "dora_aware_at": AWARE, "dora_classified_at": datetime(2026, 11, 1, 10, 0)}
		rows = by_id(compliance.deadlines(inc, AWARE))
		self.assertEqual(rows["dora_initial"]["due_at"], datetime(2026, 11, 1, 14, 0))
		self.assertEqual((rows["dora_intermediate"]["due_at"], rows["dora_intermediate"]["estimated"]), (datetime(2026, 11, 4, 14, 0), True))
		self.assertEqual((rows["dora_final"]["due_at"], rows["dora_final"]["estimated"]), (datetime(2026, 12, 4, 14, 0), True))
		inc["dora_initial_at"] = datetime(2026, 11, 1, 12, 30)
		inc["dora_intermediate_at"] = datetime(2026, 11, 6, 8, 0)  # an updated intermediate report: the latest one counts
		rows = by_id(compliance.deadlines(inc, datetime(2026, 11, 6, 9, 0)))
		self.assertEqual(rows["dora_initial"]["status"], "done")
		self.assertEqual((rows["dora_intermediate"]["due_at"], rows["dora_intermediate"]["status"]), (datetime(2026, 11, 4, 12, 30), "done_late"))
		self.assertEqual((rows["dora_final"]["due_at"], rows["dora_final"]["estimated"]), (datetime(2026, 12, 6, 8, 0), False))
		self.assertEqual({r["regime"] for r in rows.values()}, {"DORA"})
		self.assertEqual(compliance.deadlines({**inc, "dora_classified_at": None}, AWARE), [])  # not classified as major yet

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

	def test_markdown_dora_and_ongoing(self):
		inc = {"name": "WINC-7", "status": "Open", "nis2_significant": 1, "nis2_aware_at": AWARE,
		       "nis2_intermediate_requested_at": datetime(2026, 11, 3, 9, 0), "dora_major": 1, "dora_aware_at": AWARE,
		       "dora_classified_at": AWARE}  # fmt: skip
		text = compliance.report_markdown(inc, compliance.deadlines(inc, AWARE))
		self.assertIn("- DORA major ICT-related incident, aware since 2026-11-01 09:00, classified as major on 2026-11-01 09:00", text)
		self.assertIn("- NIS2 intermediate report requested on 2026-11-03 09:00", text)
		self.assertIn("| Intermediate report requested by the CSIRT / authority | — | — | open |", text)
		self.assertIn("| Initial notification (4 h after classification, 24 h after awareness) | 2026-11-01 13:00 |", text)
		self.assertIn("Art. 23(4)(e)", text)  # still ongoing: progress report
		self.assertNotIn("Art. 23(4)(e)", compliance.report_markdown({**inc, "status": "Resolved"}, compliance.deadlines(inc, AWARE)))

	def test_markdown_without_flags(self):
		text = compliance.report_markdown({"name": "WINC-1"}, [])
		self.assertIn("No regulatory flag is set on this incident.", text)


if __name__ == "__main__":
	unittest.main()
