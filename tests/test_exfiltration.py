import unittest

from frappe_wolf.wolf import exfiltration as exf

MB = 1_000_000


class TestClassify(unittest.TestCase):
	def test_exports(self):
		got = exf.classify("/api/method/frappe.desk.reportview.export_query", "POST", {"doctype": "Customer"})
		self.assertEqual((got["kind"], got["label"], got["doctype"]), ("export", "report export", "Customer"))
		got = exf.classify("/api/method/frappe.desk.query_report.export_query", "POST", {"report_name": "General Ledger"})
		self.assertEqual(got["doctype"], "General Ledger")
		self.assertEqual(exf.classify("/", "POST", {"cmd": "frappe.core.doctype.data_export.exporter.export_data"})["label"], "Data Export")

	def test_data_import_template_counts_only_with_records(self):
		path = "/api/method/frappe.core.doctype.data_import.data_import.download_template"
		self.assertIsNone(exf.classify(path, "GET", {"doctype": "Item", "export_records": "blank_template"}))
		self.assertEqual(exf.classify(path, "GET", {"doctype": "Item", "export_records": "all"})["kind"], "export")

	def test_list_reads_whatever_the_page_size(self):
		self.assertEqual(exf.classify("/api/method/frappe.client.get_list", "GET", {"doctype": "Customer", "limit_page_length": 20})["kind"], "list")
		self.assertEqual(exf.classify("/api/method/frappe.desk.reportview.get", "POST", {"doctype": "Item"})["label"], "list view")
		rest = exf.classify("/api/resource/Sales Invoice", "GET", {"limit_page_length": 0})
		self.assertEqual((rest["kind"], rest["doctype"]), ("list", "Sales Invoice"))
		self.assertEqual(exf.classify("/api/v2/document/Item/", "GET", {})["doctype"], "Item")

	def test_not_taking_data_out(self):
		self.assertIsNone(exf.classify("/api/resource/Customer/CUST-0001", "GET", {}))  # one document
		self.assertIsNone(exf.classify("/api/resource/Customer", "POST", {}))  # a create
		self.assertIsNone(exf.classify("/api/method/frappe.client.get_count", "GET", {"doctype": "Customer"}))
		self.assertIsNone(exf.classify("/app/customer", "GET", {}))


class TestRows(unittest.TestCase):
	def test_rows_in(self):
		self.assertEqual(exf.rows_in([{"name": 1}, {"name": 2}]), 2)
		self.assertEqual(exf.rows_in({"keys": ["name"], "values": [[1], [2], [3]]}), 3)
		self.assertIsNone(exf.rows_in("file contents"))


class TestVerdict(unittest.TestCase):
	def test_floor(self):
		self.assertIsNone(exf.verdict(49 * MB, None, 50 * MB))  # below the floor: even without history

	def test_against_the_users_largest_hour(self):
		self.assertIsNone(exf.verdict(200 * MB, 50 * MB, 50 * MB))  # 4x
		self.assertEqual(exf.verdict(250 * MB, 50 * MB, 50 * MB), "warning")  # 5x
		self.assertEqual(exf.verdict(1000 * MB, 50 * MB, 50 * MB), "critical")  # 20x
		self.assertEqual(exf.verdict(100 * MB, 10 * MB, 50 * MB, factor=10), "warning")

	def test_without_history(self):
		self.assertEqual(exf.verdict(60 * MB, None, 50 * MB), "critical")
		self.assertEqual(exf.verdict(60 * MB, 0, 50 * MB), "critical")
		# the site itself has less than 14 days of history: every user lacks one
		self.assertEqual(exf.verdict(60 * MB, None, 50 * MB, learning=True), "warning")


if __name__ == "__main__":
	unittest.main()
