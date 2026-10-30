# Wolf UI API

Read APIs behind the Wolf UI (`/wolf`, [#19](https://github.com/bthuner/frappe-wolf/issues/19)).
They need the Wolf User, Wolf Manager or System Manager role, and apply the caller's access
rules (see [Privacy and access](../README.md#privacy-and-access)) inside the query. Answers are
not cached, so each one is the caller's own.

## Common arguments

### Time window

| Argument | Meaning |
|---|---|
| `range` | Relative window ending now: a number and `m`, `h` or `d`, such as `15m`, `24h`, `7d`. Default `24h`. |
| `since`, `until` | Explicit window, ISO 8601. A time without an offset is read as UTC, as in the agent protocol. `until` defaults to now and needs `since`. They win over `range`. |

A relative window is measured in real time, so across a DST change `24h` is 24 hours, not 23
or 25 hours of wall clock. Windows longer than 400 days, or with `since` after `until`, are
refused. The Desk overview APIs (`api/ui.py`) accept the same `range` values. Every response that
reads a window returns what it actually read, in UTC:

```json
"window": {"since": "2026-10-29T13:00:00Z", "until": "2026-10-30T13:00:00Z", "relative": "24h", "time_zone": "Europe/Paris"}
```

`time_zone` is the site's. Stored times are in that zone; the UI converts with it, never with
the browser's zone.

### Scope

`site`, `bench`, `host` narrow a query to records with those values. **`bench` needs `host`**:
bench names such as `frappe-bench` repeat across hosts, so a bench alone is ambiguous and
refused.

### Access

A restricted Wolf User (one with Wolf User Scope rows) gets counts and inventory over the
records they may see, and `"restricted": true`. A Wolf User **without any** Wolf User Scope
row is not restricted: they see everything, as in Desk. That is the current rule, kept as is.

## `frappe_wolf.api.scope.get_counts`

Exact counts for the overview and entity dashboards. Arguments: window and scope.

```json
{
  "window": {...},
  "scope": {"host": "bench-01"},
  "restricted": false,
  "incidents": {
    "unresolved": 60, "unacknowledged": 40, "critical_unresolved": 7,
    "by_status": {"Open": 40, "Acknowledged": 20},
    "by_severity": {"info": 0, "warning": 53, "critical": 7},
    "sites_with_unresolved": 1, "unresolved_without_site": 5,
    "opened_in_window": 61, "resolved_in_window": 1,
    "basis": "..."
  },
  "events": {"records": 12, "occurrences": 140, "by_type": {"error": {"records": 3, "occurrences": 90, "by_severity": {...}}}, "basis": "..."},
  "agents": {"agent": {"fresh": 3, "stale": 1, "never_seen": 0, "disabled": 0}, "satellite": {...}, "silence_minutes": 5, "basis": "..."}
}
```

- **Incidents:** `unresolved` counts Open and Acknowledged incidents now, whatever the window.
  `unacknowledged` is Open only. Merged incidents are excluded. `unresolved_without_site`
  counts host- or bench-wide incidents, which have no site. `opened_in_window` and
  `resolved_in_window` use `started_at` and `resolved_at`.
- **Events:** `records` are Wolf Events first seen in the window. `occurrences` sums their
  reported `count`. A deduplicated record keeps counting repeats, including repeats after the
  window, so `occurrences` is not a count of occurrences inside the window.
- **Agents:** agents are hosts, so `site` and `bench` do not filter them. A satellite is not
  a host and is counted apart; with `host`, it counts when one of its registered benches runs on
  that host. `fresh` means seen within the agent silence threshold of Wolf Settings, the same rule
  as the Desk overview's online flag.

## `frappe_wolf.api.scope.get_inventory`

The hosts, benches and sites the caller may see. Argument: `days` (default 7, at most 90), the
telemetry window.

```json
{
  "hosts": [
    {"host": "bench-01", "sources": ["agent", "events", "metrics"], "last_seen": "2026-10-30T12:59:30Z",
     "benches": [{"bench": "frappe-bench", "host": "bench-01", "sources": [...], "last_seen": "...",
                  "sites": [{"site": "a.example.com", "sources": [...], "last_seen": "..."}]}],
     "sites": []}
  ],
  "unplaced_sites": [],
  "note": "Hosts and benches come from agent registrations and from telemetry in the window. ...",
  "days": 7,
  "restricted": false
}
```

Sources: `agent` and `satellite` (what an agent declared when it registered), `events` (by
when they last happened, so a recurring deduplicated event keeps its site listed), `metrics`
(rollups) and `incidents` (unresolved ones, whatever their age). A registration names every
site of a bench; a restricted user sees only the granted ones. Neither kind is complete on its own: a
bench added after its agent registered appears once it sends telemetry, and a quiet site only
through its agent's registration. The UI shows `sources` and `note` rather than presenting the
list as complete.

- A bench is always under its host.
- `host.sites` holds sites seen on a host whose bench is unknown.
- `unplaced_sites` holds sites seen without a host (for example a satellite with no host
  information); they are not attached to a guessed host.
- Times are UTC.

## Incident page

### `frappe_wolf.api.incident_view.list_incidents`

Incidents the caller may see, newest first. Arguments: `status` (`unresolved`, the default,
`resolved` or `all`), the scope arguments, `limit` (at most 200) and `cursor`. Pages follow
`(started_at, name)`, so two incidents started at the same second are neither repeated nor
skipped. Returns `incidents`, `paging` (`has_more`, `next_cursor`) and `time_zone`.

### `frappe_wolf.api.incident_view.get_incident`

The incident header in one call: title, status, severity, scope, start, acknowledgement,
resolution and owner, rule, silence, notification and escalation times, merge and split links,
and the timeline window. `deployment` gives the time of the deployment before the incident,
with `available: false` when retention deleted its event but the incident kept the time.
`event_records` (linked Wolf Events) and `event_count` (the repeats they absorbed) are
different numbers. `can` says which actions the caller may take: Wolf Users read and comment,
and Wolf Managers also acknowledge, resolve, take the incident and work on it in Desk.

### `frappe_wolf.api.incident_view.get_timeline`

One page of the timeline, oldest first. Arguments: `incident`, `limit` (default 100, at most
500), `cursor`, and `families`, a comma-separated subset of:

| Family | Rows |
|---|---|
| `evidence` | Wolf Events linked to the incident, at any time |
| `context` | Other events in its scope (site, the same bench on the same host, or host-wide) during its window, if the caller may see them, and the deployment before it even when that is older than the window. An incident with no site, bench or host has no context. |
| `activity` | Status changes, assignments, comments and notes, from Frappe's Version and Comment history |
| `delivery` | Notification attempts (Wolf Delivery) |

Each row has a stable `id` (`event:<name>`, `activity:comment:<name>`,
`activity:version:<name>:<field>`, `delivery:<name>`, `deployment:<incident>`), `family`,
`kind`, `at` (UTC) and a plain-text `summary`, plus per kind:

- **Events:** `relation` is `linked`, `context` or `preceding_change` (the deployment before the
  incident, never called its cause). `count`, `first_seen` and `last_seen` describe a
  deduplicated record: one row, not one row per occurrence. `details` is the event payload,
  for display as text.
- **Activity:** `user_name`. Comments and notes come as plain `text`, never HTML to render.
- **Deliveries:** `channel`, `action`, `target`, `result` and `http_status`. `result` is
  `accepted` when the endpoint took the message, which does not mean a person read it.

Rows are ordered by time, then events, activity and deliveries, then id. Rows with the same
time have no causal order. `paging.next_cursor` continues after the last row; the same rows
come back once each whatever the page size. Activity keeps the newest 200 recorded changes
and 300 comments; `truncated.activity_version` and `truncated.activity_comment` report when
older ones exist. `window` is the incident's window (start minus `padding_minutes`, to its
resolution or now), the same one Desk and the postmortem use.

### `frappe_wolf.api.incident_view.act`

POST `incident` and `action`, one of `acknowledge`, `resolve` or `assign_to_me`. It runs the
incident's own method, so Frappe's write permission applies, and returns the new
`get_incident`. The incident's state is checked first: acknowledging needs an Open incident,
and resolving or taking needs an unresolved one. A page left open on a stale incident gets
an error instead of reopening it. Merged incidents refuse every action. Merge, split and
postmortems stay in Desk for now.
