"""siem.Sender against real local collectors: TCP, UDP and TLS (a CA made for the test)."""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

from frappe_wolf.wolf import siem

MESSAGES = ["<134>1 2026-11-02T10:00:00.000Z h frappe-wolf - x - one", "<134>1 2026-11-02T10:00:00.000Z h frappe-wolf - x - deux é\nligne"]


def make_ca_and_server(tmp):
	"""(ca.pem, server.pem, server.key): a CA, and a server certificate it signed for 127.0.0.1 only."""
	def run(*args):
		subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=tmp)

	# the extensions Python's strict verification (3.13+) asks of a CA and a server certificate
	run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=Wolf test CA",
	    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
	    "-addext", "subjectKeyIdentifier=hash")  # fmt: skip
	run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=collector")
	with open(os.path.join(tmp, "san.cnf"), "w") as f:
		f.write("subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n"
		        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
		        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")  # fmt: skip
	run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "server.pem",
	    "-days", "2", "-extfile", "san.cnf")  # fmt: skip
	return tuple(os.path.join(tmp, n) for n in ("ca.pem", "server.pem", "server.key"))


class Collector:
	"""A one-connection-at-a-time stream collector; `tls` is an SSLContext or None."""

	def __init__(self, tls=None):
		self.sock = socket.create_server(("127.0.0.1", 0))
		self.port = self.sock.getsockname()[1]
		self.tls, self.data, self.errors = tls, [], []
		self.done = threading.Event()
		self.thread = threading.Thread(target=self.serve, daemon=True)
		self.thread.start()

	def serve(self):
		try:
			conn, _ = self.sock.accept()
			with conn:
				if self.tls:
					conn = self.tls.wrap_socket(conn, server_side=True)
				conn.settimeout(5)
				chunks = []
				while True:
					try:
						chunk = conn.recv(65536)
					except ssl.SSLError as e:
						self.errors.append(e)
						break
					if not chunk:
						break
					chunks.append(chunk)
				self.data.append(b"".join(chunks))
				if self.tls:
					try:
						conn.unwrap()
					except (OSError, ssl.SSLError):
						pass
				conn.close()
		except (OSError, ssl.SSLError) as e:
			self.errors.append(e)
		finally:
			self.done.set()

	def received(self):
		self.done.wait(5)
		self.sock.close()
		return b"".join(self.data)


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to make the test certificates")
class TestSender(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.tmp = tempfile.TemporaryDirectory()
		cls.ca, cls.cert, cls.key = make_ca_and_server(cls.tmp.name)

	@classmethod
	def tearDownClass(cls):
		cls.tmp.cleanup()

	def server_context(self):
		context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
		context.load_cert_chain(self.cert, self.key)
		return context

	def test_tcp_octet_counting(self):
		c = Collector()
		siem.Sender("127.0.0.1", c.port, "tcp").send(MESSAGES)
		self.assertEqual(siem.unframe(c.received()), MESSAGES)

	def test_tcp_newline(self):
		c = Collector()
		siem.Sender("127.0.0.1", c.port, "tcp", framing="newline").send(MESSAGES)
		lines = c.received().decode("utf-8").split("\n")
		self.assertEqual(lines, [MESSAGES[0], MESSAGES[1].replace("\n", " "), ""])  # one message per line

	def test_tls_with_the_collector_ca(self):
		c = Collector(self.server_context())
		siem.Sender("127.0.0.1", c.port, "tls", ca_file=self.ca).send(MESSAGES)
		self.assertEqual(siem.unframe(c.received()), MESSAGES)
		self.assertEqual(c.errors, [])  # closed with close_notify, not cut

	def test_tls_refuses_an_unknown_ca(self):
		c = Collector(self.server_context())
		with self.assertRaises(ssl.SSLCertVerificationError):
			siem.Sender("127.0.0.1", c.port, "tls").send(MESSAGES)  # system CAs only
		c.received()

	def test_tls_without_verification(self):
		c = Collector(self.server_context())
		siem.Sender("127.0.0.1", c.port, "tls", verify_tls=False).send(MESSAGES)
		self.assertEqual(siem.unframe(c.received()), MESSAGES)

	def test_tls_name_must_match(self):
		c = Collector(self.server_context())
		with self.assertRaises(ssl.SSLCertVerificationError):
			siem.Sender("localhost", c.port, "tls", ca_file=self.ca, timeout=3).send(MESSAGES)  # a name the certificate lacks
		c.received()

	def test_missing_ca_file_is_an_oserror(self):
		with self.assertRaises(OSError):
			siem.Sender("127.0.0.1", 1, "tls", ca_file="/nonexistent/ca.pem").send(MESSAGES)

	def test_udp_one_datagram_per_message(self):
		with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
			s.bind(("127.0.0.1", 0))
			s.settimeout(5)
			big = "x" * 9000 + "é"
			siem.Sender("127.0.0.1", s.getsockname()[1], "udp").send([MESSAGES[0], big])
			first, second = s.recv(65535), s.recv(65535)
		self.assertEqual(first.decode("utf-8"), MESSAGES[0])
		self.assertEqual(len(second), siem.MAX_UDP)

	def test_datagram_never_splits_a_character(self):
		self.assertEqual(siem.datagram("aé", limit=2), b"a")
		self.assertEqual(siem.datagram("abc"), b"abc")

	def test_nothing_to_send(self):
		siem.Sender("127.0.0.1", 1, "tcp").send([])  # no connection attempted


if __name__ == "__main__":
	unittest.main()
