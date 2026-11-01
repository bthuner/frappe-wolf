import unittest

from frappe_wolf.wolf import response


def ev(category, payload, agent="agent-1", event_type="security", host="bench-01"):
	return {"event_type": event_type, "category": category, "title": category, "agent": agent, "host": host,
	        "bench": None, "site": "erp.example.com", "payload": payload}  # fmt: skip


class TestChecks(unittest.TestCase):
	def test_blockable(self):
		self.assertEqual(response.blockable("45.33.32.156"), (True, None))
		self.assertEqual(response.blockable("2a03:2880:f12f:83:face:b00c:0:25de"), (True, None))
		for bad in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "::1", "fe80::1", "0.0.0.0", "198.51.100.7", "x", "::ffff:10.0.0.1"):
			with self.subTest(bad):
				self.assertFalse(response.blockable(bad)[0])

	def test_check(self):
		self.assertIsNone(response.check("block_ip", "45.33.32.156", hours=24))
		self.assertIn("1 to 168", response.check("block_ip", "45.33.32.156", hours=500))
		self.assertIn("private", response.check("block_ip", "10.1.2.3"))
		self.assertIn("protected", response.check("disable_user", "Administrator"))
		self.assertIn("own account", response.check("disable_user", "ops@example.com", approver="OPS@example.com"))
		self.assertIsNone(response.check("revoke_api_keys", "intruder@example.com", approver="ops@example.com"))
		self.assertIn("unknown action", response.check("rm_rf", "x"))
		self.assertEqual(response.check("block_ip", ""), "no target")


class TestSuggest(unittest.TestCase):
	def test_ip_blocks_need_the_agent_that_saw_them(self):
		events = [
			ev("ssh_failed_login", {"ip": "45.33.32.156"}),
			ev("ssh_failed_login", {"ip": "45.33.32.156"}),  # same address, same agent: once
			ev("web_attack_probe", {"ip": "45.33.32.156"}, agent="agent-2", host="bench-02"),
			ev("web_attack_probe", {"ip": "10.0.0.9"}),  # private: never proposed
			ev("frappe_failed_login", {"ip": "45.33.32.157", "user": "a@example.com"}, agent=None),  # no agent to block
			ev("traceback", {"ip": "45.33.32.158"}, event_type="error"),
		]
		blocks = [(p["target"], p["agent"]) for p in response.suggest(events) if p["action"] == "block_ip"]
		self.assertEqual(blocks, [("45.33.32.156", "agent-1"), ("45.33.32.156", "agent-2")])

	def test_user_actions_for_site_events(self):
		events = [
			ev("api_key_generated", {"user": "intruder@example.com"}, agent=None),
			ev("privileged_user", {"user": "Administrator"}, agent=None),  # protected
			ev("ssh_failed_login", {"user": "root", "ip": "45.33.32.156"}),  # host account, not a site user
		]
		users = [(p["action"], p["target"]) for p in response.suggest(events) if p["action"] != "block_ip"]
		self.assertEqual(users, [("disable_user", "intruder@example.com"), ("logout_sessions", "intruder@example.com"),
		                         ("revoke_api_keys", "intruder@example.com")])  # fmt: skip

	def test_bounded(self):
		events = [ev("web_attack_probe", {"ip": f"45.33.{i // 250}.{i % 250 + 1}"}) for i in range(100)]
		self.assertEqual(len(response.suggest(events)), response.MAX_PROPOSALS)

	def test_label(self):
		self.assertEqual(response.label("block_ip", "45.33.32.156", 24, "bench-01"), "Block 45.33.32.156 for 24 h on bench-01")


if __name__ == "__main__":
	unittest.main()
