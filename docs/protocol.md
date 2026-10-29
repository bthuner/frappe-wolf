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

Response: `{"message": {"metrics": <accepted>, "events": <accepted>, "rejected": [...],
"push_interval": 30, "health": {...}}}`. `health` is Wolf's own heartbeat (§4); it is `null`
if the site could not compute it, and absent on older servers.
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
| `host_metric_alert` | created by the app: metric rule fired, `agent_silent`, `scheduler_stalled` |
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
{"bench": "main", "site": null, "note": "bench update", "source": "manual", "timestamp": "..."}
```

Equivalent to pushing a single `deployment` event. `wolf-agent mark-deployment` calls it.

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
