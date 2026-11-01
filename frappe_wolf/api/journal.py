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

The HMAC key is `wolf_journal_key` in the site config, created on first use. Losing it makes
the existing chain unverifiable, so back it up with the site config.
"""

import secrets

import frappe
from frappe.utils import now_datetime

from frappe_wolf.wolf import journal

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


def capture(kind, ref_doctype, ref_name, content, summary):
	"""Record an unsigned entry in the current transaction. Never fails the caller."""
	try:
		if frappe.flags.in_install or frappe.flags.in_migrate:
			return None
		text = journal.canonical({"kind": kind, "ref_doctype": ref_doctype, "ref_name": ref_name, **content})
		doc = frappe.get_doc({
			"doctype": DOCTYPE, "at": now_datetime(), "kind": kind, "ref_doctype": ref_doctype, "ref_name": ref_name,
			"user": frappe.session.user, "summary": str(summary or "")[:255], "content": text, "digest": journal.digest(text),
		})  # fmt: skip
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name
	except Exception:
		frappe.logger("frappe_wolf").exception("could not write a journal entry")
		return None


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
		frappe.db.commit()
	return len(pending)


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


@frappe.whitelist()
def get_status():
	frappe.only_for(("System Manager", "Wolf Manager"))
	last_seq, last_mac = _last()
	return {
		"entries": last_seq,
		"anchor": f"{last_seq}:{last_mac}" if last_seq else None,
		"pending": frappe.db.count(DOCTYPE, {"seq": 0}),
		"key": bool(journal_key(create=False)),
	}


@frappe.whitelist(methods=["POST"])
def verify(anchor=None):
	frappe.only_for(("System Manager", "Wolf Manager"))
	return verify_journal(anchor)
