"""Tamper-evident journal (wolf/journal.py): capture, chain, verify.

- Capture (doc events, inside the transaction that made the change): an unsigned Wolf Journal
  Entry with the canonical content and its digest, for security events, incident decisions
  (status, owner, merge, NIS2 / GDPR fields), response actions, comments on incidents and
  Wolf Settings changes (which fields changed and who changed them, never their values).
- Chain (`chain_journal`, every minute, one at a time under a lock): signs the unsigned entries
  (`seq` 0) in creation order with seq, prev_mac and mac. Doing it in one job, not in each transaction,
  means two concurrent requests can never fork the chain. An entry waits a minute at most.
- Verify (`bench --site <site> wolf-verify-journal [--anchor seq:mac]`, or `verify_journal`):
  recomputes every digest and mac; see wolf/journal.py for what it proves and what it cannot.
- Retention (`purge_journal`, daily, off unless Wolf Settings → Journal Retention Days is set):
  removes the oldest signed entries after a signed purge record that says where it stopped,
  never the newest entry, never one the SIEM export has not sent yet.

The HMAC key is `wolf_journal_key` in the site config, created on first use. Losing it makes
the existing chain unverifiable, so back it up with the site config.
"""

import secrets

import frappe
from frappe.utils import add_days, cint, now_datetime

from frappe_wolf.wolf import journal
from frappe_wolf.wolf.spa import MANAGER_ROLES

DOCTYPE = "Wolf Journal Entry"
KEY_CONF = "wolf_journal_key"
BATCH = 2000
LOCK = "wolf_journal_chain"


def journal_key(create=True):
	key = frappe.conf.get(KEY_CONF)
	if not key and create:
		from frappe.installer import update_site_config

		key = secrets.token_hex(32)
		update_site_config(KEY_CONF, key)
		frappe.local.conf[KEY_CONF] = key
	return key


SAVEPOINT = "wolf_journal"


def capture(kind, ref_doctype, ref_name, content, summary):
	"""Record an unsigned entry in the current transaction. Never fails the caller.

	The insert runs inside a savepoint: on Postgres a failed statement aborts the whole
	transaction, so a journal error is rolled back to the savepoint instead of breaking the
	write that triggered it.
	"""
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return None
	try:
		text = journal.canonical({"kind": kind, "ref_doctype": ref_doctype, "ref_name": str(ref_name), **content})
	except (TypeError, ValueError):
		frappe.logger("frappe_wolf").exception("journal content is not serialisable")
		return None
	frappe.db.savepoint(SAVEPOINT)
	try:
		doc = frappe.get_doc({
			"doctype": DOCTYPE, "at": now_datetime(), "kind": kind, "ref_doctype": ref_doctype, "ref_name": str(ref_name)[:140],
			"user": frappe.session.user, "summary": str(summary or "")[:255], "content": text, "digest": journal.digest(text),
		})  # fmt: skip
		doc.flags.ignore_permissions = True
		doc.insert()
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.logger("frappe_wolf").exception("could not write a journal entry")
		return None
	frappe.db.release_savepoint(SAVEPOINT)
	return doc.name


# --- doc events (hooks.py) -------------------------------------------------------------------


def on_event(doc, method=None):
	if doc.event_type != "security":
		return
	content = {**journal.pick(doc.as_dict(), journal.EVENT_FIELDS), "payload_sha256": journal.digest(doc.payload or "")}
	capture("security_event", doc.doctype, doc.name, content, doc.title)


def on_incident(doc, method=None):
	before = doc.get_doc_before_save()
	fields = journal.changed(before.as_dict() if before else None, doc.as_dict(), journal.INCIDENT_FIELDS)
	if not fields and before:
		return
	content = {"changed": fields, "state": journal.pick(doc.as_dict(), journal.INCIDENT_FIELDS), "title": doc.title}
	what = "created" if not before else "changed: " + ", ".join(fields)
	capture("incident", doc.doctype, doc.name, content, f"{doc.name} {what}")


