import unittest
from datetime import datetime, timedelta

from frappe_wolf.wolf import timeline

TZ = "Europe/Paris"
T0 = datetime(2026, 10, 30, 1, 47)


def event(name, at, incident=None, **kw):
	return {"name": name, "timestamp": at, "last_seen": kw.pop("last_seen", at), "incident": incident,
			"title": kw.pop("title", name), "count": kw.pop("count", 1), "payload": kw.pop("payload", {}), **kw}  # fmt: skip


class TestCursor(unittest.TestCase):
	def test_round_trip(self):
		key = (T0, 1, "activity:comment:abc")
		self.assertEqual(timeline.decode_cursor(timeline.encode_cursor(key)), key)
		self.assertIsNone(timeline.decode_cursor(None))

	def test_rejects_garbage(self):
		for bad in ("nope", "e30", timeline.encode_cursor((T0, 0, "x"))[:-3] + "!!"):
			with self.assertRaises(ValueError, msg=bad):
				timeline.decode_cursor(bad)

	def test_families(self):
		self.assertEqual(timeline.families(None), set(timeline.FAMILIES))
		self.assertEqual(timeline.families("evidence, activity"), {"evidence", "activity"})
		with self.assertRaises(ValueError):
			timeline.families("evidence,gossip")


class TestRows(unittest.TestCase):
	def test_linked_context_and_deployment(self):
		_, linked = timeline.event_row(event("E1", T0, "WINC-1", severity="critical"), "WINC-1", TZ)
		_, context = timeline.event_row(event("E2", T0, None), "WINC-1", TZ)
		_, deploy = timeline.event_row(event("D1", T0, None, event_type="deployment"), "WINC-1", TZ, "D1")
		self.assertEqual((linked["family"], linked["relation"]), ("evidence", "linked"))
		self.assertEqual((context["family"], context["relation"]), ("context", "context"))
		self.assertEqual((deploy["kind"], deploy["relation"]), ("deployment", "preceding_change"))
		self.assertEqual(linked["at"], "2026-10-30T00:47:00Z")

	def test_deduplicated_event_keeps_first_last_and_count(self):
		_, row = timeline.event_row(event("E1", T0, "W", count=14, last_seen=T0 + timedelta(minutes=5)), "W", TZ)
		self.assertEqual((row["count"], row["first_seen"], row["last_seen"]), (14, "2026-10-30T00:47:00Z", "2026-10-30T00:52:00Z"))

	def test_deleted_deployment(self):
		_, row = timeline.deployment_unavailable_row("WINC-1", T0, TZ)
		self.assertTrue(row["unavailable"])
		self.assertEqual(row["id"], "deployment:WINC-1")

	def test_activity_ids_are_stable(self):
		comment = {"kind": "comment", "name": "c1", "timestamp": T0, "plain": "on it"}
		change = {"kind": "status", "field": "status", "to": "Acknowledged", "version": "v9", "timestamp": T0}
		auto = {"kind": "status", "field": "status", "to": "Resolved", "auto": True, "timestamp": T0}
		opened = {"kind": "opened", "timestamp": T0}
		ids = [timeline.activity_row(e, "WINC-1", TZ)[1]["id"] for e in (comment, change, auto, opened)]
		self.assertEqual(ids, ["activity:comment:c1", "activity:version:v9:status",
							   "activity:status:Resolved:2026-10-30T01:47:00", "activity:opened:WINC-1"])  # fmt: skip
		# the same record twice gives the same id (refresh, reconnect)
		self.assertEqual(timeline.activity_row(auto, "WINC-1", TZ)[1]["id"], ids[2])
		self.assertEqual(timeline.activity_row(comment, "WINC-1", TZ)[1]["text"], "on it")
		self.assertIsNone(timeline.activity_row(change, "WINC-1", TZ)[1]["text"])

	def test_delivery_says_accepted_not_read(self):
		_, ok = timeline.delivery_row({"name": "d1", "at": T0, "channel_type": "slack", "action": "opened", "ok": 1,
									   "http_status": 200}, TZ)  # fmt: skip
		_, bad = timeline.delivery_row({"name": "d2", "at": T0, "kind": "email", "ok": 0, "error": "SMTP down"}, TZ)
		self.assertEqual((ok["result"], ok["summary"]), ("accepted", "slack opened accepted (200)"))
		self.assertEqual((bad["result"], bad["error"]), ("failed", "SMTP down"))


class TestPaging(unittest.TestCase):
	def all_rows(self):
		# equal timestamps across families: the order must still be total and stable
		events = [timeline.event_row(event(f"E{i}", T0 + timedelta(minutes=i // 3), "W"), "W", TZ) for i in range(9)]
		acts = [timeline.activity_row({"kind": "comment", "name": f"c{i}", "timestamp": T0 + timedelta(minutes=i)}, "W", TZ)
				for i in range(3)]  # fmt: skip
		dels = [timeline.delivery_row({"name": f"d{i}", "at": T0 + timedelta(minutes=1), "ok": 1}, TZ) for i in range(2)]
		return events, acts + dels

	def fetch(self, events, cursor_key, limit):
		"""What the SQL keyset read returns: events after the cursor, limit + 1 of them."""
		after = [kr for kr in sorted(events, key=lambda kr: kr[0]) if timeline.after(kr[0], cursor_key)]
		return after[: limit + 1], len(after) > limit

	def test_pages_have_no_gaps_or_duplicates(self):
		events, others = self.all_rows()
		expected = [r["id"] for _, r in sorted(events + others, key=lambda kr: kr[0])]
		for limit in (1, 2, 3, 5, 14, 50):
			seen, cursor = [], None
			for _ in range(50):
				fetched, more = self.fetch(events, timeline.decode_cursor(cursor), limit)
				rows, paging = timeline.page(fetched + others, limit, timeline.decode_cursor(cursor), more)
				seen += [r["id"] for r in rows]
				if not paging["has_more"]:
					break
				cursor = paging["next_cursor"]
			self.assertEqual(seen, expected, f"limit {limit}")

	def test_ties_keep_events_before_activity_before_deliveries(self):
		events, others = self.all_rows()
		rows, _ = timeline.page(events + others, 100)
		at_one_minute = [r["family"] for r in rows if r["at"] == "2026-10-30T00:48:00Z"]
		self.assertEqual(at_one_minute, ["evidence"] * 3 + ["activity", "delivery", "delivery"])


if __name__ == "__main__":
	unittest.main()


class TestKeyset(unittest.TestCase):
	def test_no_cursor(self):
		self.assertEqual(timeline.keyset_sql(None, "event", "`timestamp`"), ("", {}))

	def test_same_family_continues_by_name(self):
		sql, params = timeline.keyset_sql((T0, 0, "event:E5"), "event", "`timestamp`")
		self.assertIn("name > %(c_name)s", sql)
		self.assertEqual(params, {"c_at": T0, "c_name": "E5"})

	def test_earlier_rank_or_earlier_id_keeps_the_same_time(self):
		self.assertEqual(timeline.keyset_sql((T0, 0, "event:E5"), "delivery", "`at`")[0], "and `at` >= %(c_at)s")
		self.assertEqual(timeline.keyset_sql((T0, 0, "deployment:W1"), "event", "`timestamp`")[0], "and `timestamp` >= %(c_at)s")

	def test_later_rank_moves_on(self):
		self.assertEqual(timeline.keyset_sql((T0, 1, "activity:x"), "event", "`timestamp`")[0], "and `timestamp` > %(c_at)s")
		self.assertEqual(timeline.keyset_sql((T0, 2, "delivery:d"), "event", "`timestamp`")[0], "and `timestamp` > %(c_at)s")
