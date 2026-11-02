"""Entities of a security event, whatever its source (no frappe imports; #72 step 3).

Wolf's own detectors, Suricata (`network_ids_alert`) and Wazuh (`hids_alert`) name the same
things differently: an address in `ip`, `src_ip` or `dest_ip`, a host by its Wolf hostname, its
Wazuh agent name or one of its addresses, a site by its name or one of its domains (`http_hostname`,
`tls_sni`). `extract` turns an event into `{"ip": [...], "host": [...], "site": [...],
"user": [...]}`, normalised:

- an IP is written the canonical way (`::ffff:1.2.3.4` is `1.2.3.4`, IPv6 compressed). An
  address that is one of a known host's becomes that host, not an IP, since the IP entity is the
  other party;
- host and site names are lowercased and resolved through the aliases (`Aliases`): Wolf Agent
  hostnames and addresses, Wazuh agent names, site domains, and any alias added by hand;
- users are lowercased e-mails or login names; placeholders such as `-` or `unknown` are dropped.

The same IP or host then finds every source's events (api/entities.py).
"""

import ipaddress

KINDS = ("ip", "host", "site", "user")
MAX_PER_KIND = 10
_IP_KEYS = ("ip", "src_ip", "dest_ip")
_USER_KEYS = ("user", "suser", "username", "srcuser", "dstuser")
_SITE_KEYS = ("http_hostname", "tls_sni")
_NO_USER = {"", "-", "unknown", "none", "null", "guest", "(none)"}


def normal_ip(value):
	"""The canonical text of an IP address, or None."""
	try:
		ip = ipaddress.ip_address(str(value).strip().strip("[]"))
	except ValueError:
		return None
	if getattr(ip, "ipv4_mapped", None):
		ip = ip.ipv4_mapped
	return str(ip)


def normal_name(value):
	text = str(value or "").strip().lower().rstrip(".")
	return text[:140] or None


class Aliases:
	"""`{(kind, alias): canonical}` for hosts and sites; an IP alias of a host is kept under
	("host", ip)."""

	def __init__(self, rows=()):
		self.map = {}
		for kind, alias, canonical in rows:
			key = normal_ip(alias) if kind == "host" and normal_ip(alias) else normal_name(alias)
			if key and canonical:
				self.map[(kind, key)] = normal_name(canonical)

	def host(self, name):
		key = normal_ip(name) or normal_name(name)
		return self.map.get(("host", key), None if normal_ip(name) else key) if key else None

	def host_of_address(self, address):
		"""The host an address belongs to, or None when it is not one of a known host's."""
		ip = normal_ip(address)
		return self.map.get(("host", ip)) if ip else None

	def site(self, name):
		key = normal_name(name)
		if not key:
			return None
		return self.map.get(("site", key.split(":")[0]))


def _add(out, kind, value):
	if value and value not in out[kind] and len(out[kind]) < MAX_PER_KIND:
		out[kind].append(value)


def extract(event, payload, aliases=None):
	"""`{kind: [values]}` for an event dict (host, site) and its payload dict."""
	aliases = aliases or Aliases()
	payload = payload if isinstance(payload, dict) else {}
	out = {k: [] for k in KINDS}
	_add(out, "host", aliases.host(event.get("host")))
	_add(out, "site", normal_name(event.get("site")))
	for key in _IP_KEYS:
		value = payload.get(key)
		ip = normal_ip(value)
		if not ip:
			continue
		owner = aliases.host_of_address(ip)
		if owner:
			_add(out, "host", owner)
		else:
			_add(out, "ip", ip)
	agent = payload.get("wazuh_agent") if isinstance(payload.get("wazuh_agent"), dict) else {}
	if agent.get("name") and agent.get("id") != "000":
		_add(out, "host", aliases.host(agent["name"]))
	for key in _SITE_KEYS:
		site = aliases.site(payload.get(key))
		_add(out, "site", site)
	users = [payload.get(k) for k in _USER_KEYS]
	if isinstance(payload.get("users"), list):
		users += payload["users"]
	for user in users:
		name = normal_name(user)
		if name and name not in _NO_USER:
			_add(out, "user", name)
	return {k: v for k, v in out.items() if v}


def parse_filter(text):
	"""`kind:value` → (kind, normalised value); raises ValueError."""
	kind, sep, value = str(text or "").partition(":")
	kind = kind.strip().lower()
	if not sep or kind not in KINDS or not value.strip():
		raise ValueError("entity filter must be ip:, host:, site: or user: followed by a value")
	if kind == "ip":
		ip = normal_ip(value)
		if not ip:
			raise ValueError(f"{value!r} is not an IP address")
		return kind, ip
	return kind, normal_name(value)
