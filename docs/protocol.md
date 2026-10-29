# Wolf Agent ⇄ Wolf App protocol (v1)

All traffic is HTTPS + JSON. All timestamps are ISO-8601 **UTC** strings
(`2026-10-29T02:00:13Z`). The app converts them to the site's system timezone.

## 1. Registration

```
POST /api/method/frappe_wolf.api.agent.register
Content-Type: application/json

{
  "registration_token": "<Wolf Settings → Registration Token>",
  "hostname": "bench-01",
  "agent_version": "0.1.0",
  "os": "Debian GNU/Linux 12",
  "benches": [{"name": "main", "path": "/home/frappe/frappe-bench", "sites": ["a.example.com"]}]
}
```

Response (`message` key, Frappe convention):

```json
{"message": {"agent_id": "WAG-0001", "agent_secret": "<random 40 chars>", "push_interval": 30}}
```

The secret is returned once; the agent stores it in `/etc/wolf-agent/config.json` (mode 0600).
The Wolf site keeps only a salted hash (`hmac-sha256$<salt>$<digest>` on Wolf Agent), so it
cannot show the secret again. Re-registering the same hostname, or **Rotate secret** on the
Wolf Agent form (which shows the new secret once), replaces it.

Optional `"kind"`: `"agent"` (default) or `"satellite"`. A satellite is another Frappe site
running `frappe_wolf` in satellite mode; it registers with `hostname` set to its site name and
one bench entry carrying its real `host`:

```json
{"registration_token": "...", "kind": "satellite", "hostname": "b.example.com", "agent_version": "0.1.0",
 "os": "Linux-6.1...", "benches": [{"name": "frappe-bench", "path": "/home/frappe/frappe-bench",
 "sites": ["b.example.com"], "host": "bench-01"}]}
```

A hostname already registered with the other kind is refused. A site in satellite mode refuses
all registrations (register with its central site instead).

## 2. Telemetry push

```
POST /api/method/frappe_wolf.api.ingest.push
Content-Type: application/json
X-Wolf-Agent-Id: WAG-0001
X-Wolf-Agent-Secret: <secret>

{
  "sent_at": "2026-10-29T02:00:30Z",
  "agent_version": "0.1.0",
  "hostname": "bench-01",
  "metrics": [
    {"metric_name": "cpu_usage", "value": 12.5, "timestamp": "...", "host": "bench-01",
     "bench": null, "site": null, "labels": {}}
  ],
  "events": [
    {"type": "error", "category": "http_502", "severity": "critical", "timestamp": "...",
     "host": "bench-01", "bench": "main", "site": "a.example.com", "source": "nginx",
     "title": "502 on GET /api/method/foo", "fingerprint": "sha1-of-stable-parts",
     "payload": {"route": "/api/method/foo", "status": 502, "count": 14, "sample_lines": ["..."]}}
  ]
}
```

Response: `{"message": {"metrics": <accepted>, "events": <accepted>, "rejected": [...],
"push_interval": 30, "health": {...}, "clock_skew_seconds": 2, "clamped": 0}}`. `health` is
Wolf's own heartbeat (§4); it is `null` if the site could not compute it, and absent on older
servers.

**Rate limit.** Pushes are counted per agent in one-minute windows (Wolf Settings → Max Pushes
Per Minute, default 120, 0 = off). Over the limit the server answers HTTP 429; agents and
satellites treat 429 like a network error: the batch stays spooled and is replayed after a backoff.

**Scrubbing.** The agent removes e-mail addresses, credentials, card numbers and IBANs from
event titles and payloads before sending (agent README, "Scrubbing"). The server stores what
it receives as is.

**Clock skew.** `sent_at` is stamped when the request is sent (also for replayed spool batches).
The server records `sent_at - server time` as the agent's `clock_skew_seconds` (positive = agent
ahead, shown on Wolf Agent) and returns it; the agent logs a warning above 60 s. Metric and event
timestamps more than **Wolf Settings → Max Clock Skew** (default 300 s, 0 = off) in the future
are clamped to the server time and counted in `clamped`. Rule windows and charts also ignore rows
stamped more than a minute in the future.
HTTP 401/403 → bad credentials (agent stops and logs). HTTP 5xx or network error → agent
keeps the batch in its on-disk spool and retries with backoff. Max 5000 metrics + 1000 events
per request; the agent splits larger batches.

