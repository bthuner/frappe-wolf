import json
import unittest
from datetime import datetime

from frappe_wolf.wolf import attack, schema

NOW = datetime(2026, 10, 29, 3, 0, 0)


class TestAttack(unittest.TestCase):
	def test_every_mapped_technique_is_named(self):
		for category, ids in attack.CATEGORIES.items():
			for tid in ids:
				with self.subTest(category=category, technique=tid):
					self.assertIn(tid, attack.TECHNIQUES)

	def test_enrich_merges_agent_techniques(self):
		payload = attack.enrich("web_attack_probe", {"count": 3, "attack_techniques": ["t1190", "T1595.003", "bogus"]})
		self.assertEqual([t["id"] for t in payload["mitre_attack"]], ["T1595.003", "T1190"])
		self.assertEqual(payload["mitre_attack"][1]["name"], "Exploit Public-Facing Application")
		self.assertEqual(payload["mitre_attack"][1]["tactic"], "initial-access")
		self.assertEqual(payload["count"], 3)

	def test_unknown_ids_kept_unnamed(self):
		payload = attack.enrich("custom", {"attack_techniques": ["T1059.004"]})
		self.assertEqual(payload["mitre_attack"], [{"id": "T1059.004", "name": None, "tactic": None}])

	def test_nothing_to_add(self):
		self.assertIsNone(attack.enrich("custom", None))
		self.assertEqual(attack.enrich("custom", {"a": 1}), {"a": 1})
		own = {"mitre_attack": [{"id": "T9999"}]}
		self.assertIs(attack.enrich("ssh_failed_login", own), own)
		self.assertEqual(attack.enrich("ssh_failed_login", None)["mitre_attack"][0]["id"], "T1110")

	def test_normalize_event_enriches_security_only(self):
		sec = schema.normalize_event({"type": "security", "category": "admin_group_change", "host": "h"}, "UTC", NOW)
		self.assertEqual(json.loads(sec["payload"])["mitre_attack"][0]["id"], "T1098")
		err = schema.normalize_event({"type": "error", "category": "ssh_failed_login", "host": "h"}, "UTC", NOW)
		self.assertIsNone(err["payload"])


if __name__ == "__main__":
	unittest.main()
