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

	def test_purged_head_starts_from_its_prev_mac(self):
		self.assertTrue(journal.verify(KEY, build()[2:])["ok"])

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


if __name__ == "__main__":
	unittest.main()
