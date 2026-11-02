import unittest

from frappe_wolf.wolf import posture

SSTI = {"id": "GHSA-aaaa", "cve": None, "severity": "critical", "summary": "SSTI", "url": "https://github.com/x",
        "published": "2026-10-15", "ranges": [{"range": "<16.21.0", "patched": "16.21.0"}, {"range": "<15.111.0", "patched": "15.111.0"}]}
XSS = {"id": "GHSA-bbbb", "cve": None, "severity": "medium", "summary": "XSS", "url": "https://github.com/y",
       "published": "2026-09-01", "ranges": [{"range": "< v16.30.0", "patched": "v16.30.0"}]}
SOURCES = {"frappe": {"advisories": [SSTI, XSS], "why": None}, "erpnext": {"advisories": [], "why": None}}

GOOD = {
	"settings": {"enable_two_factor_auth": 1, "enable_password_policy": 1, "minimum_password_score": "3",
	             "allow_consecutive_login_attempts": 5, "allow_guests_to_upload_files": 0},
	"system_manager_2fa": True,
	"site_config_findings": [],
	"exposed_services": {"bench-01": 0},
	"backup_age_hours": 3.5,
	"tls_days_left": 60,
	"hsts_max_age_days": {"erp.example.org": 365},
	"spf": {"example.org": (True, "ends in -all")},
	"dmarc": {"example.org": (True, "p=reject")},
	"app_versions": {"frappe": {(16, 35, 0)}},
	"advisories": SOURCES,
}  # fmt: skip


def status(result):
	return {c["id"]: c["status"] for c in result["checks"]}


class TestPosture(unittest.TestCase):
	def test_all_good(self):
		r = posture.evaluate(GOOD)
		self.assertEqual(set(status(r).values()), {"pass"})
		self.assertEqual((r["score"], r["coverage"], r["failed"], r["unknown"]), (100, 100, 0, 0))
		self.assertTrue(all(c["fix"] is None for c in r["checks"]))

	def test_failures_carry_evidence_and_fix(self):
		bad = {**GOOD, "settings": {**GOOD["settings"], "enable_two_factor_auth": 0, "allow_guests_to_upload_files": 1},
		       "site_config_findings": ["developer_mode"], "exposed_services": {"bench-01": 2}, "tls_days_left": 3}  # fmt: skip
		r = posture.evaluate(bad)
		s = status(r)
		self.assertEqual([k for k, v in s.items() if v == "fail"], ["two_factor", "guest_uploads", "site_config", "exposure", "tls"])
		two_fa = next(c for c in r["checks"] if c["id"] == "two_factor")
		self.assertEqual(two_fa["evidence"], "disabled in System Settings")
		self.assertIn("System Manager role", two_fa["fix"])
		self.assertEqual(r["score"], round(100 * (2 + 1 + 2 + 3 + 3) / 23))

	def test_2fa_needs_the_role_too(self):
		r = posture.evaluate({**GOOD, "system_manager_2fa": False})
		self.assertEqual(status(r)["two_factor"], "fail")

	def test_lockout_bounds(self):
		for attempts, expected in ((0, "fail"), (1, "pass"), (10, "pass"), (11, "fail"), (None, "fail")):
			with self.subTest(attempts):
				r = posture.evaluate({**GOOD, "settings": {**GOOD["settings"], "allow_consecutive_login_attempts": attempts}})
				self.assertEqual(status(r)["login_lockout"], expected)

	def test_unknown_never_counts_as_passed(self):
		remote = {"settings": None, "site_config_risks": 0, "exposed_services": {}, "backup_age_hours": None}
		r = posture.evaluate(remote)
		s = status(r)
		self.assertEqual(s["two_factor"], "unknown")
		self.assertEqual(s["site_config"], "pass")  # from the agent's count
		self.assertEqual(s["exposure"], "unknown")
		self.assertEqual((s["hsts"], s["spf"], s["dmarc"]), ("unknown", "unknown", "unknown"))
		self.assertEqual(s["advisories"], "unknown")
		self.assertEqual(r["score"], round(100 * 3 / 23))
		self.assertEqual(r["coverage"], round(100 * 3 / 23))
		self.assertEqual(r["unknown"], 11)

	def test_site_findings_win_over_the_agent_count(self):
		r = posture.evaluate({**GOOD, "site_config_findings": None, "site_config_risks": 2})
		self.assertEqual(status(r)["site_config"], "fail")
		r = posture.evaluate({**GOOD, "site_config_findings": [], "site_config_risks": 2})
		self.assertEqual(status(r)["site_config"], "pass")

	def test_exposure_unknown_hosts_are_ignored(self):
		r = posture.evaluate({**GOOD, "exposed_services": {"a": None, "b": 0}})
		self.assertEqual(status(r)["exposure"], "pass")

	def test_weights_sum(self):
		self.assertEqual(sum(c[2] for c in posture.CHECKS), 23)

	def test_hsts(self):
		r = posture.evaluate({**GOOD, "hsts_max_age_days": {"erp.example.org": 365, "shop.example.org": 0, "old.example.org": 30}})
		hsts = next(c for c in r["checks"] if c["id"] == "hsts")
		self.assertEqual(hsts["status"], "fail")
		self.assertEqual(hsts["evidence"], "old.example.org: 30 days; shop.example.org: no header")
		self.assertIn("Strict-Transport-Security", hsts["fix"])

	def test_mail_unknown_when_dns_did_not_answer(self):
		r = posture.evaluate({**GOOD, "spf": {"example.org": None}, "dmarc": {}})
		self.assertEqual((status(r)["spf"], status(r)["dmarc"]), ("unknown", "unknown"))


