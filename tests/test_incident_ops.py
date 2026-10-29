import json
import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import incident_ops as ops

T0 = datetime(2026, 10, 29, 2, 0, 0)


def at(minutes):
	return T0 + timedelta(minutes=minutes)


class TestHelpers(unittest.TestCase):
	def test_to_datetime(self):
		self.assertEqual(ops.to_datetime("2026-10-29 02:00:00"), T0)
		self.assertEqual(ops.to_datetime("2026-10-29 02:00:00.123456"), T0.replace(microsecond=123456))
		self.assertEqual(ops.to_datetime("2026-10-29T02:00:00"), T0)
		self.assertEqual(ops.to_datetime(T0), T0)
		self.assertIsNone(ops.to_datetime(None))
		self.assertIsNone(ops.to_datetime(""))
		self.assertIsNone(ops.to_datetime("not a date"))

	def test_max_severity(self):
		self.assertEqual(ops.max_severity("info", "critical", "warning"), "critical")
		self.assertEqual(ops.max_severity("info", None, "bogus"), "info")
		self.assertIsNone(ops.max_severity(None))

	def test_minutes_between_and_duration(self):
		self.assertEqual(ops.minutes_between(T0, at(90)), 90.0)
		self.assertIsNone(ops.minutes_between(at(5), T0))
		self.assertIsNone(ops.minutes_between(T0, None))
		self.assertEqual(ops.format_duration(0), "0 min")
		self.assertEqual(ops.format_duration(61), "1 h 1 min")
		self.assertEqual(ops.format_duration(1440 + 120), "1 d 2 h")
		self.assertEqual(ops.format_duration(None), "unknown")


class TestMerge(unittest.TestCase):
	def test_check_merge(self):
		a = {"name": "WINC-1", "merged_into": None}
		b = {"name": "WINC-2", "merged_into": None}
		self.assertIsNone(ops.check_merge(a, b))
		self.assertIn("itself", ops.check_merge(a, dict(a)))
		self.assertIn("already merged", ops.check_merge({**a, "merged_into": "WINC-3"}, b))
		self.assertIn("WINC-9", ops.check_merge(a, {**b, "merged_into": "WINC-9"}))
		self.assertIsNotNone(ops.check_merge(None, b))

	def test_target_covers_source_window_and_severity(self):
		src = {"started_at": at(-30), "last_event_at": at(50), "severity": "critical",
		       "deployment_event": "EV-D", "deployment_at": at(-35)}
		tgt = {"started_at": T0, "last_event_at": at(40), "severity": "warning", "deployment_event": None}
		self.assertEqual(
			ops.merged_target_fields(src, tgt),
			{"started_at": at(-30), "last_event_at": at(50), "severity": "critical",
			 "deployment_event": "EV-D", "deployment_at": at(-35)},
		)

	def test_target_keeps_what_it_already_covers(self):
		src = {"started_at": at(10), "last_event_at": at(20), "severity": "info", "deployment_event": "EV-X"}
		tgt = {"started_at": T0, "last_event_at": at(40), "severity": "critical", "deployment_event": "EV-D"}
		self.assertEqual(ops.merged_target_fields(src, tgt), {})

	def test_follow_merges(self):
		chain = {"A": "B", "B": "C", "C": None}
		self.assertEqual(ops.follow_merges("A", chain.get), "C")
		self.assertEqual(ops.follow_merges("C", chain.get), "C")
		self.assertIsNone(ops.follow_merges("A", {"A": "B", "B": "A"}.get))
		long_chain = {str(i): str(i + 1) for i in range(50)}
		self.assertIsNone(ops.follow_merges("0", long_chain.get, max_depth=10))


class TestSplit(unittest.TestCase):
	def test_parse_names(self):
		self.assertEqual(ops.parse_names('["a", "b", "a"]'), ["a", "b"])
		self.assertEqual(ops.parse_names("a, b\nc"), ["a", "b", "c"])
		self.assertEqual(ops.parse_names(["x", " ", "y"]), ["x", "y"])
		self.assertEqual(ops.parse_names(None), [])
		self.assertEqual(ops.parse_names("[broken"), [])

	def test_check_split(self):
		linked = ["e1", "e2", "e3"]
		self.assertEqual(ops.check_split(linked, ["e1", "e3"]), (None, ["e1", "e3"]))
		self.assertEqual(ops.check_split(linked, json.dumps(["e2"])), (None, ["e2"]))
		err, names = ops.check_split(linked, [])
		self.assertTrue(err)
		self.assertEqual(names, [])
		err, _ = ops.check_split(linked, ["e1", "other"])
		self.assertIn("other", err)
		err, _ = ops.check_split(linked, linked)
		self.assertIn("Keep at least one", err)

	def test_split_fields_and_title(self):
		events = [
			{"title": "late", "timestamp": at(20), "last_seen": at(45), "severity": "warning"},
			{"title": "early", "timestamp": at(5), "last_seen": None, "severity": "critical"},
		]
		self.assertEqual(
			ops.split_fields(events), {"started_at": at(5), "last_event_at": at(45), "severity": "critical"}
		)
		self.assertEqual(ops.split_title("Source", events), "early")
		self.assertEqual(ops.split_title("Source", [{"timestamp": T0}]), "Split from Source")
		self.assertEqual(ops.split_fields([{"timestamp": T0}])["severity"], "warning")


def version(minutes, owner, *changes):
	return {"owner": owner, "creation": at(minutes), "data": json.dumps({"changed": [list(c) for c in changes]})}


