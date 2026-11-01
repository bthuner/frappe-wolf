"""A short cache for the overview's two slow reads (#10): the inventory and the signals.

What is cached is read without any access condition and is the same for every user; each
request then keeps what its caller may see (wolf/scopes.py), so a cached value never carries
one user's rights to another. Rules, incidents and anything else that turns a value into a
state are read per request, not cached.

A cached value is at most `wolf_read_cache_seconds` old (site config, default 30 s: an agent's
push interval); 0 turns the cache off. The reads say when their data was read (`as_of`).
"""

import frappe
from frappe.utils import cint, now_datetime

DEFAULT_SECONDS = 30
_PREFIX = "wolf_read_cache"


def seconds():
	value = frappe.conf.get("wolf_read_cache_seconds")
	return DEFAULT_SECONDS if value is None else max(cint(value), 0)


def get(name, key, compute):
	"""(value, as_of): `compute()`'s value, from the cache while it is younger than seconds()."""
	ttl = seconds()
	if not ttl:
		return compute(), now_datetime()
	cache_key = f"{_PREFIX}|{name}|{key}"
	# always from Redis, never from frappe.local.cache: that one lives as long as the process
	# in a worker or a script, not as long as the TTL
	hit = frappe.cache.get_value(cache_key, expires=True, use_local_cache=False)
	if hit is not None:
		return hit["value"], hit["as_of"]
	as_of = now_datetime()
	value = compute()
	frappe.cache.set_value(cache_key, {"value": value, "as_of": as_of}, expires_in_sec=ttl)
	return value, as_of


def clear():
	"""Drop every cached read of this site (tests, after changing what they read)."""
	frappe.cache.delete_keys(f"{_PREFIX}|")
