"""Suricata `eve.json` alerts → `security` / `network_ids_alert` events (#72, step 1).

Suricata is optional: the agent reads its EVE log when there is one (`suricata.eve_log`,
default `/var/log/suricata/eve.json`), and works the same without it.

Only `event_type: alert` records are read. Each (signature, remote address) pair gives one
event per push interval, with a count, so a scan that fires the same rule a thousand times is
one event, not a thousand. What the alert record itself carries is kept as context:
- the app-layer fields Suricata adds to alerts by default (`http` hostname, URL, method,
  status, user agent; `tls` SNI; `dns` query);
- `flow_id`, `community_id` (to find the flow in Suricata or Zeek);
- the rule's ATT&CK metadata (`mitre_technique_id`, as in ET Open).

- **Severity**: the rule's priority. 1 is critical, 2 is warning, 3 and above are info and are
  only sent at or under `suricata.max_priority`.
- **Remote address** (the `ip` Wolf enriches, correlates and may propose to block):
  1. the rule's `alert.source`, when it sets one (rules with a `target` keyword);
  2. otherwise the end of the flow that is not one of this host's addresses. The flow is the
     connection, not the alerting packet, so a rule on the server's answer still names the
     client, and an outbound connection names the remote server;
  3. failing that, the end that opened the connection.
- **Site**: the HTTP host name or TLS SNI, when it is one of the bench's sites or domains.
"""

import ipaddress
import json
from datetime import datetime

from ..util import event, fingerprint, utc_iso

MAX_GROUPS = 500  # per push interval: past that, alerts are only counted
MAX_TARGETS = 10
SEVERITY = {1: "critical", 2: "warning"}


def _public(address):
	try:
		ip = ipaddress.ip_address(str(address))
	except ValueError:
		return False
	return ip.is_global


def local_addresses(fib_trie="/proc/net/fib_trie", if_inet6="/proc/net/if_inet6"):
	"""This host's own addresses (Linux), to tell the remote end of a flow from ours."""
	found = set()
	try:
		with open(fib_trie) as f:
			last = None
			for line in f:
				parts = line.split()
				if parts[:1] == ["|--"] and len(parts) > 1:
					last = parts[1]
				elif "/32 host LOCAL" in line and last:
					found.add(last)
	except OSError:
		pass
	try:
		with open(if_inet6) as f:
			for line in f:
				raw = line.split()[0] if line.split() else ""
				if len(raw) == 32:
					found.add(str(ipaddress.ip_address(int(raw, 16))))
	except (OSError, ValueError):
		pass
	return found


def remote_address(record, local=()):
	"""The address the alert is about (see the module docstring)."""
	source = ((record.get("alert") or {}).get("source") or {}).get("ip")
	if source:
		return source
	flow = record.get("flow") or {}
	opener, responder = flow.get("src_ip") or record.get("src_ip"), flow.get("dest_ip") or record.get("dest_ip")
	if opener in local and responder and responder not in local:
		return responder
	if responder in local or opener not in local:
		return opener or responder
	if not _public(opener) and _public(responder):
		return responder
	return opener or responder


def timestamp(text):
	"""Suricata's `2026-11-02T15:00:00.123456+0000` as UTC ISO (Python 3.9 reads no `+0000`)."""
	for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
		try:
			return utc_iso(datetime.strptime(str(text), fmt))
		except ValueError:
			continue
	return None


def techniques(alert):
	meta = alert.get("metadata") or {}
	ids = meta.get("mitre_technique_id") or []
	return [str(t).strip().upper() for t in ids if str(t).strip()][:10] if isinstance(ids, list) else []


def context(record):
	"""App-layer fields Suricata put on the alert record, trimmed."""
	out = {}
	http = record.get("http") or {}
	for key in ("hostname", "url", "http_method", "status", "http_user_agent"):
		if http.get(key) is not None:
			out[f"http_{key}" if not key.startswith("http_") else key] = str(http[key])[:300]
	sni = (record.get("tls") or {}).get("sni")
	if sni:
		out["tls_sni"] = str(sni)[:255]
	dns = record.get("dns") or {}
	query = dns.get("rrname") or ((dns.get("query") or [{}])[0].get("rrname") if isinstance(dns.get("query"), list) else None)
	if query:
		out["dns_query"] = str(query)[:255]
	return out


