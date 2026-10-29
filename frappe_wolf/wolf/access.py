"""Per-site / bench / host access for Wolf Users, enforced in lists, reports, forms and Wolf APIs.

Grants are Wolf User Scope rows (user, scope_type, value). Site, bench and host are Data
fields, so Frappe User Permissions (which follow Link fields) cannot express this; the
`permission_query_conditions` and `has_permission` hooks below do, and api/ui.py applies the
same rules to its raw SQL. System Manager, Wolf Manager and Administrator are never
restricted, nor is a user without any Wolf User Scope row.
"""

import frappe

from frappe_wolf.wolf import scopes

UNRESTRICTED_ROLES = ("System Manager", "Wolf Manager")
_CACHE = "wolf_scope_grants"


def clear_cache():
	if hasattr(frappe.local, _CACHE):
		delattr(frappe.local, _CACHE)


def grants(user=None):
	"""None (sees everything) or {scope_type: values} for `user`; cached for the request."""
	user = user or frappe.session.user
	if user == "Administrator":
		return None
	cache = getattr(frappe.local, _CACHE, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, _CACHE, cache)
	if user not in cache:
		if set(frappe.get_roles(user)) & set(UNRESTRICTED_ROLES):
			cache[user] = None
		else:
			rows = frappe.get_all("Wolf User Scope", filters={"user": user}, fields=["scope_type", "value"])
			cache[user] = scopes.build_grants(rows) if rows else None
	return cache[user]


def query_condition(doctype, user=None):
	return scopes.sql_literal_condition(
		grants(user), scopes.COLUMNS[doctype], f"`tab{doctype}`", lambda v: frappe.db.escape(v, percent=False)
	)


def sql_condition(doctype, user=None, table=None):
	"""(" and (...)", params) to append to a raw SQL WHERE on `doctype`."""
	return scopes.sql_param_condition(grants(user), scopes.COLUMNS[doctype], table)


def visible(doctype, row, user=None):
	return scopes.row_visible(row, grants(user), scopes.COLUMNS[doctype])


# hooks.py: permission_query_conditions (user, doctype) -> SQL, has_permission (doc, ptype, user) -> bool


def event_query(user=None, doctype=None, **kwargs):
	return query_condition("Wolf Event", user) or ""


def metric_query(user=None, doctype=None, **kwargs):
	return query_condition("Wolf Metric Sample", user) or ""


def incident_query(user=None, doctype=None, **kwargs):
	return query_condition("Wolf Incident", user) or ""


def silence_query(user=None, doctype=None, **kwargs):
	return query_condition("Wolf Silence", user) or ""


def agent_query(user=None, doctype=None, **kwargs):
	return query_condition("Wolf Agent", user) or ""


def has_permission(doc, ptype=None, user=None, **kwargs):
	"""Frappe resolves `doc` to a Document before calling controller permission hooks."""
	if doc.doctype not in scopes.COLUMNS:
		return True
	return visible(doc.doctype, doc, user)
