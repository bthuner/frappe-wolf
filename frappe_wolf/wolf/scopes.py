"""Which sites, benches and hosts a restricted Wolf User may see (pure logic, see access.py).

`grants` is None for an unrestricted user, else {scope_type: tuple(values)} built from the
user's Wolf User Scope rows. A record is visible when any of its scope columns holds a
granted value (rows are OR-ed): a user granted site X sees X's events whatever bench or
host they came from, and a host-wide event (no site) only with a host grant.
"""

SCOPE_TYPES = ("site", "bench", "host")
_SCOPED = {"site": "site", "bench": "bench", "host": "host"}

# doctype -> {scope_type: column}
COLUMNS = {
	"Wolf Event": _SCOPED,
	"Wolf Metric Sample": _SCOPED,
	"Wolf Metric Rollup": _SCOPED,
	"Wolf Incident": _SCOPED,
	"Wolf Silence": _SCOPED,
	# an agent's hostname is its host; a satellite's hostname is its site name
	"Wolf Agent": {"host": "hostname", "site": "hostname"},
}


def build_grants(rows):
	grants = {}
	for row in rows:
		scope_type, value = row.get("scope_type"), (row.get("value") or "").strip()
		if scope_type in SCOPE_TYPES and value:
			grants.setdefault(scope_type, set()).add(value)
	return {k: tuple(sorted(v)) for k, v in grants.items()}


def row_visible(row, grants, columns):
	if grants is None:
		return True
	for scope_type, values in grants.items():
		column = columns.get(scope_type)
		if column and row.get(column) in values:
			return True
	return False


def _pairs(grants, columns):
	"""(column, values) to OR together; one entry per column even if two scope types share it."""
	merged = {}
	for scope_type, values in grants.items():
		column = columns.get(scope_type)
		if column and values:
			merged.setdefault(column, set()).update(values)
	return [(column, tuple(sorted(values))) for column, values in sorted(merged.items())]


def sql_literal_condition(grants, columns, table, escape):
	"""WHERE fragment with escaped literals, for permission_query_conditions. None = no restriction."""
	if grants is None:
		return None
	parts = [f"{table}.`{column}` in ({', '.join(escape(v) for v in values)})" for column, values in _pairs(grants, columns)]
	return "(" + " or ".join(parts) + ")" if parts else "1=0"


def sql_param_condition(grants, columns, table=None):
	"""(" and (...)", params) for raw SQL with named parameters; ("", {}) when unrestricted."""
	if grants is None:
		return "", {}
	prefix = f"{table}." if table else ""
	parts, params = [], {}
	for i, (column, values) in enumerate(_pairs(grants, columns)):
		params[f"wolf_scope_{i}"] = values
		parts.append(f"{prefix}`{column}` in %(wolf_scope_{i})s")
	return " and (" + (" or ".join(parts) if parts else "1=0") + ")", params
