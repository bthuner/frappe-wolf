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
Re-registering the same hostname rotates the secret.

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

Response: `{"message": {"metrics": <accepted>, "events": <accepted>, "push_interval": 30}}`.
HTTP 401/403 → bad credentials (agent stops and logs). HTTP 5xx or network error → agent
keeps the batch in its on-disk spool and retries with backoff. Max 5000 metrics + 1000 events
per request; the agent splits larger batches.

### Event types (`type`)

| type              | typical `category` values                                           |
|-------------------|---------------------------------------------------------------------|
| `error`           | `http_5xx`, `http_502`, `traceback`                                 |
| `cron_failure`    | `scheduled_job`                                                     |
| `worker_failure`  | `worker_crash`, `job_failed`, `worker_killed`                       |
| `security`        | `ssh_failed_login`, `ssh_root_login`, `frappe_failed_login`, `new_admin_ip`, `privileged_user` |
| `host_metric_alert` | (created by the app when a metric rule fires)                     |
| `deployment`      | `manual`, `pilot`, `migrate`                                        |

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

## 4. Incident action links (alerts → app)

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