class TestSPF(unittest.TestCase):
	def test_verdicts(self):
		cases = [
			(["v=spf1 include:_spf.google.com -all"], (True, "ends in -all")),
			(["v=spf1 mx ~all", "google-site-verification=x"], (True, "ends in ~all")),
			(["v=spf1 redirect=_spf.example.net"], (True, "delegated with redirect=_spf.example.net (not followed)")),
			(["v=spf1 +all"], (False, "ends in +all: any sender is accepted")),
			(["v=spf1 a ?all"], (False, "ends in ?all: neutral")),
			(["v=spf1 a mx"], (False, "no all term: any other sender passes as neutral")),
			(["v=spf1 -all", "v=spf1 mx -all"], (False, "2 SPF records: receivers treat that as an error")),
			([], (False, "no SPF record")),
		]
		for records, expected in cases:
			with self.subTest(records):
				self.assertEqual(posture.spf_verdict(records), expected)


class TestDMARC(unittest.TestCase):
	def test_verdicts(self):
		self.assertEqual(posture.dmarc_verdict(["v=DMARC1; p=reject; rua=mailto:d@example.org"]), (True, "p=reject"))
		self.assertEqual(posture.dmarc_verdict(["v=DMARC1;p=quarantine;pct=25"]), (True, "p=quarantine, applied to 25% of messages"))
		self.assertEqual(posture.dmarc_verdict(["v=DMARC1; p=none"]),
		                 (False, "p=none: reports only, spoofed mail is still delivered"))  # fmt: skip
		self.assertEqual(posture.dmarc_verdict([]), (False, "no DMARC record"))
		# a subdomain follows sp= of its organisational domain when there is one
		self.assertEqual(posture.dmarc_verdict(["v=DMARC1; p=reject; sp=none"], subdomain=True)[0], False)
		self.assertEqual(posture.dmarc_verdict(["v=DMARC1; p=reject"], subdomain=True), (True, "p=reject"))


class TestAdvisoryCheck(unittest.TestCase):
	def check(self, versions, sources=SOURCES):
		return posture.check_advisories(versions, sources)

	def test_affected_names_the_fix(self):
		row = self.check({"frappe": {(16, 20, 0)}})
		self.assertEqual(row["status"], "fail")
		self.assertEqual(row["evidence"], "frappe 16.20.0: 2 advisories (1 critical, 1 medium), fixed in 16.30.0 (GHSA-aaaa, GHSA-bbbb)")
		self.assertIn("bench update", row["fix"])
		# the 15 line only knows the SSTI fix
		self.assertIn("frappe 15.110.0: 1 advisory (1 critical), fixed in 15.111.0", self.check({"frappe": {(15, 110, 0)}})["evidence"])

	def test_clear(self):
		row = self.check({"frappe": {(16, 30, 0)}, "erpnext": {(16, 1, 0)}})
		self.assertEqual(row["status"], "unknown")  # no erpnext advisory names any line: cannot tell
		row = self.check({"frappe": {(16, 30, 0), (15, 111, 0)}})
		self.assertEqual((row["status"], row["evidence"]), ("pass", "frappe 15.111.0, frappe 16.30.0: no published advisory applies"))

	def test_unknown(self):
		self.assertEqual(self.check({"frappe": {(16, 35, 0)}}, None)["evidence"], "the advisory check is off in Wolf Settings")
		self.assertIn("no app_version sample", self.check({})["evidence"])
		self.assertIn("no app_version sample", self.check({"frappe": set()})["evidence"])
		row = self.check({"frappe": {(4, 10, 0)}})  # a fork numbered differently
		self.assertEqual(row["status"], "unknown")
		self.assertIn("not 4.x", row["evidence"])
		row = self.check({"frappe": {(16, 35, 0)}}, {"frappe": {"advisories": None, "why": "advisories last fetched on 2026-10-01"}})
		self.assertEqual((row["status"], row["evidence"]), ("unknown", "frappe 16.35.0: advisories last fetched on 2026-10-01"))

	def test_one_affected_version_fails_the_site(self):
		row = self.check({"frappe": {(16, 35, 0), (16, 0, 1)}, "erpnext": {(4, 0, 0)}})
		self.assertEqual(row["status"], "fail")
		self.assertTrue(row["evidence"].startswith("frappe 16.0.1: 2 advisories"))
		self.assertIn("erpnext 4.0.0", row["evidence"])


if __name__ == "__main__":
	unittest.main()
