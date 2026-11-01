import base64
import json
import unittest

from frappe_wolf.wolf import reputation

FIREHOL = """# FireHOL level 1
#
0.0.0.0/8
192.0.2.0/24
198.51.100.7
203.0.113.0/25
not an address
"""
DROP = "; Spamhaus DROP\n203.0.113.0/24 ; SBL123\n2001:db8::/32 ; SBL456\n"


class TestIndex(unittest.TestCase):
	def setUp(self):
		self.index = reputation.Index({"firehol_level1": reputation.parse_list(FIREHOL),
		                               "spamhaus_drop": reputation.parse_list(DROP)})  # fmt: skip

	def test_parse_skips_comments_and_garbage(self):
		self.assertEqual(len(reputation.parse_list(FIREHOL)), 4)
		self.assertEqual([str(n) for n in reputation.parse_list(DROP)], ["203.0.113.0/24", "2001:db8::/32"])

	def test_lookup(self):
		self.assertEqual(self.index.lookup("192.0.2.200"), ["firehol_level1"])
		self.assertEqual(self.index.lookup("198.51.100.7"), ["firehol_level1"])
		self.assertEqual(self.index.lookup("198.51.100.8"), [])
		self.assertEqual(self.index.lookup("203.0.113.5"), ["firehol_level1", "spamhaus_drop"])
		self.assertEqual(self.index.lookup("203.0.113.200"), ["spamhaus_drop"])
		self.assertEqual(self.index.lookup("2001:db8::1"), ["spamhaus_drop"])
		self.assertEqual(self.index.lookup("::ffff:192.0.2.1"), ["firehol_level1"])
		self.assertEqual(self.index.lookup("nonsense"), [])

	def test_overlapping_ranges_merge(self):
		index = reputation.Index({"x": reputation.parse_list("10.0.0.0/24\n10.0.0.128/25\n10.0.1.0/24\n")})
		self.assertEqual(len(index.ranges[("x", 4)][0]), 1)
		self.assertEqual(index.lookup("10.0.1.255"), ["x"])
		self.assertEqual(index.lookup("10.0.2.0"), [])

	def test_json_round_trip(self):
		again = reputation.Index.from_json(json.loads(json.dumps(self.index.to_json())))
		self.assertEqual(again.lookup("203.0.113.5"), ["firehol_level1", "spamhaus_drop"])
		self.assertEqual(again.names(), ["firehol_level1", "spamhaus_drop"])

	def test_tag(self):
		payload, severity = reputation.tag({"ip": "192.0.2.9", "count": 3}, "warning", self.index)
		self.assertEqual(payload["reputation"], {"listed": True, "lists": ["firehol_level1"]})
		self.assertEqual(severity, "critical")
		self.assertEqual(reputation.tag({"ip": "192.0.2.9"}, "info", self.index)[1], "warning")
		same = {"ip": "8.8.8.8"}
		self.assertIs(reputation.tag(same, "warning", self.index)[0], same)
		self.assertEqual(reputation.tag(None, "warning", self.index), (None, "warning"))
		self.assertEqual(reputation.tag({"ip": "192.0.2.9"}, "warning", None), ({"ip": "192.0.2.9"}, "warning"))


class TestSettingsHelpers(unittest.TestCase):
	def test_parse_sources(self):
		text = "firehol_level1 https://iplists.firehol.org/files/firehol_level1.netset\n# off\nhttps://x.example/list.txt\nlocal private/files/bad.txt"
		self.assertEqual(reputation.parse_sources(text), [
			("firehol_level1", "https://iplists.firehol.org/files/firehol_level1.netset"),
			("list", "https://x.example/list.txt"),
			("local", "private/files/bad.txt"),
		])  # fmt: skip

	def test_api_key_from_header(self):
		self.assertEqual(reputation.api_key_from_header("token abc123:secret"), "abc123")
		basic = base64.b64encode(b"abc123:secret").decode()
		self.assertEqual(reputation.api_key_from_header(f"Basic {basic}"), "abc123")
		self.assertIsNone(reputation.api_key_from_header("Bearer xyz"))
		self.assertIsNone(reputation.api_key_from_header("Basic !!!"))
		self.assertIsNone(reputation.api_key_from_header(None))

	def test_lines(self):
		self.assertEqual(reputation.lines(" a \n\n# c\nb"), ["a", "b"])


if __name__ == "__main__":
	unittest.main()
