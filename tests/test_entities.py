import unittest

from frappe_wolf.wolf import entities

ALIASES = entities.Aliases([
	("host", "172.18.0.14", "bench-01"), ("host", "10.0.0.7", "erp-web02"), ("host", "erp-web02.prod.example.com", "erp-web02"),
	("host", "WAZUH-ERP02", "erp-web02"), ("site", "erp.example.com", "erp.localhost"), ("site", "www.example.com", "erp.localhost"),
])  # fmt: skip


class TestExtract(unittest.TestCase):
	def test_wolf_web_probe(self):
		got = entities.extract({"host": "Bench-01", "site": "erp.localhost"}, {"ip": "::ffff:45.33.32.156", "users": ["Jane@Example.com", "-"]}, ALIASES)
		self.assertEqual(got, {"ip": ["45.33.32.156"], "host": ["bench-01"], "site": ["erp.localhost"], "user": ["jane@example.com"]})

	def test_suricata_alert(self):
		# a real eve.json alert: the remote client, our server as dest, the site from the HTTP host
		payload = {"ip": "45.33.32.156", "src_ip": "45.33.32.156", "dest_ip": "172.18.0.14", "http_hostname": "www.example.com:443"}
		got = entities.extract({"host": "bench-01", "site": None}, payload, ALIASES)
		self.assertEqual(got, {"ip": ["45.33.32.156"], "host": ["bench-01"], "site": ["erp.localhost"]})  # dest_ip is the host

	def test_wazuh_alert(self):
		payload = {"ip": "45.33.32.156", "user": "deploy", "wazuh_agent": {"id": "004", "name": "wazuh-erp02", "ip": "10.0.0.7"}}
		got = entities.extract({"host": "erp-web02.prod.example.com"}, payload, ALIASES)
		self.assertEqual(got, {"ip": ["45.33.32.156"], "host": ["erp-web02"], "user": ["deploy"]})  # three names, one host
		syslog = {"wazuh_agent": {"id": "000", "name": "manager"}, "user": "deploy"}
		self.assertEqual(entities.extract({"host": "web01"}, syslog, ALIASES), {"host": ["web01"], "user": ["deploy"]})

	def test_nothing_known(self):
		self.assertEqual(entities.extract({}, {"ip": "not an ip", "user": "unknown"}), {})
		self.assertEqual(entities.extract({"host": "x"}, None), {"host": ["x"]})

	def test_caps(self):
		payload = {"users": [f"u{i}" for i in range(30)]}
		self.assertEqual(len(entities.extract({}, payload)["user"]), entities.MAX_PER_KIND)


class TestFilters(unittest.TestCase):
	def test_parse(self):
		self.assertEqual(entities.parse_filter("ip:2001:DB8:0:0::1"), ("ip", "2001:db8::1"))
		self.assertEqual(entities.parse_filter("host: ERP-01 "), ("host", "erp-01"))
		self.assertEqual(entities.parse_filter("user:Jane@Example.com"), ("user", "jane@example.com"))
		for bad in ("ip:nope", "mac:aa", "host:", "nothing"):
			with self.assertRaises(ValueError):
				entities.parse_filter(bad)

	def test_aliases(self):
		self.assertEqual(ALIASES.host("WAZUH-ERP02"), "erp-web02")
		self.assertEqual(ALIASES.host("other"), "other")
		self.assertIsNone(ALIASES.host("192.0.2.1"))  # an unknown address is not a host name
		self.assertEqual(ALIASES.host_of_address("::ffff:10.0.0.7"), "erp-web02")
		self.assertIsNone(ALIASES.site("unknown.example.com"))


if __name__ == "__main__":
	unittest.main()
