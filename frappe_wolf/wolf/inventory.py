"""Which hosts, benches and sites Wolf knows about, and from where (no frappe imports).

Two kinds of source: what agents declared when they registered (`agent`, `satellite`), and
what telemetry shows (`events`, `metrics`, `incidents`). Neither is complete on its own: a
bench added after registration only appears once it sends telemetry, and a quiet site only
through its agent. Every node says which sources it came from, so the UI can say so too.

A bench is identified by (host, bench): bench names such as `frappe-bench` repeat across hosts.
"""

import json

from frappe_wolf.wolf import scopes

COLUMNS = {"site": "site", "bench": "bench", "host": "host"}

NOTE = (
	"Hosts and benches come from agent registrations and from telemetry in the window. A bench "
	"or site added after its agent registered appears once it sends events or metrics."
)


def _benches(value):
	if isinstance(value, list):
		return value
	try:
		benches = json.loads(value or "[]")
	except (TypeError, ValueError):
		return []
	return benches if isinstance(benches, list) else []


def agent_triples(agents):
	"""(host, bench, site, source, last_seen) from Wolf Agent rows (hostname, kind, benches, last_seen).

	An agent's hostname is its host. A satellite's hostname is its site name, so its benches
	carry the host they run on.
	"""
	out = []
	for agent in agents:
		kind = agent.get("kind") or "agent"
		seen = agent.get("last_seen")
		benches = [b for b in _benches(agent.get("benches")) if isinstance(b, dict)]
		host = agent.get("hostname") if kind == "agent" else None
		if kind == "agent" and host:
			out.append((host, None, None, kind, seen))
		elif kind == "satellite" and agent.get("hostname") and not any(b.get("host") for b in benches):
			out.append((None, None, agent["hostname"], kind, seen))  # its site, host unknown
		for bench in benches:
			bench_host = host or bench.get("host")
			if not bench_host:
				continue  # a bench without a host cannot be told apart from its namesakes
			sites = [s for s in bench.get("sites") or [] if s] or [None]
			for site in sites:
				out.append((bench_host, bench.get("name") or None, site, kind, seen))
	return out


def observed_triples(rows, source):
	"""(host, bench, site, source, last_seen) from telemetry rows (host, bench, site, last_seen)."""
	return [
		(r.get("host") or None, r.get("bench") or None, r.get("site") or None, source, r.get("last_seen"))
		for r in rows
		if r.get("host") or r.get("site")
	]


def visible(triples, grants):
	"""The triples a user may see, by the same rule as Wolf records (wolf/scopes.py)."""
	return [t for t in triples if scopes.row_visible({"host": t[0], "bench": t[1], "site": t[2]}, grants, COLUMNS)]


def _later(a, b):
	return b if a is None or (b is not None and b > a) else a


def build(triples):
	"""Tree of hosts > benches > sites, each with its sources and last time seen.

	Sites seen without a host (telemetry from a site with no host column) are listed apart
	in `unplaced_sites`, so they are not attached to a guessed host.
	"""
	hosts, unplaced = {}, {}
	for host, bench, site, source, seen in triples:
		if not host:
			node = unplaced.setdefault(site, {"site": site, "sources": set(), "last_seen": None})
			node["sources"].add(source)
			node["last_seen"] = _later(node["last_seen"], seen)
			continue
		h = hosts.setdefault(host, {"host": host, "sources": set(), "last_seen": None, "benches": {}, "sites": {}})
		h["sources"].add(source)
		h["last_seen"] = _later(h["last_seen"], seen)
		if bench:
			b = h["benches"].setdefault(bench, {"bench": bench, "host": host, "sources": set(), "last_seen": None, "sites": {}})
			b["sources"].add(source)
			b["last_seen"] = _later(b["last_seen"], seen)
			parent = b["sites"]
		else:
			parent = h["sites"]  # a site seen on the host, bench unknown
		if site:
			s = parent.setdefault(site, {"site": site, "sources": set(), "last_seen": None})
			s["sources"].add(source)
			s["last_seen"] = _later(s["last_seen"], seen)

	def finish(node, children=()):
		node["sources"] = sorted(node["sources"])
		for key in children:
			node[key] = [finish(c, ("sites",) if key == "benches" else ()) for _, c in sorted(node[key].items())]
		return node

	return {
		"hosts": [finish(h, ("benches", "sites")) for _, h in sorted(hosts.items())],
		"unplaced_sites": [finish(s) for _, s in sorted(unplaced.items(), key=lambda kv: kv[0] or "")],
		"note": NOTE,
	}
