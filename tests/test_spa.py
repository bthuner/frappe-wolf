import unittest

from frappe_wolf.wolf import spa


class TestAccess(unittest.TestCase):
	def test_guests_log_in_first(self):
		self.assertEqual(spa.access("Guest", ["Guest"]), "login")
		self.assertEqual(spa.access(None, []), "login")

	def test_wolf_roles_may_open(self):
		for role in ("Wolf User", "Wolf Manager", "System Manager"):
			self.assertIsNone(spa.access("ops@example.com", ["All", role]))
		self.assertIsNone(spa.access("Administrator", []))

	def test_other_users_are_forbidden(self):
		self.assertEqual(spa.access("clerk@example.com", ["All", "Accounts User"]), "forbidden")


class TestPermissions(unittest.TestCase):
	def test_wolf_user_reads_and_comments_only(self):
		can = spa.permissions("ops@example.com", ["Wolf User"])
		self.assertEqual(can, {"read": True, "comment": True, "act": False, "configure": False})

	def test_managers_act(self):
		for role in ("Wolf Manager", "System Manager"):
			can = spa.permissions("lead@example.com", [role])
			self.assertTrue(can["act"] and can["configure"])
		self.assertTrue(spa.permissions("Administrator", [])["act"])

	def test_no_role_no_rights(self):
		self.assertFalse(any(spa.permissions("clerk@example.com", ["Accounts User"]).values()))


class TestBoot(unittest.TestCase):
	def boot(self, **kw):
		args = dict(
			user="ops@example.com", full_name="Ops", roles=["Wolf User"], time_zone="Europe/Paris",
			server_version="0.1.0", is_satellite=False, restricted=True,
		)  # fmt: skip
		return spa.boot(**{**args, **kw})

	def test_logs_are_not_offered_without_a_backend(self):
		logs = self.boot()["capabilities"]["logs"]
		self.assertFalse(logs["available"])
		self.assertIn("raw log lines", logs["reason"])

	def test_satellite_has_no_incidents(self):
		self.assertFalse(self.boot(is_satellite=True)["capabilities"]["incidents"]["available"])
		self.assertTrue(self.boot(is_satellite=True)["satellite"])

	def test_scope_and_zone_are_passed_through(self):
		data = self.boot()
		self.assertTrue(data["capabilities"]["restricted_scope"])
		self.assertEqual(data["time_zone"], "Europe/Paris")
		self.assertEqual(data["user"], {"name": "ops@example.com", "full_name": "Ops"})
		self.assertEqual(self.boot(full_name=None)["user"]["full_name"], "ops@example.com")

	def test_every_fallback_is_a_desk_route(self):
		for url in self.boot()["desk"]["fallbacks"].values():
			self.assertTrue(url.startswith("/app/"), url)


if __name__ == "__main__":
	unittest.main()