class TestActivity(unittest.TestCase):
	def test_rule_incident_with_versions_and_comments(self):
		inc = {"name": "WINC-1", "alert_rule": "502 spike", "owner": "Administrator", "started_at": T0,
		       "acknowledged_at": at(5), "acknowledged_by": "ops@example.com",
		       "resolved_at": at(30), "resolved_by": "ops@example.com"}
		versions = [
			version(5, "ops@example.com", ("status", "Open", "Acknowledged")),
			version(10, "lead@example.com", ("assigned_to", None, "ops@example.com"), ("notes", "", "x")),
			version(30, "ops@example.com", ("status", "Acknowledged", "Resolved")),
		]
		comments = [
			{"name": "c1", "comment_type": "Comment", "content": "<p>looking</p>", "owner": "ops@example.com",
			 "comment_email": "ops@example.com", "creation": at(6)},
			{"name": "c2", "comment_type": "Like", "content": "", "owner": "x", "creation": at(7)},
			{"name": "c3", "comment_type": "Info", "content": "Merged WINC-2", "owner": "a", "creation": at(8)},
		]
		out = ops.build_activity(inc, versions, comments)
		self.assertEqual(
			[(e["kind"], e.get("to") or e.get("comment_type")) for e in out],
			[("opened", None), ("status", "Acknowledged"), ("comment", "Comment"), ("note", "Info"),
			 ("field", "ops@example.com"), ("status", "Resolved")],
		)
		# version entries cover ack/resolve: no synthetic duplicates
		self.assertEqual(sum(1 for e in out if e["kind"] == "status"), 2)
		self.assertEqual(ops.describe_activity(out[0]), "Incident opened by rule 502 spike")
		self.assertEqual(ops.describe_activity(out[1], "Ops"), "Ops acknowledged")
		self.assertEqual(ops.describe_activity(out[4], "Lead"), "Lead set assigned to to ops@example.com")

	def test_auto_resolve_without_version_is_synthesized(self):
		inc = {"name": "WINC-1", "alert_rule": "disk", "started_at": T0, "resolved_at": at(12), "resolved_by": None}
		out = ops.build_activity(inc, [], [])
		self.assertEqual([e["kind"] for e in out], ["opened", "status"])
		self.assertTrue(out[1]["auto"])
		self.assertEqual(ops.describe_activity(out[1]), "Automatically resolved (condition cleared)")

	def test_link_ack_far_from_version_is_kept(self):
		# acknowledged by link at +5, an unrelated old Version to Acknowledged long before
		inc = {"name": "WINC-1", "started_at": T0, "owner": "me@example.com",
		       "acknowledged_at": at(60), "acknowledged_by": "link"}
		out = ops.build_activity(inc, [version(1, "x", ("status", "Open", "Acknowledged"))], [])
		acks = [e for e in out if e.get("to") == "Acknowledged"]
		self.assertEqual(len(acks), 2)
		self.assertEqual(out[0]["user"], "me@example.com")  # manual incident: opened by its owner
		self.assertEqual(ops.describe_activity(out[0], "Me"), "Incident opened by Me")

	def test_bad_version_data_is_ignored(self):
		inc = {"name": "WINC-1", "started_at": T0}
		out = ops.build_activity(inc, [{"owner": "x", "creation": at(1), "data": "{nope"}], [])
		self.assertEqual([e["kind"] for e in out], ["opened"])


class TestPostmortem(unittest.TestCase):
	def test_snapshot_escapes_and_filters(self):
		events = [
			{"linked": True, "event_type": "error", "severity": "critical", "title": "<script>x</script>",
			 "timestamp": at(1), "count": 3, "site": "a.example.com"},
			{"linked": False, "event_type": "error", "severity": "warning", "title": "unrelated", "timestamp": at(2)},
			{"linked": False, "event_type": "deployment", "severity": "info", "title": "deploy", "timestamp": at(-5)},
		]
		activity = [
			{"kind": "status", "to": "Resolved", "timestamp": at(9), "user": "ops@example.com"},
			{"kind": "comment", "content": "secret", "timestamp": at(3), "user": "x"},
		]
		html = ops.timeline_snapshot_html(events, activity, user_label=lambda u: "Ops")
		self.assertNotIn("<script>", html)
		self.assertIn("&lt;script&gt;", html)
		self.assertNotIn("unrelated", html)
		self.assertNotIn("secret", html)
		self.assertIn("(x3)", html)
		self.assertLess(html.index("deploy"), html.index("&lt;script"))
		self.assertIn("Ops resolved", html)
		self.assertEqual(ops.timeline_snapshot_html([], []), "<p>No events.</p>")

	def test_snapshot_limit(self):
		events = [{"linked": True, "title": f"e{i}", "timestamp": at(i)} for i in range(5)]
		html = ops.timeline_snapshot_html(events, [], limit=2)
		self.assertEqual(html.count("<li>"), 2)
		self.assertIn("2 first entries", html)

	def test_defaults(self):
		inc = {"name": "WINC-1", "title": "502 <spike>", "summary": "http_502 = 12", "site": "a", "host": "h1",
		       "severity": "critical", "started_at": T0, "resolved_at": at(75), "event_count": 4,
		       "deployment_at": at(-3)}
		d = ops.postmortem_defaults(inc, "<ul></ul>")
		self.assertEqual(d["title"], "Postmortem: 502 <spike>")
		self.assertEqual(d["duration_minutes"], 75.0)
		self.assertIn("1 h 15 min", d["impact"])
		self.assertIn("a / h1", d["impact"])
		self.assertIn("2026-10-29 01:57:00", d["root_cause"])
		self.assertEqual(d["timeline_snapshot"], "<ul></ul>")
		self.assertIn("http_502 = 12", d["summary"])


if __name__ == "__main__":
	unittest.main()
