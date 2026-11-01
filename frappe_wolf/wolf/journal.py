"""Tamper-evident journal: an HMAC chain over security records (no frappe imports).

Each entry keeps the canonical JSON `content` of what happened (a security event, an incident
status change, a response action, a settings change), its SHA-256 `digest`, and

    mac = HMAC-SHA256(key, "seq|at|kind|ref_doctype|ref_name|digest|prev_mac")

where `prev_mac` is the previous entry's mac (`GENESIS` for the first). `verify` recomputes
everything: an edited content changes its digest, an edited, removed or reordered entry breaks
the chain after it. Deleting the newest entries leaves a valid shorter chain, which is why an
anchor (the newest `seq` and `mac`) is worth copying somewhere Wolf cannot write: the SIEM
export sends every entry out, and `verify(anchor=...)` checks a copied anchor is still there.

The key lives in the site config (`wolf_journal_key`), not in the database: someone who can
only write the database cannot forge a chain.
"""

import hashlib
import hmac
import json

GENESIS = "0" * 64


def canonical(content):
	"""Stable JSON: sorted keys, no whitespace, non-JSON values as strings."""
	return json.dumps(content, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def digest(text):
	return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mac(key, seq, at, kind, ref_doctype, ref_name, content_digest, prev_mac):
	message = "|".join(str(x if x is not None else "") for x in (seq, at, kind, ref_doctype, ref_name, content_digest, prev_mac))
	return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def chain(key, pending, last_seq=0, last_mac=GENESIS):
	"""Seq and mac for `pending` entries (dicts with at, kind, ref_doctype, ref_name, digest),
	in order, after (last_seq, last_mac). Returns [(entry, seq, mac)]."""
	out = []
	seq, prev = last_seq, last_mac or GENESIS
	for entry in pending:
		seq += 1
		m = mac(key, seq, entry["at"], entry["kind"], entry.get("ref_doctype"), entry.get("ref_name"), entry["digest"], prev)
		out.append((entry, seq, m))
		prev = m
	return out


def verify(key, entries, anchor=None):
	"""Check chained entries (ordered by seq). Returns {ok, checked, first_seq, last_seq, last_mac, problem}.

	`entries` must start at the first entry kept; a journal whose oldest entries were purged
	starts from that entry's own `prev_mac`. `anchor` is a (seq, mac) copied earlier.
	"""
	result = {"ok": True, "checked": 0, "first_seq": None, "last_seq": None, "last_mac": None, "problem": None}

	def fail(seq, why):
		result.update(ok=False, problem={"seq": seq, "why": why})
		return result

	prev_seq, prev_mac = None, None
	seen = {}
	for e in entries:
		seq = e["seq"]
		if prev_seq is None:
			result["first_seq"] = seq
			prev_mac = e["prev_mac"]
			if seq == 1 and prev_mac != GENESIS:
				return fail(seq, "the first entry does not start from the genesis value")
		elif seq != prev_seq + 1:
			return fail(seq, f"entry {prev_seq + 1} is missing (sequence jumps from {prev_seq} to {seq})")
		if e["prev_mac"] != prev_mac:
			return fail(seq, "prev_mac does not match the previous entry: an entry was removed, edited or reordered")
		if digest(e["content"]) != e["digest"]:
			return fail(seq, "content does not match its digest: the content was edited")
		expected = mac(key, seq, e["at"], e["kind"], e.get("ref_doctype"), e.get("ref_name"), e["digest"], prev_mac)
		if not hmac.compare_digest(expected, e["mac"] or ""):
			return fail(seq, "mac does not match: the entry was edited or the key is not the one that signed it")
		seen[seq] = e["mac"]
		prev_seq, prev_mac = seq, e["mac"]
		result["checked"] += 1
	result["last_seq"], result["last_mac"] = prev_seq, prev_mac
	if anchor:
		a_seq, a_mac = int(anchor[0]), anchor[1]
		if a_seq not in seen:
			return fail(a_seq, f"anchored entry {a_seq} is not in the journal any more (newest is {prev_seq})")
		if not hmac.compare_digest(seen[a_seq], a_mac):
			return fail(a_seq, "anchored entry has another mac than the one copied")
	return result


def parse_anchor(text):
	"""`seq:mac` → (seq, mac), or None."""
	seq, _, value = str(text or "").strip().partition(":")
	if not seq.isdigit() or len(value) != 64:
		return None
	return int(seq), value.lower()


# what to keep from each record (no secrets, payloads by digest)
EVENT_FIELDS = ("event_type", "category", "severity", "title", "source", "timestamp", "host", "bench", "site", "agent")
INCIDENT_FIELDS = ("status", "severity", "assigned_to", "acknowledged_by", "resolved_by", "merged_into", "nis2_significant",
                   "nis2_aware_at", "nis2_early_warning_at", "nis2_notification_at", "nis2_final_report_at",
                   "gdpr_breach", "gdpr_aware_at", "gdpr_notified_at", "regulatory_reference")  # fmt: skip
RESPONSE_FIELDS = ("action", "target", "status", "incident", "agent", "host", "duration_hours", "approved_by",
                   "approved_at", "applied_at", "expires_at", "result")  # fmt: skip


def pick(record, fields):
	return {f: record.get(f) for f in fields if record.get(f) not in (None, "")}


def changed(before, after, fields):
	"""Field names whose value differs (all of `fields` set on a new record)."""
	if not before:
		return [f for f in fields if after.get(f) not in (None, "", 0)]
	return [f for f in fields if (before.get(f) or None) != (after.get(f) or None)]
