import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import conditions as c


def run(steps, fire_after=1, resolve_after=1, auto_resolve=True, threshold=90, resolve_threshold=None):
	"""Drive hysteresis_step over a series of values, simulating incident open/close."""
	state, is_open, actions = None, False, []
	for value in steps:
		row = None if value is None else {"value": value, "samples": 1}
		breached = row is not None and c.is_breached(row, ">", threshold)
		still = c.is_still_breached(row, ">", threshold, resolve_threshold)
		state, action = c.hysteresis_step(state, breached, still, is_open, fire_after, resolve_after, auto_resolve)
		if action == "fire":
			is_open = True
		elif action == "resolve":
			is_open = False
		actions.append(action)
	return actions


class TestHysteresis(unittest.TestCase):
	def test_defaults_match_previous_behaviour(self):
		self.assertEqual(run([95, 95, 80, 95]), ["fire", "update", "resolve", "fire"])

	def test_fire_after_consecutive(self):
		self.assertEqual(run([95, 95, 95, 95], fire_after=3), ["pending", "pending", "fire", "update"])

	def test_fire_counter_resets_on_clear(self):
		self.assertEqual(run([95, 95, 80, 95, 95, 95], fire_after=3), ["pending", "pending", None, "pending", "pending", "fire"])

	def test_missing_data_resets_fire_counter(self):
		self.assertEqual(run([95, None, 95], fire_after=2), ["pending", None, "pending"])

	def test_resolve_after_consecutive(self):
		self.assertEqual(run([95, 80, 80, 80], resolve_after=3), ["fire", "pending", "pending", "resolve"])

	def test_resolve_counter_resets_on_breach(self):
		self.assertEqual(run([95, 80, 95, 80, 80], resolve_after=2), ["fire", "pending", "update", "pending", "resolve"])

	def test_resolve_threshold_holds_incident(self):
		# fires above 90, only resolves once at or below 80
		self.assertEqual(run([95, 85, 85, 80], resolve_threshold=80), ["fire", None, None, "resolve"])

	def test_flapping_around_threshold_with_hysteresis(self):
		values = [91, 89, 91, 89, 91, 89]
		self.assertEqual(run(values).count("fire"), 3)
		self.assertEqual(run(values, resolve_threshold=80).count("fire"), 1)
		self.assertEqual(run(values, fire_after=2).count("fire"), 0)

	def test_no_auto_resolve(self):
		self.assertEqual(run([95, 10, 10], auto_resolve=False), ["fire", None, None])

	def test_missing_row_counts_as_clear(self):
		self.assertEqual(run([95, None, None], resolve_after=2), ["fire", "pending", "resolve"])

	def test_step_returns_compact_state(self):
		self.assertEqual(c.hysteresis_step(None, True, True, False, fire_after=3), ([1, 0], "pending"))
		self.assertEqual(c.hysteresis_step([1, 0], True, True, False, fire_after=3), ([2, 0], "pending"))
		self.assertEqual(c.hysteresis_step([2, 0], True, True, False, fire_after=3), (None, "fire"))
		self.assertEqual(c.hysteresis_step([2, 0], False, False, False, fire_after=3), (None, None))
		self.assertEqual(c.hysteresis_step(None, False, False, True, resolve_after=2), ([0, 1], "pending"))
		self.assertEqual(c.hysteresis_step(None, True, True, False, fire_after=0), (None, "fire"))

	def test_state_round_trip(self):
		state = {"host=a": [2, 0], "host=b": [0, 1], "host=c": [0, 0]}
		self.assertEqual(c.load_state(c.dump_state(state)), {"host=a": [2, 0], "host=b": [0, 1]})
		self.assertEqual(c.load_state(None), {})
		self.assertEqual(c.load_state("not json"), {})
		self.assertEqual(c.load_state("[1, 2]"), {})
		self.assertEqual(c.load_state('{"x": "bad", "y": [1]}'), {})

	def test_resolve_threshold_validation(self):
		self.assertIsNone(c.resolve_threshold_error(">", 90, 80))
		self.assertIsNone(c.resolve_threshold_error(">=", 90, 90))
		self.assertIsNotNone(c.resolve_threshold_error(">", 90, 95))
		self.assertIsNone(c.resolve_threshold_error("<", 10, 20))
		self.assertIsNotNone(c.resolve_threshold_error("<=", 10, 5))
		self.assertIsNotNone(c.resolve_threshold_error("==", 1, 1))