def incident_set(name, values, reason):
	"""For the engine's direct writes (auto-resolve, reopen), which fire no doc event."""
	capture("incident", "Wolf Incident", name, {"changed": sorted(values), "state": values, "by": "wolf"},
	        f"{name} {reason}")  # fmt: skip


def on_response_action(doc, method=None):
	content = journal.pick(doc.as_dict(), journal.RESPONSE_FIELDS)
	capture("response_action", doc.doctype, doc.name, content, f"{doc.action} {doc.target}: {doc.status}")


def on_comment(doc, method=None):
	if doc.get("reference_doctype") != "Wolf Incident" or doc.get("comment_type") != "Comment":
		return
	content = {"incident": doc.reference_name, "author": doc.comment_email or doc.owner,
	           "content_sha256": journal.digest(doc.content or "")}  # fmt: skip
	capture("incident_comment", doc.doctype, doc.name, content, f"comment on {doc.reference_name}")


def on_settings(doc, method=None):
	before = doc.get_doc_before_save()
	if not before:
		return
	skip = {"modified", "modified_by", "creation", "owner", "idx", "docstatus", "doctype", "name"}
	old, new = before.as_dict(), doc.as_dict()
	fields = sorted(k for k in set(old) | set(new) if k not in skip and not isinstance(new.get(k), list)
	                and (old.get(k) or None) != (new.get(k) or None))  # fmt: skip
	if fields:
		# names only: some values (decoy keys, tokens) must not be copied around
		capture("settings", doc.doctype, doc.name, {"changed": fields}, "Wolf Settings changed: " + ", ".join(fields)[:200])


# --- chaining and verification ---------------------------------------------------------------


def _last():
	row = frappe.db.sql(f"select seq, mac from `tab{DOCTYPE}` where seq > 0 order by seq desc limit 1", as_dict=True)
	return (row[0].seq, row[0].mac) if row else (0, journal.GENESIS)


def _lock():
	try:
		from frappe.utils.synchronization import filelock

		return filelock(LOCK, timeout=300)
	except ImportError:  # older Frappe: the scheduler runs one instance of the job anyway
		from contextlib import nullcontext

		return nullcontext()


def _pending():
	return frappe.db.sql(
		f"""select name, `at`, kind, ref_doctype, ref_name, digest from `tab{DOCTYPE}`
		where coalesce(seq, 0) = 0 order by creation asc, name asc limit {BATCH}""",
		as_dict=True,
	)


def chain_journal():
	"""Scheduler (every minute): sign the waiting entries, in creation order, under a lock."""
	if not frappe.db.exists(DOCTYPE, {"seq": 0}):
		return 0
	with _lock():
		signed = _sign_pending()
		frappe.db.commit()
	return signed


def _sign_pending():
	"""Sign the waiting entries. Call under the lock."""
	pending = _pending()  # read again inside the lock: another run may have signed them
	if not pending:
		return 0
	key = journal_key()
	last_seq, last_mac = _last()
	for entry, seq, mac in journal.chain(key, pending, last_seq, last_mac):
		frappe.db.sql(
			f"update `tab{DOCTYPE}` set seq = %s, prev_mac = %s, mac = %s where name = %s and coalesce(seq, 0) = 0",
			(seq, last_mac, mac, entry["name"]),
		)
		last_mac = mac
	return len(pending)


def _siem_exported():
	"""The last journal seq the SIEM export sent, when it sends the journal; else None."""
	settings = frappe.get_cached_doc("Wolf Settings")
	if not (cint(settings.get("siem_enabled")) and cint(settings.get("siem_journal"))):
		return None
	import json

	from frappe_wolf.api import siem

	# read from the table, not get_global: its cache is shared between processes, and a job that
	# reads it while the cursor's write is not committed yet puts the old value back
	raw = frappe.db.get_value("DefaultValue", {"parent": "__global", "defkey": siem.CURSOR_KEY}, "defvalue")
	try:
		return cint(json.loads(raw or "{}").get("journal_seq"))
	except ValueError:
		return 0