class SuricataParser:
	def __init__(self, host, log_file=None, max_priority=3, site_benches=None, site_aliases=None, local=None, **_):
		self.host, self.log_file = host, log_file
		self.local = set(local) if local is not None else local_addresses()
		self.max_priority = int(max_priority or 3)
		self.site_benches = dict(site_benches or {})  # site -> bench
		self.site_aliases = dict(site_aliases or {})  # custom domain -> site
		self.groups = {}
		self.overflow = 0
		self.bad_lines = 0

	def site_of(self, record):
		for name in ((record.get("http") or {}).get("hostname"), (record.get("tls") or {}).get("sni")):
			if not name:
				continue
			name = str(name).lower().split(":")[0]
			site = name if name in self.site_benches else self.site_aliases.get(name)
			if site:
				return site, self.site_benches.get(site)
		return None, None

	def feed(self, line):
		if '"alert"' not in line:
			return  # flows, stats, http... records: not read
		try:
			record = json.loads(line)
		except ValueError:
			self.bad_lines += 1
			return
		if not isinstance(record, dict) or record.get("event_type") != "alert":
			return
		alert = record.get("alert") or {}
		try:
			priority = int(alert.get("severity") or 3)
		except (TypeError, ValueError):
			priority = 3
		if priority > self.max_priority:
			return
		ip = remote_address(record, self.local)
		key = (alert.get("signature_id"), ip)
		g = self.groups.get(key)
		if g is None:
			if len(self.groups) >= MAX_GROUPS:
				self.overflow += 1
				return
			site, bench = self.site_of(record)
			g = self.groups[key] = {
				"count": 0, "priority": priority, "first": timestamp(record.get("timestamp")), "targets": [], "site": site, "bench": bench,
				"signature": alert.get("signature"), "sid": alert.get("signature_id"), "rev": alert.get("rev"),
				"category": alert.get("category"), "action": alert.get("action"), "techniques": techniques(alert),
				"src_ip": record.get("src_ip"), "src_port": record.get("src_port"), "dest_ip": record.get("dest_ip"),
				"dest_port": record.get("dest_port"), "proto": record.get("proto"), "app_proto": record.get("app_proto"),
				"flow_id": record.get("flow_id"), "community_id": record.get("community_id"), "context": context(record),
			}  # fmt: skip
		g["count"] += 1
		g["last"] = timestamp(record.get("timestamp"))
		flow = record.get("flow") or {}
		if ip == (flow.get("src_ip") or record.get("src_ip")):
			target = f"{flow.get('dest_ip') or record.get('dest_ip')}:{flow.get('dest_port') or record.get('dest_port')}"
		else:
			target = str(flow.get("src_ip") or record.get("src_ip"))
		if target not in g["targets"] and len(g["targets"]) < MAX_TARGETS:
			g["targets"].append(target)

	def drain(self, now=None):
		ts = utc_iso(now)
		events = []
		for (sid, ip), g in self.groups.items():
			severity = SEVERITY.get(g["priority"], "info")
			blocked = " (blocked)" if g["action"] == "blocked" else ""
			title = f"Suricata: {g['signature'] or f'rule {sid}'}" + (f" from {ip}" if ip else "") + (f", {g['count']} alerts" if g["count"] > 1 else "") + blocked
			payload = {
				"ip": ip, "count": g["count"], "signature": g["signature"], "sid": sid, "rev": g["rev"],
				"rule_category": g["category"], "priority": g["priority"], "action": g["action"],
				"src_ip": g["src_ip"], "src_port": g["src_port"], "dest_ip": g["dest_ip"], "dest_port": g["dest_port"],
				"proto": g["proto"], "app_proto": g["app_proto"], "flow_id": g["flow_id"], "community_id": g["community_id"],
				"first_seen": g["first"], "last_seen": g.get("last"), "targets": g["targets"], "log_file": self.log_file,
				**g["context"],
			}  # fmt: skip
			if g["techniques"]:
				payload["attack_techniques"] = g["techniques"]
			events.append(event("security", "network_ids_alert", severity, g["first"] or ts, self.host, title[:255],
			                    fingerprint(self.host, "suricata", sid, ip), {k: v for k, v in payload.items() if v is not None},
			                    source="suricata", bench=g["bench"], site=g["site"]))  # fmt: skip
		if self.overflow:
			events.append(event("security", "network_ids_alert", "warning", ts, self.host,
			                    f"Suricata: {self.overflow} more alerts not detailed (over {MAX_GROUPS} signature/address pairs)",
			                    fingerprint(self.host, "suricata", "overflow"), {"count": self.overflow, "log_file": self.log_file},
			                    source="suricata"))  # fmt: skip
		self.groups, self.overflow = {}, 0
		return [], events
