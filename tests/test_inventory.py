import json
import unittest
from datetime import datetime

from frappe_wolf.wolf import inventory, scopes

T1, T2 = datetime(2026, 10, 30, 10, 0), datetime(2026, 10, 30, 11, 0)

AGENTS = [
	{"hostname": "h1", "kind": "agent", "last_seen": T1,
	 "benches": json.dumps([{"name": "frappe-bench", "path": "/home/frappe/frappe-bench", "sites": ["a.example.com"]}])},
	# same bench name on another host: a different bench
	{"hostname": "h2", "kind": "agent", "last_seen": T1,
	 "benches": [{"name": "frappe-bench", "sites": ["b.example.com", "c.example.com"]}]},
	# a satellite's hostname is its site; its bench says which host it runs on
	{"hostname": "sat.example.com", "kind": "satellite", "last_seen": T2,
	 "benches": [{"name": "bench-x", "host": "h3", "sites": ["sat.example.com"]}]},
	{"hostname": "old.example.com", "kind": "satellite", "last_seen": None, "benches": "not json"},
]  # fmt: skip


def tree(triples):
	return inventory.build(triples)


class TestTriples(unittest.TestCase):
	def test_agents(self):
		triples = inventory.agent_triples(AGENTS)
		self.assertIn(("h1", None, None, "agent", T1), triples)
		self.assertIn(("h2", "frappe-bench", "c.example.com", "agent", T1), triples)
		self.assertIn(("h3", "bench-x", "sat.example.com", "satellite", T2), triples)
		self.assertIn((None, None, "old.example.com", "satellite", None), triples)  # host unknown

	def test_satellite_site_is_kept_when_its_benches_do_not_list_it(self):
		sat = {"hostname": "sat.example.com", "kind": "satellite", "last_seen": T2,
			   "benches": [{"name": "bench-x", "host": "h3", "sites": []}]}  # fmt: skip
		self.assertIn(("h3", "bench-x", "sat.example.com", "satellite", T2), inventory.agent_triples([sat]))
		self.assertEqual(inventory.satellite_hosts(sat), {"h3"})

	def test_agent_state(self):
		self.assertEqual(inventory.agent_state("Disabled", T2, T1), "disabled")
		self.assertEqual(inventory.agent_state("Active", None, T1), "never_seen")
		self.assertEqual(inventory.agent_state("Active", T2, T1), "fresh")
		self.assertEqual(inventory.agent_state("Active", T1, T2), "stale")

	def test_observed_rows_need_a_host_or_site(self):
		rows = [{"host": "h1", "bench": "frappe-bench", "site": None, "last_seen": T2}, {"host": None, "site": None}]
		self.assertEqual(inventory.observed_triples(rows, "events"), [("h1", "frappe-bench", None, "events", T2)])


class TestBuild(unittest.TestCase):
	def test_benches_are_per_host(self):
		hosts = tree(inventory.agent_triples(AGENTS))["hosts"]
		self.assertEqual([h["host"] for h in hosts], ["h1", "h2", "h3"])
		h1, h2 = hosts[0], hosts[1]
		self.assertEqual([(b["host"], b["bench"]) for b in h1["benches"] + h2["benches"]],
						 [("h1", "frappe-bench"), ("h2", "frappe-bench")])  # fmt: skip
		self.assertEqual([s["site"] for s in h2["benches"][0]["sites"]], ["b.example.com", "c.example.com"])

	def test_sources_and_last_seen_merge(self):
		triples = inventory.agent_triples(AGENTS[:1]) + [("h1", "frappe-bench", "a.example.com", "events", T2)]
		bench = tree(triples)["hosts"][0]["benches"][0]
		self.assertEqual(bench["sources"], ["agent", "events"])
		self.assertEqual(bench["last_seen"], T2)
		self.assertEqual(bench["sites"][0]["sources"], ["agent", "events"])

	def test_observed_only_bench_is_marked_as_such(self):
		h = tree([("h9", "new-bench", "n.example.com", "metrics", T1)])["hosts"][0]
		self.assertEqual(h["sources"], ["metrics"])  # no agent registration: the UI says so

	def test_site_without_bench_or_host(self):
		t = tree([("h1", None, "x.example.com", "events", T1), (None, None, "y.example.com", "events", T1)])
		self.assertEqual([s["site"] for s in t["hosts"][0]["sites"]], ["x.example.com"])
		self.assertEqual([s["site"] for s in t["unplaced_sites"]], ["y.example.com"])
		self.assertTrue(t["note"])


class TestVisibility(unittest.TestCase):
	def test_same_rule_as_records(self):
		triples = inventory.agent_triples(AGENTS)
		site_only = scopes.build_grants([{"scope_type": "site", "value": "b.example.com"}])
		visible = inventory.visible(triples, site_only)
		self.assertEqual(visible, [("h2", "frappe-bench", "b.example.com", "agent", T1)])
		# a host-wide node needs a host grant
		host_grant = scopes.build_grants([{"scope_type": "host", "value": "h1"}])
		self.assertEqual({t[:3] for t in inventory.visible(triples, host_grant)},
						 {("h1", None, None), ("h1", "frappe-bench", "a.example.com")})  # fmt: skip
		self.assertEqual(inventory.visible(triples, None), triples)

	def test_restricted_tree_hides_other_sites_of_the_bench(self):
		grants = scopes.build_grants([{"scope_type": "site", "value": "b.example.com"}])
		h2 = tree(inventory.visible(inventory.agent_triples(AGENTS), grants))["hosts"][0]
		self.assertEqual([s["site"] for s in h2["benches"][0]["sites"]], ["b.example.com"])


if __name__ == "__main__":
	unittest.main()