Satellites use this same endpoint with their satellite credentials. They send no metrics, at
most 100 events per request, every event with explicit `host`/`bench`/`site`, `fingerprint` and
`count` (duplicates seen before forwarding are merged into one event). They push an empty body
every minute as a heartbeat. 401/403 → retry in 15 minutes; other 4xx (except 408/429) → the
batch is dropped; 5xx or network error → retry after 1, 2, 4, 8, then 15 minutes.

### Event types (`type`)

| type              | typical `category` values                                           |
|-------------------|---------------------------------------------------------------------|
| `error`           | `http_5xx`, `http_502`, `traceback`, `press_job_failed`, `pilot_task_failed` |
| `cron_failure`    | `scheduled_job`                                                     |
| `worker_failure`  | `worker_crash`, `job_failed`, `worker_killed`                       |
| `security`        | `ssh_failed_login`, `ssh_root_login`, `frappe_failed_login`, `new_admin_ip`, `privileged_user` |
| `host_metric_alert` | created by the app: metric rule fired, `agent_silent`, `scheduler_stalled` |
| `deployment`      | `manual`, `pilot` (Pilot tasks, see [pilot.md](pilot.md)), `migrate`, `press` (Press agent jobs, see [press.md](press.md)) |

`severity`: `info` | `warning` | `critical`.

### Metric names emitted by the agent

| metric_name          | unit     | scope / labels                    |
|----------------------|----------|-----------------------------------|
| `cpu_usage`          | %        | host                              |
| `memory_used_percent`| %        | host                              |
| `swap_used_percent`  | %        | host                              |
| `disk_used_percent`  | %        | host, `labels.mount`              |
| `load_1m` / `load_5m` / `load_15m` | — | host                        |
| `load_per_core`      | —        | host (load_1m / cpu count)        |
| `http_requests`      | count per push interval | host, bench, site  |
| `http_5xx`           | count per push interval | host, bench, site  |
| `http_502`           | count per push interval | host, bench, site  |
| `queue_length`       | jobs     | host, bench, `labels.queue`       |

## 3. Deployment marker

```
POST /api/method/frappe_wolf.api.ingest.deployment
X-Wolf-Agent-Id / X-Wolf-Agent-Secret headers
{"bench": "main", "site": null, "note": "bench update", "source": "manual", "timestamp": "...",
 "silence_minutes": 15}
```

Equivalent to pushing a single `deployment` event. `wolf-agent mark-deployment` calls it.

`silence_minutes` is optional (added after v1, backward compatible: older apps ignore it, and
agents omit it unless `--silence` is given). When it is a positive integer, the app also creates a
**Wolf Silence** (maintenance window) from now for that many minutes (capped at 24 h), scoped to the
agent's host plus the given bench and site. Incidents matching the silence still open and update,
so the timeline stays complete, but their notifications are held back; incidents still open when
the window ends are notified then.

Response: `{"message": {"event": "<Wolf Event name>"}}`, plus
`"silence": "WSIL-00001", "silence_until": "2026-10-29 02:15:00"` (site time zone) when a silence
was created. An agent that asked for a silence and gets no `silence` key back is talking to an app
without silence support and should warn.

## 4. Wolf health (heartbeat)

```
GET|POST /api/method/frappe_wolf.api.ingest.status
X-Wolf-Agent-Id / X-Wolf-Agent-Secret headers
```

Response: `{"message": {"agent": "WAG-0001", "push_interval": 30, "health": {...}}}`. The same
`health` object comes back with every push, so the agent does not need to poll `status`; it is
for `wolf-agent check` and scripts. Read-only: it does not update the agent's last seen.

```json
{
  "ok": false,
  "problems": ["alert rules not evaluated for 12 min; scheduler disabled (pause_scheduler)"],
  "checked_at": "2026-10-29T02:12:30Z",
  "server_version": "0.1.0",
  "rules": {"last_evaluated": "2026-10-29T02:00:01Z", "age_seconds": 749},
  "scheduler": {"enabled": false, "reason": "pause_scheduler",
                "last_job_run": "2026-10-29T02:00:00Z", "age_seconds": 750},
  "queues": {"lengths": {"short": 0, "default": 3, "long": 0}, "workers": 2}
}
```

