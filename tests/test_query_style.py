"""Query options Frappe checks only inside a web request (db_smoke calls them outside one)."""

import pathlib
import re
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent / "frappe_wolf"
# frappe.get_all / get_list refuse backticks in order_by over HTTP ("invalid backtick notation"),
# found on the demo bench: raw SQL may quote columns, the ORM's order_by may not
ORDER_BY = re.compile(r"order_by\s*=\s*[\"'][^\"']*`")


class OrderByStyle(unittest.TestCase):
	def test_no_backticks_in_orm_order_by(self):
		found = [
			f"{path.relative_to(APP)}:{n}"
			for path in APP.rglob("*.py")
			for n, line in enumerate(path.read_text().splitlines(), 1)
			if ORDER_BY.search(line)
		]
		self.assertEqual(found, [])


if __name__ == "__main__":
	unittest.main()
