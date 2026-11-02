"""A short cache for the overview's two slow reads (#10): the inventory and the signals.

What is cached is read without any access condition and is the same for every user; each
request then keeps what its caller may see (wolf/scopes.py), so a cached value never carries
one user's rights to another. Rules, incidents and anything else that turns a value into a
state are read per request, not cached.

A value is fresh for `wolf_read_cache_seconds` (site config, default 30 s: an agent's push
interval); 0 turns the cache off. Past that and up to STALE_FACTOR times longer, it is still
served at once, and a background job reads it again (stale-while-revalidate): a page does not
wait for a read the cache can do without it. Past that, the request reads.

Between visits, the scheduler reads again the values asked for in the last ACTIVE_SECONDS
(refresh_active, every minute). With `scheduler_tick_interval` at 60 s, an overview opened after
a few idle minutes finds a value under STALE_FACTOR x 30 s old; at Frappe's default 4 min tick,
those visits read, as without it.

The reads say when their data was read (`as_of`).
"""

import frappe
from frappe.utils import cint, now_datetime

DEFAULT_SECONDS = 30
STALE_FACTOR = 4  # a value is served up to 4 x its fresh time (2 min by default), while re-read
ACTIVE_SECONDS = 600  # the scheduler keeps a value warm for 10 min after it was last asked for
# values kept warm at most, per site: each costs a read a minute (0.6 s at 100 hosts), and an
# overview, a few dashboards and the inventory are what is opened between two minutes
MAX_ACTIVE = 10
_PREFIX = "wolf_read_cache"
_ACTIVE = "wolf_read_cache_active"

# name -> function(key) that reads the value; registered by the modules that own the reads, so
# a background job can read a value again from its name and key alone
_READERS = {
	"inventory": "frappe_wolf.api.scope.inventory_triples_for",
	"signals": "frappe_wolf.api.dashboard.signal_series_for",
}


def seconds():
	value = frappe.conf.get("wolf_read_cache_seconds")
	return DEFAULT_SECONDS if value is None else max(cint(value), 0)


def _cache_key(name, key):
	return f"{_PREFIX}|{name}|{key}"


def _read(name, key):
	return frappe.get_attr(_READERS[name])(key)


def _store(name, key, ttl):
	"""Read the value now and keep it for STALE_FACTOR x ttl. Returns (value, as_of)."""
	as_of = now_datetime()
	value = _read(name, key)
	frappe.cache.set_value(_cache_key(name, key), {"value": value, "as_of": as_of}, expires_in_sec=ttl * STALE_FACTOR)
	return value, as_of


def get(name, key):
	"""(value, as_of) of the read `name` for `key` (a string its reader understands).

	Fresh: from the cache. Stale but younger than STALE_FACTOR x the fresh time: from the cache,
	and read again in the background. Older or missing: read now.
	"""
	key = str(key)
	ttl = seconds()
	if not ttl:
		return _read(name, key), now_datetime()
	frappe.cache.hset(_ACTIVE, f"{name}|{key}", now_datetime())
	# always from Redis, never from frappe.local.cache: that one lives as long as the process
	# in a worker or a script, not as long as the value
	hit = frappe.cache.get_value(_cache_key(name, key), expires=True, use_local_cache=False)
	if hit is None:
		return _store(name, key, ttl)
	if (now_datetime() - hit["as_of"]).total_seconds() >= ttl:
		frappe.enqueue(
			"frappe_wolf.wolf.read_cache.refresh",
			queue="short",
			job_id=_cache_key(name, key),
			deduplicate=True,  # one re-read at a time per value, however many pages ask
			name=name,
			key=key,
		)
	return hit["value"], hit["as_of"]


def refresh(name, key):
	"""Background job: read one value again (get, when it is stale)."""
	ttl = seconds()
	if ttl:
		_store(name, key, ttl)


def refresh_active():
	"""Scheduler, every minute: read again the values asked for in the last ACTIVE_SECONDS that
	are no longer fresh, so a page opened between two visits does not wait for them."""
	ttl = seconds()
	if not ttl:
		return
	now = now_datetime()
	active = frappe.cache.hgetall(_ACTIVE)
	gone = [field for field, at in active.items() if (now - at).total_seconds() > ACTIVE_SECONDS]
	if gone:
		frappe.cache.hdel(_ACTIVE, gone)
	recent = sorted(((at, field) for field, at in active.items() if field not in gone), reverse=True)[:MAX_ACTIVE]
	for _at, field in recent:
		name, _, key = (field.decode() if isinstance(field, bytes) else field).partition("|")
		if name not in _READERS:
			continue
		hit = frappe.cache.get_value(_cache_key(name, key), expires=True, use_local_cache=False)
		if hit is None or (now - hit["as_of"]).total_seconds() >= ttl:
			_store(name, key, ttl)
		frappe.db.rollback()  # reads only: end the transaction between values


def clear():
	"""Drop every cached read of this site (tests, after changing what they read)."""
	frappe.cache.delete_keys(f"{_PREFIX}|")
	frappe.cache.delete_value(_ACTIVE)
