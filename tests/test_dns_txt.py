import socket
import socketserver
import struct
import threading
import unittest

from frappe_wolf.wolf import dns_txt


def answer(query, records, rcode=0, truncated=False):
	"""A DNS response to `query` with TXT `records` (each a list of character-strings)."""
	qid = struct.unpack(">H", query[:2])[0]
	question = query[12:]
	flags = 0x8180 | rcode | (0x0200 if truncated else 0)
	out = struct.pack(">HHHHHH", qid, flags, 1, 0 if truncated else len(records), 0, 0) + question
	if truncated:
		return out
	for strings in records:
		rdata = b"".join(bytes([len(x)]) + x for x in (s.encode() for s in strings))
		out += b"\xc0\x0c" + struct.pack(">HHIH", dns_txt.TXT, dns_txt.IN, 300, len(rdata)) + rdata
	return out


class FakeDNS:
	"""UDP and TCP on the same port: `zone` maps names to TXT records; `truncate` names answer
	truncated over UDP, so the client must ask again over TCP."""

	def __init__(self, zone, truncate=()):
		self.zone, self.truncate, self.asked = zone, set(truncate), []
		outer = self

		def reply(query, tcp):
			name = []
			pos = 12
			while query[pos]:
				name.append(query[pos + 1 : pos + 1 + query[pos]].decode())
				pos += 1 + query[pos]
			name = ".".join(name)
			outer.asked.append((name, "tcp" if tcp else "udp"))
			if name not in outer.zone:
				return answer(query, [], rcode=dns_txt.NXDOMAIN)
			return answer(query, outer.zone[name], truncated=name in outer.truncate and not tcp)

		class UDP(socketserver.BaseRequestHandler):
			def handle(self):
				data, sock = self.request
				sock.sendto(reply(data, False), self.client_address)

		class TCP(socketserver.BaseRequestHandler):
			def handle(self):
				length = struct.unpack(">H", self.request.recv(2))[0]
				out = reply(self.request.recv(length), True)
				self.request.sendall(struct.pack(">H", len(out)) + out)

		self.udp = socketserver.UDPServer(("127.0.0.1", 0), UDP)
		self.port = self.udp.server_address[1]
		self.tcp = socketserver.TCPServer(("127.0.0.1", self.port), TCP)
		for server in (self.udp, self.tcp):
			threading.Thread(target=server.serve_forever, daemon=True).start()

	def close(self):
		for server in (self.udp, self.tcp):
			server.shutdown()
			server.server_close()


class TestLookup(unittest.TestCase):
	def setUp(self):
		self.dns = FakeDNS({
			"example.org": [["v=spf1 include:_spf.example.net ", "-all"], ["google-site-verification=abc"]],
			"_dmarc.example.org": [["v=DMARC1; p=reject"]],
			"big.example.org": [["v=spf1 -all"]],
			"empty.example.org": [],
		}, truncate={"big.example.org"})  # fmt: skip
		self.addCleanup(self.dns.close)

	def lookup(self, name):
		return dns_txt.lookup(name, servers=["127.0.0.1"], timeout=2, port=self.dns.port)

	def test_strings_of_a_record_are_joined(self):
		self.assertEqual(self.lookup("example.org"), ["v=spf1 include:_spf.example.net -all", "google-site-verification=abc"])

	def test_missing_name_or_record(self):
		self.assertEqual(self.lookup("nowhere.example.org"), [])
		self.assertEqual(self.lookup("empty.example.org"), [])

	def test_truncated_answer_is_asked_again_over_tcp(self):
		self.assertEqual(self.lookup("big.example.org"), ["v=spf1 -all"])
		self.assertEqual([a for a in self.dns.asked if a[0] == "big.example.org"],
		                 [("big.example.org", "udp"), ("big.example.org", "tcp")])  # fmt: skip

	def test_no_resolver_answering(self):
		with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
			s.bind(("127.0.0.1", 0))
			silent = s.getsockname()[1]  # bound, never answers
			with self.assertRaises(dns_txt.DNSError):
				dns_txt.lookup("example.org", servers=["127.0.0.1"], timeout=0.3, port=silent)
		with self.assertRaises(dns_txt.DNSError):
			dns_txt.lookup("example.org", servers=[])


class TestParsing(unittest.TestCase):
	def test_wrong_id_is_refused(self):
		other = answer(dns_txt.build_query("example.org", 8), [["x"]])  # the answer to another question
		with self.assertRaises(dns_txt.DNSError):
			dns_txt.parse_response(other, 7)

	def test_invalid_names(self):
		for name in ("", ".", "a" * 64 + ".org"):
			with self.subTest(name):
				with self.assertRaises(dns_txt.DNSError):
					dns_txt.build_query(name, 1)

	def test_resolvers(self):
		import tempfile

		with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
			f.write("# comment\nsearch lan\nnameserver 192.0.2.53\nnameserver 2001:db8::53\noptions edns0\n")
			f.flush()
			self.assertEqual(dns_txt.resolvers(f.name), ["192.0.2.53", "2001:db8::53"])
		self.assertEqual(dns_txt.resolvers("/nonexistent/resolv.conf"), [])


if __name__ == "__main__":
	unittest.main()
