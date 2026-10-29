import unittest
from urllib.parse import parse_qs, urlparse

from frappe_wolf.wolf import links

SECRET = "s3cret"
NOW = 1_800_000_000


class TestLinks(unittest.TestCase):
	def params(self, **kw):
		return links.build_params(SECRET, "WINC-00001", kw.pop("action", "ack"), 3600, now=NOW, **kw)

	def verify(self, p, **kw):
		return links.verify(
			kw.pop("secret", SECRET), p["incident"], p["action"], p["expires"], p["sig"], p.get("actor"), now=kw.pop("now", NOW)
		)

	def test_round_trip(self):
		p = self.params()
		self.assertEqual(p["expires"], NOW + 3600)
		self.assertEqual(self.verify(p), (True, None))

	def test_round_trip_with_actor(self):
		p = self.params(actor="ops@example.com")
		self.assertEqual(self.verify(p), (True, None))

	def test_expired(self):
		p = self.params()
		self.assertEqual(self.verify(p, now=NOW + 3601), (False, "expired"))
		self.assertEqual(self.verify(p, now=NOW + 3600), (True, None))

	def test_tampering_is_rejected(self):
		p = self.params(actor="ops@example.com")
		for field, value in (
			("incident", "WINC-00002"),
			("action", "resolve"),
			("expires", NOW + 999999),
			("actor", "boss@example.com"),
			("sig", "0" * 64),
		):
			with self.subTest(field=field):
				self.assertEqual(self.verify(dict(p, **{field: value})), (False, "invalid"))
		without_actor = dict(p)
		del without_actor["actor"]
		self.assertEqual(self.verify(without_actor), (False, "invalid"))

	def test_wrong_secret(self):
		self.assertEqual(self.verify(self.params(), secret="other"), (False, "invalid"))

	def test_garbage_input(self):
		self.assertEqual(links.verify(SECRET, "WINC-1", "delete", NOW, "x", now=NOW), (False, "invalid"))
		self.assertEqual(links.verify(SECRET, "WINC-1", "ack", "soon", "x", now=NOW), (False, "invalid"))
		self.assertEqual(links.verify("", "WINC-1", "ack", NOW, "x", now=NOW), (False, "invalid"))
		self.assertEqual(links.verify(SECRET, "WINC-1", "ack", NOW, None, now=NOW), (False, "invalid"))
		with self.assertRaises(ValueError):
			links.sign(SECRET, "WINC-1", "delete", NOW)

	def test_actions_sign_differently(self):
		self.assertNotEqual(self.params(action="ack")["sig"], self.params(action="resolve")["sig"])

	def test_build_url(self):
		url = links.build_url("https://wolf.example.com/api/method/x", SECRET, "WINC-1", "resolve", 60, actor="a@b.c", now=NOW)
		parsed = urlparse(url)
		self.assertEqual(parsed.path, "/api/method/x")
		q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
		self.assertEqual(q["actor"], "a@b.c")
		self.assertEqual(links.verify(SECRET, q["incident"], q["action"], q["expires"], q["sig"], q["actor"], now=NOW), (True, None))
