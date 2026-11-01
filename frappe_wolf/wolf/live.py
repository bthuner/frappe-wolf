"""Live updates for the Wolf UI: authorized invalidations over Frappe's socket.io.

The server never pushes telemetry. It tells subscribed browsers *that* something changed, in
rooms Frappe guards with the reader's own permissions, and the browser fetches again through
the APIs, which apply the permissions once more:

- `doc:Wolf Incident/<name>`, joined with `doc_subscribe`: Frappe checks read permission on that
  incident (so Wolf User Scope rules apply). Message `{"kind": "incident", "incident": name}`.
- `doctype:Wolf Incident` and `doctype:Wolf Event`, joined with `doctype_subscribe` (read
  permission on the DocType). Message `{"kind": "incidents"}` or `{"kind": "events"}`: no
  names, so a restricted reader only learns that something changed somewhere.

Each room gets at most one message per request or job, sent after commit. List messages are
also throttled to one per `THROTTLE_SECONDS` per kind across requests (ingest can attach
hundreds of events a minute); one held back by the throttle marks its kind dirty, and
`flush()` (every minute) sends it, so a change is never lost, only delayed.
"""

import frappe

EVENT = "wolf_invalidate"
INCIDENT_DOCTYPE = "Wolf Incident"
LIST_DOCTYPES = {"incidents": "Wolf Incident", "events": "Wolf Event"}
THROTTLE_SECONDS = 5
_THROTTLE_KEY = "wolf_live_throttle:"
_DIRTY_KEY = "wolf_live_dirty:"


def _helper(name):
	import frappe.realtime

	return getattr(frappe.realtime, name, None)


def doc_room(doctype, name):
	helper = _helper("get_doc_room")
	return helper(doctype, name) if helper else f"doc:{doctype}/{name}"


def doctype_room(doctype):
	helper = _helper("get_doctype_room")
	return helper(doctype) if helper else f"doctype:{doctype}"


def _publish(message, room):
	sent = frappe.flags.wolf_live_sent
	if sent is None:
		sent = frappe.flags.wolf_live_sent = set()
	if room in sent:
		return False
	sent.add(room)
	try:
		frappe.publish_realtime(EVENT, message, room=room, after_commit=True)
	except Exception:
		# live updates are a convenience: never fail the write that triggered them
		frappe.log_error(title="[Wolf] Could not publish a live update")
	return True


def incident_changed(name):
	"""An incident's header, timeline, activity or deliveries changed."""
	if not name:
		return
	_publish({"kind": "incident", "incident": name}, doc_room(INCIDENT_DOCTYPE, name))
	list_changed("incidents")


def list_changed(kind):
	"""Something in a list (incidents, events) changed: throttled, never dropped."""
	doctype = LIST_DOCTYPES[kind]
	room = doctype_room(doctype)
	if room in (frappe.flags.wolf_live_sent or ()):
		return
	cache = frappe.cache()
	if cache.get_value(_THROTTLE_KEY + kind):
		cache.set_value(_DIRTY_KEY + kind, 1)
		return
	cache.set_value(_THROTTLE_KEY + kind, 1, expires_in_sec=THROTTLE_SECONDS)
	_publish({"kind": kind}, room)


def flush():
	"""Scheduler, every minute: send the list messages the throttle held back."""
	cache = frappe.cache()
	for kind, doctype in LIST_DOCTYPES.items():
		if cache.get_value(_DIRTY_KEY + kind):
			cache.delete_value(_DIRTY_KEY + kind)
			_publish({"kind": kind}, doctype_room(doctype))


# doc_events (hooks.py)


def on_incident(doc, method=None):
	incident_changed(doc.name)


def on_event(doc, method=None):
	list_changed("events")
	if doc.get("incident"):
		incident_changed(doc.incident)


def on_comment(doc, method=None):
	if doc.get("reference_doctype") == INCIDENT_DOCTYPE:
		incident_changed(doc.reference_name)


def on_delivery(doc, method=None):
	incident_changed(doc.get("incident"))
