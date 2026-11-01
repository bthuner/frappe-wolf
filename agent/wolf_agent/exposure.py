"""Bench exposure: services listening beyond loopback, and risky site_config settings.

Listening sockets come from `/proc/net/tcp` and `/proc/net/tcp6` (no root needed, no
external tool). A socket counts as exposed when it listens on every interface (`0.0.0.0`,
`::`) or on a public address, for a port a bench uses internally: MariaDB/Postgres, the
bench Redis instances, socket.io and the gunicorn web port, which nginx is meant to front.
A firewall may still block it: the agent cannot see that, and says so.

Each site's `site_config.json` (over `common_site_config.json`) is read for settings that
should never reach production: `developer_mode`, `allow_tests`, a `host_name` over plain HTTP
and `allow_cors: "*"`.

Metrics `exposed_services` (host) and `site_config_risks` (site) carry the counts on every
run. Events (`service_exposed`, `insecure_site_config`) are sent only when the set changes,
like process states, so an exposure that lasts does not repeat every interval.
"""

import ipaddress
import json
import logging
import os
from urllib.parse import urlsplit

from .util import event, fingerprint, metric, utc_iso

log = logging.getLogger(__name__)

# port -> (service, severity when exposed). Redis has no password by default: critical.
DEFAULT_PORTS = {
	3306: ("MariaDB", "warning"),
	5432: ("PostgreSQL", "warning"),
	6379: ("Redis", "critical"),
	11000: ("Redis (queue)", "critical"),
	12000: ("Redis (socket.io)", "critical"),
	13000: ("Redis (cache)", "critical"),
	9000: ("socket.io", "warning"),
	8000: ("gunicorn (bench web)", "warning"),
}
LISTEN = "0A"
# what the exposure opens the door to (MITRE ATT&CK)
ATTACK = {"service_exposed": ["T1190"], "insecure_site_config": ["T1190"]}


def _ipv4(hexaddr):
	return str(ipaddress.IPv4Address(int(hexaddr, 16).to_bytes(4, "little")))


def _ipv6(hexaddr):
	# four 32-bit words, each in host (little-endian) order
	raw = b"".join(int(hexaddr[i : i + 8], 16).to_bytes(4, "little") for i in range(0, 32, 8))
	return str(ipaddress.IPv6Address(raw))


def parse_proc_net(text, ipv6=False):
	"""Listening `(address, port)` pairs from a /proc/net/tcp{,6} table."""
	out = []
	for line in (text or "").splitlines()[1:]:
		parts = line.split()
		if len(parts) < 4 or parts[3] != LISTEN or ":" not in parts[1]:
			continue
		addr, port = parts[1].rsplit(":", 1)
		try:
			out.append(((_ipv6 if ipv6 else _ipv4)(addr), int(port, 16)))
		except (ValueError, ipaddress.AddressValueError):
			continue
	return out


def is_exposed(address):
	"""Listening on all interfaces or on a public address (not loopback, not a LAN address)."""
	ip = ipaddress.ip_address(address)
	if getattr(ip, "ipv4_mapped", None):
		ip = ip.ipv4_mapped
	return ip.is_unspecified or (not ip.is_loopback and not ip.is_private and not ip.is_link_local)


def _port(url):
	try:
		return urlsplit(str(url)).port
	except ValueError:
		return None


def read_json(path):
	try:
		with open(path) as f:
			data = json.load(f)
		return data if isinstance(data, dict) else {}
	except (OSError, ValueError):
		return {}


def bench_ports(benches, extra=None):
	"""Sensitive ports: the defaults, each bench's own (common_site_config), and `extra`."""
	ports = dict(DEFAULT_PORTS)
	for bench in benches:
		common = read_json(os.path.join(bench.get("path") or "", "sites", "common_site_config.json"))
		for key, service, severity in (
			("redis_queue", "Redis (queue)", "critical"), ("redis_cache", "Redis (cache)", "critical"),
			("redis_socketio", "Redis (socket.io)", "critical"),
		):  # fmt: skip
			port = _port(common.get(key))
			if port:
				ports[port] = (service, severity)
		for key, service in (("webserver_port", "gunicorn (bench web)"), ("socketio_port", "socket.io"), ("db_port", "database")):
			try:
				if common.get(key):
					ports[int(common[key])] = (service, "warning")
			except (TypeError, ValueError):
				pass
	for port, service in (extra or {}).items():
		try:
			ports[int(port)] = (str(service), "warning")
		except (TypeError, ValueError):
			pass
	return ports