class TestSilence(unittest.TestCase):
	scope = {"host": "h1", "bench": "main", "site": "a.example.com"}

	def test_empty_silence_matches_everything(self):
		self.assertTrue(c.silence_matches({}, self.scope, "R"))
		self.assertTrue(c.silence_matches({}, {}, None))

	def test_scope_fields_must_all_match(self):
		self.assertTrue(c.silence_matches({"host": "h1", "bench": "main"}, self.scope))
		self.assertFalse(c.silence_matches({"host": "h2", "bench": "main"}, self.scope))
		self.assertTrue(c.silence_matches({"site": "a.example.com"}, self.scope))
		self.assertFalse(c.silence_matches({"site": "b.example.com"}, self.scope))

	def test_bench_silence_skips_host_level_incident(self):
		self.assertFalse(c.silence_matches({"host": "h1", "bench": "main"}, {"host": "h1"}))
		self.assertTrue(c.silence_matches({"host": "h1"}, {"host": "h1"}))

	def test_rule_restriction(self):
		self.assertTrue(c.silence_matches({"alert_rule": "High CPU"}, self.scope, "High CPU"))
		self.assertFalse(c.silence_matches({"alert_rule": "High CPU"}, self.scope, "5xx spike"))
		self.assertFalse(c.silence_matches({"alert_rule": "High CPU"}, self.scope, None))


class TestEscalationAndDigest(unittest.TestCase):
	def test_escalation_due(self):
		now = datetime(2026, 10, 29, 2, 30)
		self.assertTrue(c.escalation_due(now - timedelta(minutes=15), 15, now))
		self.assertFalse(c.escalation_due(now - timedelta(minutes=14), 15, now))
		self.assertFalse(c.escalation_due(now - timedelta(hours=5), 0, now))
		self.assertFalse(c.escalation_due(None, 15, now))

	def test_should_digest(self):
		self.assertTrue(c.should_digest("hourly", "warning", "fired"))
		self.assertTrue(c.should_digest("hourly", "info", "resolved"))
		self.assertFalse(c.should_digest("hourly", "critical", "fired"))
		self.assertFalse(c.should_digest("hourly", "warning", "escalated"))
		self.assertFalse(c.should_digest("hourly", "warning", "test"))
		self.assertFalse(c.should_digest("none", "warning", "fired"))
		self.assertFalse(c.should_digest(None, "warning", "fired"))

	def test_channel_destination_normalizes_email(self):
		a = c.channel_destination({"channel_type": "Email", "recipients": "B@x.org, a@x.org"})
		b = c.channel_destination({"channel_type": "Email", "recipients": "a@x.org\nb@x.org\n"})
		self.assertEqual(a, b)
		self.assertEqual(c.channel_destination({"channel_type": "Webhook", "webhook_url": " https://h/k "}), "https://h/k")

	def test_group_digest(self):
		items = [
			{"channel_type": "Email", "destination": "a@x.org", "incident": "1"},
			{"channel_type": "Webhook", "destination": "https://h", "incident": "2"},
			{"channel_type": "Email", "destination": "a@x.org", "incident": "3"},
			{"channel_type": "Email", "destination": "", "incident": "4"},
		]
		groups = c.group_digest(items)
		self.assertEqual(list(groups), [("Email", "a@x.org"), ("Webhook", "https://h")])
		self.assertEqual([i["incident"] for i in groups[("Email", "a@x.org")]], ["1", "3"])
