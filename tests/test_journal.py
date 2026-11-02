import unittest

from frappe_wolf.wolf import journal

KEY = "k" * 64


def build(n=4):
	pending = []
	for i in range(n):
		content = journal.canonical({"kind": "security_event", "i": i, "title": f"event {i}"})
		pending.append({"at": f"2026-11-01 10:00:0{i}", "kind": "security_event", "ref_doctype": "Wolf Event",
		                "ref_name": f"E{i}", "content": content, "digest": journal.digest(content)})  # fmt: skip
	entries, prev = [], journal.GENESIS
	for entry, seq, mac in journal.chain(KEY, pending):
		entries.append({**entry, "seq": seq, "prev_mac": prev, "mac": mac})
		prev = mac
	return entries


class TestJournal(unittest.TestCase):
	def test_valid_chain(self):
		entries = build()
		r = journal.verify(KEY, entries)
		self.assertTrue(r["ok"], r)
		self.assertEqual((r["checked"], r["first_seq"], r["last_seq"], r["last_mac"]), (4, 1, 4, entries[-1]["mac"]))

	def test_continues_an_existing_chain(self):
		entries = build(2)
		more = build(1)[0]
		((_, seq, mac),) = journal.chain(KEY, [more], 2, entries[-1]["mac"])
		entries.append({**more, "seq": seq, "prev_mac": entries[-1]["mac"], "mac": mac})
		self.assertTrue(journal.verify(KEY, entries)["ok"])

	def test_edited_content(self):
		entries = build()
		entries[1]["content"] = entries[1]["content"].replace("event 1", "nothing to see")
		r = journal.verify(KEY, entries)
		self.assertEqual((r["ok"], r["problem"]["seq"]), (False, 2))
		self.assertIn("content was edited", r["problem"]["why"])

	def test_edited_content_and_digest(self):
		entries = build()
		entries[1]["content"] = "{}"
		entries[1]["digest"] = journal.digest("{}")
		self.assertIn("mac does not match", journal.verify(KEY, entries)["problem"]["why"])

	def test_removed_entry(self):
		entries = build()
		del entries[1]
		r = journal.verify(KEY, entries)
		self.assertEqual(r["problem"]["seq"], 3)
		self.assertIn("entry 2 is missing", r["problem"]["why"])

	def test_renumbered_after_removal(self):
		entries = build()
		del entries[1]
		for i, e in enumerate(entries, 1):
			e["seq"] = i
		self.assertIn("prev_mac does not match", journal.verify(KEY, entries)["problem"]["why"])

	def test_wrong_key(self):
		self.assertIn("key", journal.verify("other", build())["problem"]["why"])

	def test_oldest_entries_removed_without_a_purge_record(self):
		r = journal.verify(KEY, build()[2:])
		self.assertEqual(r["problem"], {"seq": 3, "why": "entries 1 to 2 were removed without a purge record"})


	def test_first_entry_must_start_from_genesis(self):
		entries = build()
		entries[0]["prev_mac"] = "f" * 64
		self.assertIn("genesis", journal.verify(KEY, entries)["problem"]["why"])

	def test_anchor(self):
		entries = build()
		self.assertTrue(journal.verify(KEY, entries, anchor=(3, entries[2]["mac"]))["ok"])
		r = journal.verify(KEY, entries[:2], anchor=(3, entries[2]["mac"]))  # newest entries deleted
		self.assertIn("not in the journal any more", r["problem"]["why"])
		self.assertIn("another mac", journal.verify(KEY, entries, anchor=(3, "a" * 64))["problem"]["why"])

	def test_parse_anchor(self):
		self.assertEqual(journal.parse_anchor(f"12:{'A' * 64}"), (12, "a" * 64))
		self.assertIsNone(journal.parse_anchor("12:short"))
		self.assertIsNone(journal.parse_anchor("x:" + "a" * 64))

	def test_changed(self):
		self.assertEqual(journal.changed(None, {"status": "Open", "severity": "critical", "merged_into": None},
		                                 ("status", "severity", "merged_into")), ["status", "severity"])  # fmt: skip
		self.assertEqual(journal.changed({"status": "Open"}, {"status": "Resolved"}, ("status", "assigned_to")), ["status"])

	def test_canonical_is_stable(self):
		self.assertEqual(journal.canonical({"b": 1, "a": "é"}), '{"a":"é","b":1}')


def with_purge(n=6, through=2):
	"""A journal of n entries plus a purge record (through `through`), its head purged."""
	entries = build(n)
	cut = entries[through - 1]
	content = journal.canonical({"kind": journal.PURGE_KIND, "through_seq": through, "through_mac": cut["mac"], "count": through})
	record = {"at": "2026-11-01 11:00:00", "kind": journal.PURGE_KIND, "ref_doctype": "Wolf Journal Entry",
	          "ref_name": f"1-{through}", "content": content, "digest": journal.digest(content)}  # fmt: skip
	[(entry, seq, mac)] = journal.chain(KEY, [record], n, entries[-1]["mac"])
	entries.append({**entry, "seq": seq, "prev_mac": entries[-1]["mac"], "mac": mac})
	return entries[through:], cut


class TestRetention(unittest.TestCase):
	def test_purged_head_with_its_record(self):
		kept, _ = with_purge()
		r = journal.verify(KEY, kept)
		self.assertTrue(r["ok"], r)
		self.assertEqual((r["first_seq"], r["purged_through"], r["checked"]), (3, 2, 5))

	def test_more_removed_after_the_purge(self):
		kept, _ = with_purge()
		r = journal.verify(KEY, kept[1:])  # entry 3 deleted too: looks like a purge, is not one
		self.assertIn("should start at entry 3", r["problem"]["why"])

	def test_purge_record_with_another_cut(self):
		kept, _ = with_purge(through=2)
		kept[0]["prev_mac"] = "e" * 64  # the first kept entry does not chain from the recorded cut
		self.assertFalse(journal.verify(KEY, kept)["ok"])

	def test_anchor_in_the_purged_part(self):
		kept, cut = with_purge(through=2)
		self.assertTrue(journal.verify(KEY, kept, anchor=(2, cut["mac"]))["ok"])  # the record kept its mac
		r = journal.verify(KEY, kept, anchor=(1, "a" * 64))
		self.assertIn("was purged by the retention", r["problem"]["why"])

	def test_purge_point(self):
		self.assertEqual(journal.purge_point(1, 10, 6), 5)
		self.assertEqual(journal.purge_point(1, 10, None), 9)  # all old: the newest is kept
		self.assertEqual(journal.purge_point(1, 10, 6, exported=3), 3)  # not before the SIEM has it
		self.assertIsNone(journal.purge_point(4, 10, 4))  # nothing older than the cut
		self.assertIsNone(journal.purge_point(None, None, None))  # empty journal
		self.assertIsNone(journal.purge_point(5, 5, None))  # one entry: kept


if __name__ == "__main__":
	unittest.main()