def exposed_listeners(listeners, ports):
	"""`{port: (service, severity, sorted addresses)}` for sensitive ports listening beyond loopback."""
	out = {}
	for address, port in listeners:
		if port in ports and is_exposed(address):
			service, severity = ports[port]
			entry = out.setdefault(port, (service, severity, []))
			if address not in entry[2]:
				entry[2].append(address)
	return {p: (s, sev, sorted(a)) for p, (s, sev, a) in out.items()}


def site_config_findings(site_config):
	"""Risky settings in a site's merged config: `[{"setting", "value", "why"}]`."""
	found = []
	if site_config.get("developer_mode"):
		found.append({"setting": "developer_mode", "value": site_config["developer_mode"],
		              "why": "lets System Managers edit DocTypes and run code paths meant for development"})  # fmt: skip
	if site_config.get("allow_tests"):
		found.append({"setting": "allow_tests", "value": site_config["allow_tests"],
		              "why": "allows `bench run-tests`, which creates and deletes records on this site"})  # fmt: skip
	host_name = str(site_config.get("host_name") or "")
	if host_name.lower().startswith("http://"):
		found.append({"setting": "host_name", "value": host_name,
		              "why": "links and emails point to plain HTTP: sessions can be sniffed or redirected"})  # fmt: skip
	if site_config.get("allow_cors") in ("*", ["*"]):
		found.append({"setting": "allow_cors", "value": site_config["allow_cors"],
		              "why": "any website may call this site's API from a visitor's browser"})  # fmt: skip
	return found


def read_listeners(root="/proc/net"):
	out = []
	for name, v6 in (("tcp", False), ("tcp6", True)):
		try:
			with open(os.path.join(root, name)) as f:
				out += parse_proc_net(f.read(), ipv6=v6)
		except OSError:
			continue
	return out


class ExposureWatcher:
	"""Counts on every run; events only when what is exposed changes."""

	def __init__(self, host, extra_ports=None, proc_root="/proc/net"):
		self.host = host
		self.extra_ports = extra_ports
		self.proc_root = proc_root
		self.last_ports = None  # None: first run, report what is already exposed
		self.last_sites = {}

	def collect(self, benches, now=None):
		ts = utc_iso(now)
		metrics, events = [], []
		exposed = exposed_listeners(read_listeners(self.proc_root), bench_ports(benches, self.extra_ports))
		metrics.append(metric("exposed_services", len(exposed), ts, self.host))
		previous = self.last_ports or {}
		for port, (service, severity, addresses) in sorted(exposed.items()):
			if self.last_ports is not None and port in previous and previous[port][2] == addresses:
				continue
			where = "all interfaces" if any(ipaddress.ip_address(a).is_unspecified for a in addresses) else ", ".join(addresses)
			events.append(event(
				"security", "service_exposed", severity, ts, self.host,
				f"{service} (port {port}) listens on {where}",
				fingerprint(self.host, "service_exposed", port),
				{"port": port, "service": service, "addresses": addresses, "attack_techniques": ATTACK["service_exposed"],
				 "note": "a firewall may still block it; Wolf only sees the listening socket"},
				source="exposure",
			))  # fmt: skip
		self.last_ports = exposed

		for bench in benches:
			sites_dir = os.path.join(bench.get("path") or "", "sites")
			common = read_json(os.path.join(sites_dir, "common_site_config.json"))
			for site in bench.get("sites") or []:
				config = {**common, **read_json(os.path.join(sites_dir, site, "site_config.json"))}
				findings = site_config_findings(config)
				metrics.append(metric("site_config_risks", len(findings), ts, self.host, bench["name"], site))
				key = (bench["name"], site)
				names = sorted(f["setting"] for f in findings)
				if findings and names != self.last_sites.get(key):
					events.append(event(
						"security", "insecure_site_config", "critical" if "developer_mode" in names else "warning",
						ts, self.host, f"Risky site settings on {site}: {', '.join(names)}",
						fingerprint(site, "insecure_site_config"),
						{"findings": findings, "attack_techniques": ATTACK["insecure_site_config"]},
						source="exposure", bench=bench["name"], site=site,
					))  # fmt: skip
				self.last_sites[key] = names
		return metrics, events
