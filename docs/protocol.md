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
"push_interval": 30, "health": {...}, "clock_skew_seconds": 2, "clamped": 0, "queued": true}}`.
`health` is Wolf's own heartbeat (§4); it is `null` if the site could not compute it, and absent
on older servers.

**Response actions.** The response also carries `actions`: the blocks a Wolf Manager approved for
this agent and not yet reported, `[{"id": "WRA-00001", "action": "block_ip", "ip": "45.33.32.156",
"seconds": 86400}]` (empty when there are none). The agent sends each result with a later push
as `"action_results": [{"id": "WRA-00001", "ok": true, "detail": "..."}]`; an id is accepted once,
and only from the agent it was given to. See [cyber.md](cyber.md#step-5-human-validated-response-built-except-signed-links).

**Ingest mode.** With **Wolf Settings → Ingest Mode** `async` (the default), the app validates and
normalizes the batch during the request, queues the inserts for a background worker and answers
with `"queued": true`. `metrics` / `events` then count the items that passed validation and were
queued; the worker inserts them and attaches the events to open incidents a few seconds later. A
failure in the worker is not reported back to the agent (it is in the site's Error Log). If the
queue is unreachable, or with `sync` mode, the batch is stored during the request and `queued` is
`false`; storage errors then fail the request (HTTP 5xx) and the agent retries. The agent's last
seen time and clock skew are recorded during the request in both modes, so heartbeats do not wait
for the queue. `queued` is absent on older servers; agents must not rely on it.

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
| `error`           | `http_5xx`, `http_502`, `traceback`, `press_job_failed`, `pilot_task_failed`, `backup_failed`, `email_queue_backlog`, `email_queue_error` |
| `cron_failure`    | `scheduled_job`                                                     |
| `worker_failure`  | `worker_crash`, `job_failed`, `worker_killed`, `process_down`       |
| `security`        | `data_exfiltration`, `ssh_failed_login`, `ssh_root_login`, `ssh_login_after_failures`, `sudo_failure`, `account_created`, `admin_group_change`, `web_attack_probe`, `web_sensitive_file_exposed`, `server_script_change`, `client_script_change`, `console_execution`, `permission_change`, `security_settings_weakened`, `api_key_generated`, `webhook_external`, `service_exposed`, `insecure_site_config`, `frappe_failed_login`, `new_admin_ip`, `admin_login_new_country`, `privileged_user` |
| `host_metric_alert` | created by the app: metric rule fired, `agent_silent`, `scheduler_stalled` |
| `deployment`      | `manual`, `pilot` (Pilot tasks, see [pilot.md](pilot.md)), `migrate`, `press` (Press agent jobs, see [press.md](press.md)) |

`severity`: `info` | `warning` | `critical`.

With IP reputation enabled, a security event whose payload `ip` is on a downloaded blocklist
gets `reputation: {"listed": true, "lists": [...]}` at ingest and one severity step more.

Security payloads may carry `attack_techniques`, a list of MITRE ATT&CK technique IDs
(`T1110`, `T1552.001`). At ingest the app adds `mitre_attack`, a list of `{id, name, tactic}`
merging those IDs with the category's own mapping ([cyber.md](cyber.md#5-mapping-reference)).

Categories added with the infrastructure signals:

| category | raised by | when | payload |
|----------|-----------|------|---------|
| `process_down` | agent (`source`: `supervisor` / `systemd`) | a bench process enters supervisor `FATAL`/`UNKNOWN` or systemd `failed`; once per transition | `process`, `state`, `detail`, `manager` |
| `backup_failed` | site (Error Log hook) | an Error Log whose title/method names a backup (S3/Dropbox/Google Drive backup failures) or whose traceback goes through `frappe/utils/backups.py`; `critical` | as `traceback` events |
| `email_queue_backlog` | site, every 5 min (satellites too) | Email Queue rows `Not Sent`/`Sending`, due, older than **Wolf Settings → Email Queue Stale Minutes** (default 30); one event per site, bumped while it lasts | `pending`, `oldest`, `stale_minutes`, `suspended` |
| `email_queue_error` | site, every 5 min | Email Queue rows that went to `Error` in the last 15 min | `errors`, `latest` |
| `ssh_login_after_failures` | agent (`source`: `sshd`) | a successful SSH login from an address with 5+ failures in the hour before; `critical` | `ip`, `users`, `failures_before` |
| `sudo_failure` / `account_created` / `admin_group_change` | agent (`source`: `auth`) | failed sudo or user not in sudoers / `useradd` / user added to an admin group (`critical`) | `users`, `count`, `uid` / `group` |
| `web_attack_probe` | agent (`source`: `nginx`) | requests matching probe rules, per (site, client) and push interval; `critical` from 50 | `ip`, `count`, `rules`, `statuses`, `targets`, `user_agents` |
| `web_sensitive_file_exposed` | agent (`source`: `nginx`) | a secret-bearing file (`.env`, `.git/`, keys, dumps) answered with a 2xx; `critical` | as `web_attack_probe` |
| `server_script_change` / `client_script_change` | site (doc events) | a script created, enabled, or its code, type or trigger changed; `critical` for API / Scheduler Event Server Scripts and guest access | `script`, `change`, `script_type`, `reference_doctype`, `api_method`, `allow_guest`, `by`, `excerpt` |
| `console_execution` | site (Console Log) | a System Console run | `console_log`, `by`, `type`, `committed`, `excerpt` |
| `permission_change` | site (Role Permission Manager request, Role Profile, Custom DocPerm, User Permission, User) | a permission rule added, changed, removed or reset; a Role Profile created or granting more roles; a User Permission letting its user see more; a Website user given desk access | `doctype`, `role`, `action`, `ptype`, `value` / `role_profile`, `roles`, `added` / `kind` (`custom_docperm`, `user_permission`, `desk_access`), `granted`, `revoked`, `everyone`, `user`, `allow`, `for_value`, `previous`, `via` |
| `security_settings_weakened` | site (System Settings) | a save that lowers a defence; `critical` | `changes` (`field`, `label`, `old`, `new`), `by` |
| `api_key_generated` | site (User) | a new API secret; `critical` for privileged users | `user`, `api_key`, `privileged`, `by` |
| `data_exfiltration` | site (`after_request`) | a user's exports and list reads this hour past the floor and the factor times their largest hour of 14 days; `critical` at 20x or without history | `user`, `hour_bytes`, `hour_requests`, `hour_rows`, `baseline_bytes`, `ratio`, `factor`, `floor_bytes`, `last_request` (`kind`, `label`, `method`, `doctype`, `bytes`), `ip` |
| `service_exposed` | agent (`source`: `exposure`) | a bench port starts listening beyond loopback (or its addresses change); `critical` for Redis | `port`, `service`, `addresses`, `note` |
| `insecure_site_config` | agent (`source`: `exposure`) | a site's risky settings change and are not empty; `critical` with `developer_mode` | `findings` (`setting`, `value`, `why`) |
| `honeytoken_triggered` | site (Activity Log, `before_request`) | a decoy user, API key or path was used; `critical`, once per token and address every 10 min | `kind` (`user`, `api_key`, `path`), `token`, `ip`, `user_agent`, `path` |
| `regulatory_deadline` | app (`source`: `wolf`, every 5 min) | a NIS2 / GDPR deadline of a flagged incident is within 6 h (`warning`) or passed (`critical`); once per obligation and stage, linked to the incident | `incident`, `obligation`, `regime`, `stage`, `due_at`, `estimated` |
| `webhook_external` | site (Webhook) | a Webhook created, enabled or pointed at another host | `webhook`, `doctype`, `event`, `domain`, `previous_domain` |
| `admin_login_new_country` | site (Activity Log hook) | a privileged login from a new IP whose country (offline **GeoIP Database**) differs from the countries of all the user's earlier logins; `critical` | `user`, `ip`, `country`, `known_countries` |

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
| `rq_failed_jobs`     | jobs     | host, bench, `labels.queue`: size of RQ's failed-job registry (`rq:failed:<queue>`) |
| `rq_failed_jobs_added` | jobs per push interval | host, bench, `labels.queue`: increase of that registry since the previous push (never negative; none on the agent's first cycle) |
| `rq_long_running_jobs` | jobs   | host, bench, `labels.queue`: started jobs (`rq:wip:<queue>`) running longer than `rq.long_job_minutes` (default 30) |
| `rq_workers`         | workers  | host, bench: live members of `rq:workers` (dead workers whose key expired are not counted) |
| `redis_used_memory_percent` | % | host, bench, `labels.of`: queue Redis `used_memory` / `maxmemory` (`of=maxmemory`), else / `total_system_memory` (`of=system`) |
| `redis_evicted_keys` | keys per push interval | host, bench: increase of `evicted_keys` in the queue Redis |
| `backup_age_hours`   | hours    | host, bench, site: age of the newest `sites/<site>/private/backups/*-database.sql*`; every 10 min; sites without any backup send nothing |
| `tls_days_left`      | days (negative = expired) | host, bench, site, `labels.domain`: certificate `notAfter` of each public site name / custom domain, hourly |
| `exposed_services`   | count    | host: bench ports (MariaDB/Postgres, bench Redis, socket.io, gunicorn) listening on all interfaces or a public address, from `/proc/net/tcp{,6}`; every 10 min |
| `site_config_risks`  | count    | host, bench, site: `developer_mode`, `allow_tests`, a plain-HTTP `host_name`, `allow_cors: "*"` in the merged site config; every 10 min |
| `security_score`     | 0–100    | site: written by the app (hourly), not the agent; see [cyber.md](cyber.md#step-3-posture-score-and-exposure-built) |
| `user_export_bytes`  | bytes per hour | site, `labels.user`: response bytes of a user's exports and list reads over the past hour, written by the app (hourly); see [cyber.md](cyber.md#step-2-frappe-application-security-built) |
| `db_threads_connected` | connections | host, bench (when one bench uses the server), `labels.db` (`host:port`) |
| `db_connections_used_percent` | % | same: `Threads_connected` / `max_connections` |
| `db_slow_queries`    | queries per push interval | same: increase of `Slow_queries` |
| `db_replication_lag_seconds` | s | same: `Seconds_Behind_Master`, only on a replica and with REPLICATION CLIENT |
| `db_replication_running` | 0/1 | same: both replica threads running |
| `bench_processes_down` | processes | host, bench, `labels.manager`: supervisor processes in STOPPED/EXITED/BACKOFF/FATAL/UNKNOWN, or systemd units `failed` / restart loop |

Agents older than these signals simply do not send them. The app accepts any metric name, so no
server change is needed to receive them; the presets only need the samples to exist.

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
  "queues": {"lengths": {"short": 0, "default": 3, "long": 0}, "workers": 2,
             "ingest_queue": "short", "ingest_backlog_seconds": null}
}
```

| field | meaning |
|-------|---------|
| `rules.last_evaluated` | latest `last_evaluated` over enabled Wolf Alert Rules (the rule engine runs at each scheduler tick); `null` if no rule ran yet |
| `scheduler.enabled` / `reason` | `false` with `maintenance_mode`, `pause_scheduler`, `disable_scheduler` or `enable_scheduler unset in System Settings` |
| `scheduler.last_job_run` | latest `Scheduled Job Type.last_execution` (stamped when a worker starts a job) |
| `scheduler.tick_seconds` | seconds between Frappe scheduler ticks, so between two rule evaluations: 240 on Frappe 15 and Dodock unless `scheduler_tick_interval` is set; `null` on a Frappe without the setting. Informative, never a problem |
| `*.age_seconds` | server-side `now - timestamp`, so agent clock skew does not matter. **Negative** when the stored time lies in the future, which happens after a system time-zone change and stalls the scheduler until that time |
| `queues` | RQ queue lengths and worker count for the bench, or `{"error": "..."}` if Redis is unreachable |
| `queues.ingest_backlog_seconds` | how long the oldest job of the ingest queue (`ingest_queue`) has waited; `null` when it is empty. Queued pushes are answered before they are stored, so this is the only sign that telemetry is not arriving |
| `ok` / `problems` | the server's own reading with a 5 minute threshold |

Two things are problems: staleness, `abs(age) >= stale threshold` on `rules` (or on `scheduler`
when no rule is enabled), and an ingest backlog, `ingest_backlog_seconds >= stale threshold`.
Scheduler and queue state are context appended to the staleness text. The agent
applies its own threshold (`watchdog.stale_minutes`) to the raw ages rather than trusting `ok`.

While rules are stale, each push also records a `host_metric_alert` / `scheduler_stalled` event on
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

Response links, one per action Wolf proposes on the incident (up to 5):

```
GET /api/method/frappe_wolf.api.actions.response_action
    ?incident=WINC-00012&action=block_ip|disable_user|logout_sessions|revoke_api_keys&target=<ip or user>
    [&agent=<Wolf Agent>&hours=<1..168>]&expires=<unix seconds>[&actor=<email>]&sig=<hex>
```

`sig` is HMAC-SHA256 over `wolf-response-action\n<incident>\n<action>\n<target>\n<agent>\n<hours>\n<expires>\n<actor>`
with the same key. Only a logged-in Wolf Manager or System Manager can confirm (POST), through
the Response panel's rules. They appear in the text and HTML of the alert (email, one per
recipient, Raven, and the bodies of Matrix, Gotify and ntfy), as `incident.responses =
[{"label", "url"}]` in webhook bodies, as buttons in Slack and as links in Mattermost. SMS
carries none.

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
