"""DNS TXT lookups with the standard library only (no frappe imports, no dependency).

Enough for SPF and DMARC: one TXT question to the resolvers of /etc/resolv.conf, over UDP, then
TCP when the answer is truncated. `lookup(name)` returns the TXT strings (each record's
character-strings joined, as SPF and DMARC read them), [] when the name or the record does not
exist, and raises `DNSError` when no resolver answered or one failed.
"""

import random
import socket
import struct

TXT, IN = 16, 1
NOERROR, NXDOMAIN = 0, 3


class DNSError(Exception):
	pass


def build_query(name, qid):
	labels = [p for p in name.strip(".").split(".") if p]
	if not labels or any(len(p.encode()) > 63 for p in labels):
		raise DNSError(f"invalid name {name!r}")
	qname = b"".join(bytes([len(p.encode())]) + p.encode() for p in labels) + b"\0"
	return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", TXT, IN)


def _skip_name(data, pos):
	"""Position after a (possibly compressed) domain name."""
	while True:
		if pos >= len(data):
			raise DNSError("truncated name")
		length = data[pos]
		if length == 0:
			return pos + 1
		if length & 0xC0 == 0xC0:
			return pos + 2
		pos += 1 + length


def parse_response(data, qid):
	"""(rcode, truncated, [txt strings]) of a response to query `qid`."""
	if len(data) < 12:
		raise DNSError("short response")
	rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
	if rid != qid or not flags & 0x8000:
		raise DNSError("not the answer to this question")
	rcode, truncated = flags & 0x000F, bool(flags & 0x0200)
	pos = 12
	for _ in range(qd):
		pos = _skip_name(data, pos) + 4
	out = []
	for _ in range(an):
		pos = _skip_name(data, pos)
		if pos + 10 > len(data):
			raise DNSError("truncated record")
		rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", data[pos : pos + 10])
		pos += 10
		rdata = data[pos : pos + rdlen]
		if len(rdata) != rdlen:
			raise DNSError("truncated record data")
		pos += rdlen
		if rtype != TXT or rclass != IN:
			continue  # a CNAME on the way, for instance
		parts, i = [], 0
		while i < len(rdata):
			n = rdata[i]
			parts.append(rdata[i + 1 : i + 1 + n])
			i += 1 + n
		out.append(b"".join(parts).decode("utf-8", "replace"))
	return rcode, truncated, out


def resolvers(path="/etc/resolv.conf"):
	try:
		with open(path) as f:
			lines = f.read().splitlines()
	except OSError:
		return []
	return [line.split()[1] for line in lines if line.strip().startswith("nameserver") and len(line.split()) > 1]


def _ask_udp(server, query, timeout, port):
	family = socket.AF_INET6 if ":" in server else socket.AF_INET
	with socket.socket(family, socket.SOCK_DGRAM) as sock:
		sock.settimeout(timeout)
		sock.sendto(query, (server, port))
		data, _ = sock.recvfrom(65535)
	return data


def _ask_tcp(server, query, timeout, port):
	with socket.create_connection((server, port), timeout=timeout) as sock:
		sock.sendall(struct.pack(">H", len(query)) + query)
		head = sock.recv(2)
		if len(head) < 2:
			raise DNSError("no TCP answer")
		(length,) = struct.unpack(">H", head)
		data = b""
		while len(data) < length:
			chunk = sock.recv(length - len(data))
			if not chunk:
				raise DNSError("truncated TCP answer")
			data += chunk
	return data


def lookup(name, servers=None, timeout=3.0, port=53):
	"""TXT strings of `name`; [] if it has none; DNSError if no resolver could tell."""
	servers = servers if servers is not None else resolvers()
	if not servers:
		raise DNSError("no resolver in /etc/resolv.conf")
	errors = []
	for server in servers:
		qid = random.randint(0, 0xFFFF)
		query = build_query(name, qid)
		try:
			rcode, truncated, txt = parse_response(_ask_udp(server, query, timeout, port), qid)
			if truncated:
				rcode, _, txt = parse_response(_ask_tcp(server, query, timeout, port), qid)
		except (OSError, DNSError) as e:
			errors.append(f"{server}: {e}")
			continue
		if rcode == NXDOMAIN:
			return []
		if rcode != NOERROR:
			errors.append(f"{server}: rcode {rcode}")
			continue
		return txt
	raise DNSError("; ".join(errors))
