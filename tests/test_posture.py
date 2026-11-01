import unittest

from frappe_wolf.wolf import posture

GOOD = {
	"settings": {"enable_two_factor_auth": 1, "enable_password_policy": 1, "minimum_password_score": "3",
	             "allow_consecutive_login_attempts": 5, "allow_guests_to_upload_files": 0},
	"system_manager_2fa": True,
	"site_config_findings": [],
	"exposed_services": {"bench-01": 0},
	"backup_age_hours": 3.5,
	"tls_days_left": 60,
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
		self.assertEqual(r["score"], round(100 * (2 + 1 + 2) / 17))

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
		self.assertEqual(r["score"], round(100 * 3 / 17))
		self.assertEqual(r["coverage"], round(100 * 3 / 17))
		self.assertEqual(r["unknown"], 7)

	def test_site_findings_win_over_the_agent_count(self):
		r = posture.evaluate({**GOOD, "site_config_findings": None, "site_config_risks": 2})
		self.assertEqual(status(r)["site_config"], "fail")
		r = posture.evaluate({**GOOD, "site_config_findings": [], "site_config_risks": 2})
		self.assertEqual(status(r)["site_config"], "pass")

	def test_exposure_unknown_hosts_are_ignored(self):
		r = posture.evaluate({**GOOD, "exposed_services": {"a": None, "b": 0}})
		self.assertEqual(status(r)["exposure"], "pass")

	def test_weights_sum(self):
		self.assertEqual(sum(c[2] for c in posture.CHECKS), 17)


if __name__ == "__main__":
	unittest.main()
