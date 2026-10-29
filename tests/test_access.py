import unittest

from frappe_wolf.wolf import agent_secrets, rate, scopes

EVENT = scopes.COLUMNS["Wolf Event"]
AGENT = scopes.COLUMNS["Wolf Agent"]


def quote(value):
	return "'" + value.replace("'", "''") + "'"


class TestGrants(unittest.TestCase):
	def test_build_grants(self):
		rows = [
			{"scope_type": "site", "value": "b.example.com"},
			{"scope_type": "site", "value": " a.example.com "},
			{"scope_type": "site", "value": "a.example.com"},
			{"scope_type": "host", "value": "bench-01"},
			{"scope_type": "nonsense", "value": "x"},
			{"scope_type": "bench", "value": ""},
		]
		self.assertEqual(scopes.build_grants(rows), {"site": ("a.example.com", "b.example.com"), "host": ("bench-01",)})

	def test_unrestricted_sees_everything(self):
		self.assertTrue(scopes.row_visible({"site": "z"}, None, EVENT))
		self.assertIsNone(scopes.sql_literal_condition(None, EVENT, "`tabWolf Event`", quote))
		self.assertEqual(scopes.sql_param_condition(None, EVENT), ("", {}))

	def test_row_visibility_is_an_or_over_scopes(self):
		g = {"site": ("a.example.com",), "host": ("bench-02",)}
		self.assertTrue(scopes.row_visible({"site": "a.example.com", "bench": "b", "host": "bench-01"}, g, EVENT))
		self.assertTrue(scopes.row_visible({"site": None, "host": "bench-02"}, g, EVENT))
		self.assertFalse(scopes.row_visible({"site": "b.example.com", "host": "bench-01"}, g, EVENT))
		# a host-wide event (no site) needs a host grant
		self.assertFalse(scopes.row_visible({"site": None, "host": "bench-01"}, {"site": ("a.example.com",)}, EVENT))

	def test_agents_match_on_hostname(self):
		g = {"site": ("a.example.com",), "host": ("bench-01",)}
		self.assertTrue(scopes.row_visible({"hostname": "bench-01"}, g, AGENT))
		self.assertTrue(scopes.row_visible({"hostname": "a.example.com"}, g, AGENT))  # satellite
		self.assertFalse(scopes.row_visible({"hostname": "bench-09"}, g, AGENT))
		self.assertFalse(scopes.row_visible({"hostname": "x"}, {"bench": ("b",)}, AGENT))

	def test_literal_condition(self):
		g = {"site": ("a.example.com", "o'brien.example.com"), "bench": ("b1",)}
		self.assertEqual(
			scopes.sql_literal_condition(g, EVENT, "`tabWolf Event`", quote),
			"(`tabWolf Event`.`bench` in ('b1') or `tabWolf Event`.`site` in ('a.example.com', 'o''brien.example.com'))",
		)
		# a grant type the doctype has no column for hides everything
		self.assertEqual(scopes.sql_literal_condition({"bench": ("b1",)}, AGENT, "`tabWolf Agent`", quote), "1=0")

	def test_agent_condition_merges_columns(self):
		g = {"site": ("a.example.com",), "host": ("bench-01",)}
		self.assertEqual(
			scopes.sql_literal_condition(g, AGENT, "`tabWolf Agent`", quote),
			"(`tabWolf Agent`.`hostname` in ('a.example.com', 'bench-01'))",
		)

	def test_param_condition(self):
		sql, params = scopes.sql_param_condition({"site": ("a",), "host": ("h1", "h2")}, EVENT)
		self.assertEqual(sql, " and (`host` in %(wolf_scope_0)s or `site` in %(wolf_scope_1)s)")
		self.assertEqual(params, {"wolf_scope_0": ("h1", "h2"), "wolf_scope_1": ("a",)})
		sql, _ = scopes.sql_param_condition({"bench": ("b",)}, AGENT, table="a")
		self.assertEqual(sql, " and (1=0)")
		sql, _ = scopes.sql_param_condition({"site": ("a",)}, EVENT, table="e")
		self.assertEqual(sql, " and (e.`site` in %(wolf_scope_0)s)")


class TestAgentSecrets(unittest.TestCase):
	def test_new_secret(self):
		s = agent_secrets.new_secret()
		self.assertEqual(len(s), 40)
		int(s, 16)
		self.assertNotEqual(s, agent_secrets.new_secret())

	def test_round_trip(self):
		secret = agent_secrets.new_secret()
		stored = agent_secrets.hash_secret(secret)
		self.assertTrue(stored.startswith("hmac-sha256$"))
		self.assertNotIn(secret, stored)
		self.assertTrue(agent_secrets.verify(secret, stored))
		self.assertFalse(agent_secrets.verify(secret[:-1] + ("0" if secret[-1] != "0" else "1"), stored))

	def test_salted(self):
		a, b = agent_secrets.hash_secret("same"), agent_secrets.hash_secret("same")
		self.assertNotEqual(a, b)
		self.assertTrue(agent_secrets.verify("same", a) and agent_secrets.verify("same", b))
		self.assertEqual(agent_secrets.hash_secret("same", salt="ab"), agent_secrets.hash_secret("same", salt="ab"))

	def test_rejects_missing_and_malformed(self):
		stored = agent_secrets.hash_secret("x" * 40)
		for secret, hashed in (
			("", stored),
			(None, stored),
			("x" * 40, None),
			("x" * 40, ""),
			("x" * 40, "x" * 40),  # a plaintext value is not a hash
			("x" * 40, "md5$aa$bb"),
			("x" * 40, "hmac-sha256$$" + "0" * 64),
		):
			self.assertFalse(agent_secrets.verify(secret, hashed), (secret, hashed))
		with self.assertRaises(ValueError):
			agent_secrets.hash_secret("")


class TestRateWindow(unittest.TestCase):
	def test_window_key(self):
		self.assertEqual(rate.window_key("WAG-0001", now=120.5), "wolf_push_rate:WAG-0001:2")
		self.assertEqual(rate.window_key("WAG-0001", now=179.9), rate.window_key("WAG-0001", now=120))
		self.assertNotEqual(rate.window_key("WAG-0001", now=180), rate.window_key("WAG-0001", now=179))
		self.assertNotEqual(rate.window_key("WAG-0001", now=1), rate.window_key("WAG-0002", now=1))


if __name__ == "__main__":
	unittest.main()
