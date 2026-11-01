"""Offline IP reputation lists and honeytoken matching (no frappe imports).

Lists are plain text, one IPv4/IPv6 address or CIDR per line, `#` or `;` starting a comment
(FireHOL `.netset`, Spamhaus DROP / DROPv6 and most blocklists use this layout). `Index` keeps
them as sorted integer ranges, so a lookup is a binary search per list, with no network call:
the lists are downloaded once a day by api/threat.py and nothing about Wolf's events leaves
the site.

`tag(payload, severity, index)` marks a security event whose `ip` is listed: the payload gets
`reputation: {"listed": true, "lists": [...]}` and the severity goes up one step (info →
warning → critical).
"""

import bisect
import ipaddress
import re

_LINE_RE = re.compile(r"^\s*([0-9A-Fa-f:.]+(?:/\d{1,3})?)")
MAX_ENTRIES = 500_000  # per list: FireHOL level 1 is ~4,500 lines, the largest common lists ~100k
RAISE = {"info": "warning", "warning": "critical", "critical": "critical"}


def parse_list(text, limit=MAX_ENTRIES):
	"""Networks of a blocklist (invalid lines skipped)."""
	nets = []
	for line in (text or "").splitlines():
		if not line.strip() or line.lstrip().startswith(("#", ";")):
			continue
		m = _LINE_RE.match(line)
		if not m:
			continue
		try:
			nets.append(ipaddress.ip_network(m.group(1), strict=False))
		except ValueError:
			continue
		if len(nets) >= limit:
			break
	return nets


class Index:
	"""`{list name: networks}` as merged sorted ranges, per IP version."""

	def __init__(self, lists=None):
		self.ranges = {}  # (name, version) -> (starts, ends)
		for name, nets in (lists or {}).items():
			self.add(name, nets)

	def add(self, name, nets):
		for version in (4, 6):
			spans = sorted((int(n.network_address), int(n.broadcast_address)) for n in nets if n.version == version)
			merged = []
			for start, end in spans:
				if merged and start <= merged[-1][1] + 1:
					merged[-1][1] = max(merged[-1][1], end)
				else:
					merged.append([start, end])
			if merged:
				self.ranges[(name, version)] = ([s for s, _ in merged], [e for _, e in merged])

	def names(self):
		return sorted({name for name, _ in self.ranges})

	def lookup(self, address):
		"""Names of the lists containing `address` (an invalid address is in none)."""
		try:
			ip = ipaddress.ip_address(str(address).strip())
		except ValueError:
			return []
		if getattr(ip, "ipv4_mapped", None):
			ip = ip.ipv4_mapped
		value = int(ip)
		found = []
		for (name, version), (starts, ends) in self.ranges.items():
			if version != ip.version:
				continue
			i = bisect.bisect_right(starts, value) - 1
			if i >= 0 and value <= ends[i]:
				found.append(name)
		return sorted(found)

	def to_json(self):
		return {f"{name}|{v}": [starts, ends] for (name, v), (starts, ends) in self.ranges.items()}

	@classmethod
	def from_json(cls, data):
		index = cls()
		for key, (starts, ends) in (data or {}).items():
			name, _, version = key.rpartition("|")
			index.ranges[(name, int(version))] = (list(starts), list(ends))
		return index


def tag(payload, severity, index):
	"""(payload, severity) with the reputation of `payload["ip"]`; unchanged when not listed."""
	if not isinstance(payload, dict) or not payload.get("ip") or index is None:
		return payload, severity
	lists = index.lookup(payload["ip"])
	if not lists:
		return payload, severity
	return {**payload, "reputation": {"listed": True, "lists": lists}}, RAISE.get(severity, severity)


def parse_sources(text):
	"""`name url-or-path` per line (a bare URL is named after its file) → [(name, source)]."""
	out = []
	for line in (text or "").splitlines():
		parts = line.split()
		if not parts or parts[0].startswith("#"):
			continue
		if len(parts) == 1:
			source = parts[0]
			name = re.sub(r"\.(netset|ipset|txt)$", "", source.rstrip("/").rsplit("/", 1)[-1]) or source
		else:
			name, source = parts[0], parts[1]
		out.append((name[:60], source))
	return out


def lines(text):
	"""Non-empty, non-comment lines of a settings field, stripped."""
	return [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]


def api_key_from_header(value):
	"""The API key of an `Authorization: token key:secret` or `Basic base64(key:secret)` header."""
	import base64
	import binascii

	text = str(value or "").strip()
	scheme, _, rest = text.partition(" ")
	scheme = scheme.lower()
	if scheme == "token":
		return rest.split(":", 1)[0].strip() or None
	if scheme == "basic":
		try:
			decoded = base64.b64decode(rest.strip(), validate=True).decode("utf-8", "replace")
		except (binascii.Error, ValueError):
			return None
		return decoded.split(":", 1)[0].strip() or None
	return None
