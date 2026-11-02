"""API key used from an IP never seen for it (no frappe imports; api/api_keys.py records).

Every successful request authenticated by an API key (`Authorization: token key:secret` or
`Basic`) records the key's source address. The first address of a key is its baseline, and so is
every address seen in the key's first LEARNING_HOURS: integrations often call from a few servers.
After that, an address outside every known network (/24 for IPv4, /64 for IPv6, so a cloud
host that changes address within its subnet is not news) raises `api_key_new_ip`.
"""

import ipaddress
from datetime import timedelta

LEARNING_HOURS = 24
MAX_KNOWN_IN_PAYLOAD = 10


def network(address):
	"""The /24 or /64 an address belongs to; None for something that is not an address."""
	try:
		ip = ipaddress.ip_address(str(address).strip())
	except ValueError:
		return None
	if getattr(ip, "ipv4_mapped", None):
		ip = ip.ipv4_mapped
	prefix = 24 if ip.version == 4 else 64
	return ipaddress.ip_network(f"{ip}/{prefix}", strict=False)


def verdict(ip, known, first_seen, now, learning_hours=LEARNING_HOURS):
	"""What a first request from `ip` means for a key whose known addresses are `known`.

	"baseline": the key's first address, or one within its learning period;
	"same_network": a new address in the network of a known one; "new": raise the event."""
	if not known or first_seen is None or now - first_seen < timedelta(hours=learning_hours):
		return "baseline"
	net = network(ip)
	if net is not None and any(network(k) == net for k in known):
		return "same_network"
	return "new"
