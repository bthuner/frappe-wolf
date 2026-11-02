import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import api_keys

NOW = datetime(2026, 11, 2, 12, 0)
OLD = NOW - timedelta(days=3)


class TestApiKeyVerdict(unittest.TestCase):
	def test_network(self):
		self.assertEqual(str(api_keys.network("203.0.113.7")), "203.0.113.0/24")
		self.assertEqual(str(api_keys.network("2001:db8:1:2:3::4")), "2001:db8:1:2::/64")
		self.assertEqual(str(api_keys.network("::ffff:198.51.100.9")), "198.51.100.0/24")
		self.assertIsNone(api_keys.network("not an ip"))

	def test_baseline_and_learning(self):
		self.assertEqual(api_keys.verdict("203.0.113.7", [], None, NOW), "baseline")  # the key's first address
		recent = NOW - timedelta(hours=23)
		self.assertEqual(api_keys.verdict("198.51.100.1", ["203.0.113.7"], recent, NOW), "baseline")  # still learning
		self.assertEqual(api_keys.verdict("198.51.100.1", ["203.0.113.7"], None, NOW), "baseline")

	def test_after_learning(self):
		self.assertEqual(api_keys.verdict("203.0.113.99", ["203.0.113.7"], OLD, NOW), "same_network")
		self.assertEqual(api_keys.verdict("198.51.100.1", ["203.0.113.7"], OLD, NOW), "new")
		self.assertEqual(api_keys.verdict("2001:db8:1:2::99", ["2001:db8:1:2::1"], OLD, NOW), "same_network")
		self.assertEqual(api_keys.verdict("2001:db8:1:3::1", ["2001:db8:1:2::1", "bogus"], OLD, NOW), "new")
		self.assertEqual(api_keys.verdict("bogus", ["203.0.113.7"], OLD, NOW), "new")


if __name__ == "__main__":
	unittest.main()
