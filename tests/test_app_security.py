import unittest

from frappe_wolf.wolf import app_security as appsec

SETTINGS = {
	"enable_two_factor_auth": 1, "enable_password_policy": 1, "minimum_password_score": "3",
	"allow_consecutive_login_attempts": 5, "allow_login_after_fail": 60, "session_expiry": "06:00",
	"deny_multiple_sessions": 1, "disable_user_pass_login": 0, "logout_on_password_reset": 1,
	"allow_guests_to_upload_files": 0,
}  # fmt: skip


class TestWeakened(unittest.TestCase):
	def changed(self, **after):
		return {r["field"] for r in appsec.weakened(SETTINGS, {**SETTINGS, **after})}

	def test_each_weakening(self):
		self.assertEqual(self.changed(enable_two_factor_auth=0), {"enable_two_factor_auth"})
		self.assertEqual(self.changed(minimum_password_score="2"), {"minimum_password_score"})
		self.assertEqual(self.changed(allow_consecutive_login_attempts=10), {"allow_consecutive_login_attempts"})
		self.assertEqual(self.changed(allow_consecutive_login_attempts=0), {"allow_consecutive_login_attempts"})
		self.assertEqual(self.changed(allow_login_after_fail=10), {"allow_login_after_fail"})
		self.assertEqual(self.changed(session_expiry="240:00"), {"session_expiry"})
		self.assertEqual(self.changed(allow_guests_to_upload_files=1), {"allow_guests_to_upload_files"})
		self.assertEqual(self.changed(deny_multiple_sessions=0, logout_on_password_reset="0"),
		                 {"deny_multiple_sessions", "logout_on_password_reset"})  # fmt: skip

	def test_strengthening_and_noise_are_not_flagged(self):
		self.assertEqual(self.changed(enable_two_factor_auth=1, minimum_password_score="4", session_expiry="01:00",
		                              allow_consecutive_login_attempts=3, disable_user_pass_login=1), set())  # fmt: skip
		self.assertEqual(self.changed(session_expiry="garbage"), set())
		self.assertEqual(appsec.weakened(None, SETTINGS), [])  # first save: nothing to compare

	def test_row_says_what_changed(self):
		(row,) = appsec.weakened(SETTINGS, {**SETTINGS, "enable_two_factor_auth": 0})
		self.assertEqual(row, {"field": "enable_two_factor_auth", "label": "Two-factor authentication", "old": 1, "new": 0})


class TestScripts(unittest.TestCase):
	F = appsec.SERVER_SCRIPT_FIELDS

	def test_server_script(self):
		live = {"disabled": 0, "script": "x = 1", "script_type": "API", "api_method": "ping"}
		self.assertEqual(appsec.script_change(None, live, self.F), "created")
		self.assertEqual(appsec.script_change({**live, "disabled": 1}, live, self.F), "enabled")
		self.assertEqual(appsec.script_change(live, {**live, "script": "import os"}, self.F), "changed")
		self.assertIsNone(appsec.script_change(live, {**live, "description": "doc"}, self.F))
		self.assertIsNone(appsec.script_change(None, {**live, "disabled": 1}, self.F))  # runs nothing
		self.assertIsNone(appsec.script_change(live, {**live, "disabled": 1}, self.F))  # disabling is safe

	def test_client_script_uses_enabled(self):
		f = appsec.CLIENT_SCRIPT_FIELDS
		live = {"enabled": 1, "script": "frappe.ui.form.on()", "dt": "Item"}
		self.assertEqual(appsec.script_change(None, live, f), "created")
		self.assertEqual(appsec.script_change({**live, "enabled": 0}, live, f), "enabled")
		self.assertIsNone(appsec.script_change(None, {**live, "enabled": 0}, f))


class TestHelpers(unittest.TestCase):
	def test_url_domain(self):
		self.assertEqual(appsec.url_domain("https://Hooks.Example.com:8443/x?y=1"), "hooks.example.com")
		self.assertIsNone(appsec.url_domain("not a url"))
		self.assertIsNone(appsec.url_domain(None))

	def test_new_api_secret(self):
		self.assertTrue(appsec.new_api_secret("a1b2c3d4e5f6g7h"))
		self.assertFalse(appsec.new_api_secret("***************"))  # Desk sends saved passwords masked
		self.assertFalse(appsec.new_api_secret(None))

	def test_excerpt(self):
		self.assertEqual(appsec.excerpt("short"), "short")
		self.assertEqual(len(appsec.excerpt("x" * 1000)), 301)


if __name__ == "__main__":
	unittest.main()


class TestDocpermChange(unittest.TestCase):
	RULE = {"parent": "Sales Invoice", "role": "Accounts User", "read": 1, "write": 1, "if_owner": 0}

	def test_new_rule_grants_its_rights(self):
		self.assertEqual(appsec.docperm_change(None, {**self.RULE, "delete": 1}), {"granted": ["read", "write", "delete"], "revoked": []})

	def test_copy_of_the_standard_rule_grants_nothing(self):
		self.assertIsNone(appsec.docperm_change(dict(self.RULE), dict(self.RULE)))

	def test_rights_granted_and_revoked(self):
		self.assertEqual(appsec.docperm_change(self.RULE, {**self.RULE, "write": 0, "export": 1}), {"granted": ["export"], "revoked": ["write"]})

	def test_only_if_creator(self):
		owner = {**self.RULE, "if_owner": 1}
		self.assertEqual(appsec.docperm_change(owner, self.RULE), {"granted": ["all documents"], "revoked": []})
		self.assertEqual(appsec.docperm_change(self.RULE, owner), {"granted": [], "revoked": ["all documents"]})

	def test_rule_moved_to_another_role_grants_everything_there(self):
		self.assertEqual(appsec.docperm_change(self.RULE, {**self.RULE, "role": "Guest"}), {"granted": ["read", "write"], "revoked": []})


class TestUserPermissionWidens(unittest.TestCase):
	ROW = {"user": "a@example.com", "allow": "Company", "for_value": "ACME", "apply_to_all_doctypes": 1, "hide_descendants": 0}

	def test_first_restricts_further_ones_widen(self):
		self.assertFalse(appsec.user_permission_widens(None, self.ROW, others=0))  # ERPNext's employee row
		self.assertTrue(appsec.user_permission_widens(None, self.ROW, others=1))

	def test_removing_the_last_lifts_the_restriction(self):
		self.assertTrue(appsec.user_permission_widens(self.ROW, None, others=0))
		self.assertFalse(appsec.user_permission_widens(self.ROW, None, others=2))

	def test_edits(self):
		widens = lambda **after: appsec.user_permission_widens(self.ROW, {**self.ROW, **after}, others=0)  # noqa: E731
		self.assertTrue(widens(for_value="Other Co"))
		self.assertTrue(widens(user="b@example.com"))
		self.assertTrue(widens(apply_to_all_doctypes=0, applicable_for="Sales Invoice"))
		self.assertFalse(widens(hide_descendants=1))  # narrows
		self.assertFalse(widens(is_default=1))  # preselects a value only
