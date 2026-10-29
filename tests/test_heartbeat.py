import unittest
from datetime import datetime

from frappe_wolf.wolf import heartbeat as hb


def health(rules_age=None, job_age=None, **extra):
	return {"rules": {"age_seconds": rules_age}, "scheduler": {"age_seconds": job_age, "enabled": True}, **extra}


class TestHeartbeat(unittest.TestCase):
	def test_to_utc_iso(self):
		self.assertEqual(hb.to_utc_iso(datetime(2026, 10, 29, 3, 0, 13), "Europe/Paris"), "2026-10-29T02:00:13Z")
		self.assertEqual(hb.to_utc_iso(datetime(2026, 7, 1, 12, 0), "UTC"), "2026-07-01T12:00:00Z")
		self.assertIsNone(hb.to_utc_iso(None, "UTC"))

	def test_age_is_signed(self):
		now = datetime(2026, 10, 29, 2, 0)
		self.assertEqual(hb.age_seconds(datetime(2026, 10, 29, 1, 58, 30), now), 90)
		self.assertEqual(hb.age_seconds(datetime(2026, 10, 29, 3, 0), now), -3600)
		self.assertIsNone(hb.age_seconds(None, now))

	def test_fresh_is_healthy(self):
		self.assertEqual(hb.assess(health(rules_age=40, job_age=30)), [])
		self.assertEqual(hb.assess(health()), [])  # nothing ran yet: unknown, not alarming
		self.assertEqual(hb.assess(None), [])

	def test_stale_rules(self):
		problems = hb.assess(health(rules_age=720, job_age=10))
		self.assertEqual(problems, ["alert rules not evaluated for 12 min"])
		self.assertEqual(hb.assess(health(rules_age=720), stale_minutes=15), [])

	def test_falls_back_to_scheduled_jobs(self):
		self.assertEqual(hb.assess(health(job_age=600)), ["scheduled jobs not evaluated for 10 min"])

	def test_future_timestamp_after_tz_change(self):
		(problem,) = hb.assess(health(rules_age=-7200))
		self.assertIn("120 min in the future", problem)
		self.assertEqual(hb.assess(health(rules_age=-30)), [])

	def test_context_is_appended(self):
		h = health(rules_age=400, queues={"error": "Connection refused"})
		h["scheduler"].update(enabled=False, reason="pause_scheduler")
		(problem,) = hb.assess(h)
		self.assertIn("scheduler disabled (pause_scheduler)", problem)
		self.assertIn("job queue unreachable: Connection refused", problem)
		(problem,) = hb.assess(health(rules_age=400, queues={"lengths": {}, "workers": 0}))
		self.assertIn("no background worker", problem)
		# context alone is not a problem
		self.assertEqual(hb.assess(health(rules_age=10, queues={"workers": 0})), [])


if __name__ == "__main__":
	unittest.main()
