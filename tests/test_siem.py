import json
import unittest
from datetime import datetime, timezone

from frappe_wolf.wolf import siem

EVENT = {
	"name": "abc123", "event_type": "security", "category": "web_attack_probe", "severity": "critical",
	"title": "60 web attack probe(s) from 45.33.32.156 (env_file|vcs)", "source": "nginx", "count": 60,
	"timestamp": datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc), "host": "bench-01", "bench": "main",
	"site": "erp.example.com", "incident": "WINC-00042",
	"payload": {"ip": "45.33.32.156", "reputation": {"listed": True, "lists": ["firehol_level1"]},
	            "mitre_attack": [{"id": "T1595.003", "name": "Active Scanning: Wordlist Scanning", "tactic": "reconnaissance"}]},
}  # fmt: skip


class TestCef(unittest.TestCase):
	def test_header_and_extensions(self):
		line = siem.cef(EVENT, "0.1.0")
		self.assertTrue(line.startswith(
			"CEF:0|Dokos|Frappe Wolf|0.1.0|web_attack_probe|60 web attack probe(s) from 45.33.32.156 (env_file\\|vcs)|9|"))  # fmt: skip
		self.assertIn("src=45.33.32.156", line)
		self.assertIn("cs4Label=mitre_attack cs4=T1595.003", line)
		self.assertIn("cs5=firehol_level1", line)
		self.assertIn("rt=1793527200000", line)

	def test_escaping(self):
		line = siem.cef({**EVENT, "title": "a=b\\c\nd", "payload": {}})
		self.assertIn("msg=a\\=b\\\\c\\nd", line)


class TestOcsf(unittest.TestCase):
	def test_detection_finding(self):
		f = siem.ocsf(EVENT, "0.1.0")
		self.assertEqual((f["class_uid"], f["category_uid"], f["type_uid"], f["severity_id"]), (2004, 2, 200401, 5))
		self.assertEqual(f["finding_info"]["attacks"][0], {"technique": {"uid": "T1595.003", "name": "Active Scanning: Wordlist Scanning"},
		                                                   "tactic": {"name": "reconnaissance"}})  # fmt: skip
		self.assertEqual(f["src_endpoint"], {"ip": "45.33.32.156"})
		self.assertEqual(f["unmapped"]["site"], "erp.example.com")
		self.assertEqual(f["time"], 1793527200000)
		json.dumps(f)

	def test_big_payload_is_truncated(self):
		f = siem.ocsf({**EVENT, "payload": {"sample_lines": ["x" * 9000]}})
		self.assertTrue(f["unmapped"]["payload"]["truncated"])
		self.assertNotIn("src_endpoint", f)


class TestSyslog(unittest.TestCase):
	def test_rfc5424(self):
		msg = siem.syslog("hello", "erp.example.com", "critical", "web_attack_probe", datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))
		self.assertEqual(msg, "<130>1 2026-11-01T10:00:00.000Z erp.example.com frappe-wolf - web_attack_probe - hello")
		self.assertTrue(siem.syslog("x", "h", "info").startswith("<134>1 "))
		self.assertIn(" myhost ", siem.syslog("x", "my host", "warning"))  # header fields carry no spaces

	def test_frames_round_trip(self):
		data = siem.frame("é one") + siem.frame("two")
		self.assertEqual(siem.unframe(data), ["é one", "two"])
		self.assertTrue(siem.frame("é").startswith(b"2 "))  # bytes, not characters

	def test_render(self):
		ocsf = siem.render("event", EVENT, "ocsf", "erp.example.com")
		self.assertEqual(json.loads(ocsf.split(" - ", 2)[2])["class_uid"], 2004)
		entry = {"seq": 7, "at": datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc), "kind": "incident", "ref_doctype": "Wolf Incident",
		         "ref_name": "WINC-1", "summary": "WINC-1 changed: status", "digest": "d" * 64, "prev_mac": "p" * 64, "mac": "m" * 64}  # fmt: skip
		j = json.loads(siem.render("journal", entry, "ocsf", "h").split(" - ", 2)[2])
		self.assertEqual((j["wolf_journal"]["seq"], j["wolf_journal"]["mac"]), (7, "m" * 64))
		c = siem.render("journal", entry, "cef", "h")
		self.assertIn("journal:incident", c)
		self.assertIn("cs4=" + "m" * 64, c)


if __name__ == "__main__":
	unittest.main()