| field | meaning |
|-------|---------|
| `rules.last_evaluated` | latest `last_evaluated` over enabled Wolf Alert Rules (the rule engine runs every minute on the scheduler); `null` if no rule ran yet |
| `scheduler.enabled` / `reason` | `false` with `maintenance_mode`, `pause_scheduler`, `disable_scheduler` or `enable_scheduler unset in System Settings` |
| `scheduler.last_job_run` | latest `Scheduled Job Type.last_execution` (stamped when a worker starts a job) |
| `*.age_seconds` | server-side `now - timestamp`, so agent clock skew does not matter. **Negative** when the stored time lies in the future, which happens after a system time-zone change and stalls the scheduler until that time |
| `queues` | RQ queue lengths and worker count for the bench, or `{"error": "..."}` if Redis is unreachable |
| `ok` / `problems` | the server's own reading with a 5 minute threshold |

Only staleness is a problem: `abs(age) >= stale threshold` on `rules` (or on `scheduler` when no
rule is enabled). Scheduler and queue state are context appended to the problem text. The agent
applies its own threshold (`watchdog.stale_minutes`) to the raw ages rather than trusting `ok`.

While `ok` is false, each push also records a `host_metric_alert` / `scheduler_stalled` event on
the site (bumped, not duplicated, for 15 minutes). The **Scheduler stalled** preset turns it into
an incident once the scheduler runs again. A stalled site cannot alert about itself, so the live
alert is raised by the agent watchdog through its own fallback channel (see `agent/README.md`).

## 5. Incident action links (alerts → app)

Email, Raven and webhook alerts for unresolved incidents carry signed links:

```
GET /api/method/frappe_wolf.api.actions.incident_action
    ?incident=WINC-00012&action=ack|resolve&expires=<unix seconds>[&actor=<email>]&sig=<hex>
```

`sig` is HMAC-SHA256 over `wolf-incident-action\n<incident>\n<action>\n<expires>\n<actor>` with a
key derived from the site's `encryption_key`. Links expire after **Wolf Settings → Action Link
Hours** (default 24, 0 disables them). `actor` is the recipient's address in emails (one email per
recipient), absent for Raven and webhooks.

GET only renders a confirmation page; the button POSTs the same parameters, and only the POST
acknowledges or resolves, so link scanners cannot act on an incident. The incident records
`acknowledged_by` / `resolved_by` as the logged-in user, else the `actor`, else `link`.

Webhook bodies expose the same URLs as `incident.actions = {"ack": "...", "resolve": "..."}`
(empty once resolved). Hourly digests are posted as
`{"text": "...", "digest": [{"incident", "kind", "severity", "title", "at", "url"}]}`.

Slack (Block Kit buttons), Mattermost (attachment links), ntfy (`Actions` header) carry the same
links. Matrix, Gotify and SMS carry the incident URL only.

## 6. Native channels (app → services)

Each native channel is a pure builder in `frappe_wolf/wolf/channels/` that maps a notification to
the documented format of the service (the module docstrings cite the reference pages):

| Channel | Request |
|---|---|
| Slack | `POST <webhook>` `{"text", "attachments": [{"color", "fallback", "blocks": [section, actions]}]}` |
| Mattermost | `POST <webhook>` `{"attachments": [{"fallback", "color", "pretext", "title", "title_link", "text", "fields", "footer"}]}` |
| Matrix | `PUT <homeserver>/_matrix/client/v3/rooms/<room>/send/m.room.message/wolf-<attempt>` (Bearer token) `{"msgtype": "m.text", "body", "format": "org.matrix.custom.html", "formatted_body"}` |
| ntfy | `POST <server>/<topic>`, body = text, headers `Title` (RFC 2047 when not ASCII), `Priority`, `Tags`, `Click`, `Actions`, optional `Authorization: Bearer` |
| Gotify | `POST <server>/message` (`X-Gotify-Key`) `{"title", "message", "priority", "extras": {"client::display", "client::notification": {"click": {"url"}}}}` |
| SMS | the configured template, one request per number |
| PagerDuty | `POST <events url>` `{"routing_key", "event_action", "dedup_key": "<incident>", "payload": {"summary", "source", "severity", "component", "group", "class", "custom_details"}, "client", "client_url", "links"}` (payload on trigger only) |

PagerDuty event actions: `fired`, `reopened`, `escalated` → `trigger`; acknowledgement →
`acknowledge`; resolution → `resolve`; a test sends `trigger` then `resolve`.

Every request is recorded as a **Wolf Delivery** `{incident, alert_rule, at, channel_type, target,
kind, action, digest, ok, http_status, error}`. `target` names the destination without secrets
(host, room, masked phone numbers); `error` has secrets and URL paths/queries removed. Digests of
native channels are merged on a SHA-256 of the channel settings, and the digest item keeps a
reference to the channel row rather than any secret.
