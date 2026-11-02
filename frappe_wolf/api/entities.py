"""Entities of security events (wolf/entities.py): aliases, the index written at ingest, search.

- `aliases()`: Wolf Entity Alias rows, cached until one changes.
- `learn(...)`: an Auto alias, never over a Manual one. Wolf learns them from:
  - an agent's registration: its address, and its benches' site domains;
  - a push from a new address;
  - a Wazuh alert about another Wazuh agent: that agent's address.
- `index(event_name, data)`: called by events.insert_event for security events. The entities
  go in the payload (`entities`) and in Wolf Event Entity, for the `entity=` filter of the event
  list.
- `find(entity)`: the events of every source that name an entity (`ip:203.0.113.5`,
  `host:erp-01`, `site:erp.example.com`, `user:jane@example.com`).
"""

import frappe
from frappe.utils import now_datetime

from frappe_wolf.api.ui import READ_ROLES
from frappe_wolf.wolf import entities

CACHE_KEY = "wolf_entity_aliases"


def aliases():
	rows = frappe.cache.get_value(CACHE_KEY)
	if rows is None:
		rows = [tuple(r) for r in frappe.db.sql("select kind, alias, canonical from `tabWolf Entity Alias`")]
		frappe.cache.set_value(CACHE_KEY, rows, expires_in_sec=3600)
	return entities.Aliases(rows)


def learn(kind, alias, canonical, seen_from=None):
	"""Record an Auto alias; a Manual row for the same name wins and is left alone."""
	key = entities.normal_ip(alias) or entities.normal_name(alias)
	canonical = entities.normal_name(canonical)
	if not key or not canonical or key == canonical:
		return
	row = frappe.db.get_value("Wolf Entity Alias", {"kind": kind, "alias": key}, ["name", "source", "canonical"], as_dict=True)
	if row and (row.source == "Manual" or row.canonical == canonical):
		return
	if row:
		frappe.db.set_value("Wolf Entity Alias", row.name, {"canonical": canonical, "seen_from": (seen_from or "")[:140]})
	else:
		doc = frappe.get_doc({"doctype": "Wolf Entity Alias", "kind": kind, "alias": key, "canonical": canonical,
		                      "source": "Auto", "seen_from": (seen_from or "")[:140]})  # fmt: skip
		doc.flags.ignore_permissions = True
		try:
			doc.insert()
		except frappe.DuplicateEntryError:
			return
	frappe.cache.delete_value(CACHE_KEY)


def learn_from_registration(hostname, address, benches):
	"""An agent's address and its benches' site domains (agent/wolf_agent/shipper.register)."""
	if address:
		learn("host", address, hostname, f"agent {hostname}")
	for bench in benches or []:
		if not isinstance(bench, dict):
			continue
		for domain, site in (bench.get("site_domains") or {}).items():
			learn("site", domain, site, f"agent {hostname}, bench {bench.get('name')}")


def learn_from_event(data, payload):
	"""A Wazuh alert about another Wazuh agent tells that agent's address."""
	agent = payload.get("wazuh_agent") if isinstance(payload, dict) else None
	if isinstance(agent, dict) and agent.get("ip") and agent.get("name") and agent.get("id") != "000":
		learn("host", agent["ip"], agent["name"], "Wazuh agent")


def prepare(data, payload):
	"""The event's entities, and the payload carrying them (`entities` key)."""
	if data.get("event_type") != "security" or not isinstance(payload, dict):
		return None, payload
	found = entities.extract(data, payload, aliases())
	return found, ({**payload, "entities": found} if found else payload)


def index(event_name, found, at=None):
	if not found:
		return
	now = now_datetime()
	rows = [
		(frappe.generate_hash(length=12), now, now, "Administrator", "Administrator", 0, event_name, kind, value, at or now)
		for kind, values in found.items()
		for value in values
	]
	frappe.db.bulk_insert("Wolf Event Entity", ["name", "creation", "modified", "owner", "modified_by", "docstatus",
	                                            "event", "kind", "value", "at"], rows)  # fmt: skip


def filter_clause(entity):
	"""(SQL, params) restricting `tabWolf Event` to the events naming `entity` (`kind:value`)."""
	try:
		kind, value = entities.parse_filter(entity)
	except ValueError as e:
		frappe.throw(str(e), frappe.ValidationError)
	return (
		"name in (select event from `tabWolf Event Entity` where kind = %(entity_kind)s and value = %(entity_value)s)",
		{"entity_kind": kind, "entity_value": value},
	)


def purge(before):
	"""Daily: entity rows whose event is gone (the event purge keeps open incidents' events)."""
	frappe.db.sql(
		"""delete from `tabWolf Event Entity` where at < %(before)s
		and not exists (select 1 from `tabWolf Event` e where e.name = `tabWolf Event Entity`.event)""",
		{"before": before},
	)


@frappe.whitelist()
def find(entity, range=None, since=None, until=None, cursor=None, limit=50):
	"""Every source's events naming an entity: the event list with `entity=` set."""
	frappe.only_for(READ_ROLES)
	from frappe_wolf.api import explore

	return explore.list_events(range=range, since=since, until=until, entity=entity, cursor=cursor, limit=limit)