def purge_journal(now=None):
	"""Scheduler (daily): remove signed entries older than the retention, after a purge record.

	Returns the number of entries removed. The purge record is signed before anything is
	deleted, in the same lock as chain_journal, so the journal is never left without it.
	"""
	days = cint(frappe.get_cached_doc("Wolf Settings").get("journal_retention_days"))
	if days <= 0:
		return 0
	now = now or now_datetime()
	cutoff = add_days(now, -days)
	with _lock():
		_sign_pending()
		span = frappe.db.sql(f"select min(seq), max(seq) from `tab{DOCTYPE}` where seq > 0")[0]
		oldest, newest = span[0], span[1]
		after = frappe.db.sql(f"select min(seq) from `tab{DOCTYPE}` where seq > 0 and `at` >= %s", (cutoff,))[0][0]
		through = journal.purge_point(oldest, newest, after, _siem_exported())
		if through is None:
			frappe.db.commit()
			return 0
		through_mac = frappe.db.get_value(DOCTYPE, {"seq": through}, "mac")
		count = through - oldest + 1
		content = {"purged_from_seq": oldest, "through_seq": through, "through_mac": through_mac, "count": count,
		           "before": str(cutoff), "retention_days": days}  # fmt: skip
		if not capture(journal.PURGE_KIND, DOCTYPE, f"{oldest}-{through}", content,
		               f"{count} entries older than {days} days purged (1 to {through} gone)"):  # fmt: skip
			frappe.db.rollback()
			return 0
		_sign_pending()
		frappe.db.sql(f"delete from `tab{DOCTYPE}` where seq > 0 and seq <= %s", (through,))
		frappe.db.commit()
	return count


def _entries(batch=5000):
	offset = 0
	while True:
		rows = frappe.db.sql(
			f"""select seq, `at`, kind, ref_doctype, ref_name, content, digest, prev_mac, mac from `tab{DOCTYPE}`
			where seq > 0 order by seq asc limit {batch} offset {offset}""",
			as_dict=True,
		)
		if not rows:
			return
		yield from rows
		offset += batch


def verify_journal(anchor=None):
	key = journal_key(create=False)
	if not key:
		return {"ok": False, "checked": 0, "problem": {"seq": None, "why": f"no {KEY_CONF} in the site config"}}
	parsed = journal.parse_anchor(anchor) if anchor else None
	if anchor and not parsed:
		return {"ok": False, "checked": 0, "problem": {"seq": None, "why": "the anchor must be seq:mac"}}
	result = journal.verify(key, _entries(), parsed)
	result["pending"] = frappe.db.count(DOCTYPE, {"seq": 0})
	return result


def _last_purge():
	rows = frappe.get_all(DOCTYPE, filters={"kind": journal.PURGE_KIND, "seq": (">", 0)}, fields=["content", "at"],
	                      order_by="seq desc", limit=1)  # fmt: skip
	record = journal.purge_record(rows[0].content) if rows else None
	return {**record, "at": rows[0].at} if record else None


@frappe.whitelist()
def get_status():
	frappe.only_for(MANAGER_ROLES)
	last_seq, last_mac = _last()
	purge = _last_purge()
	return {
		"entries": frappe.db.count(DOCTYPE, {"seq": (">", 0)}),
		"last_seq": last_seq,
		"anchor": f"{last_seq}:{last_mac}" if last_seq else None,
		"pending": frappe.db.count(DOCTYPE, {"seq": 0}),
		"key": bool(journal_key(create=False)),
		"retention_days": cint(frappe.get_cached_doc("Wolf Settings").get("journal_retention_days")),
		"purged_through": purge["through_seq"] if purge else None,
		"purged_at": purge["at"] if purge else None,
	}


@frappe.whitelist(methods=["POST"])
def verify(anchor=None):
	frappe.only_for(MANAGER_ROLES)
	return verify_journal(anchor)
