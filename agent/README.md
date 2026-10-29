# Wolf Agent

Host-side daemon for Frappe Wolf. It collects host metrics, tails Frappe bench,
nginx and sshd logs, reads RQ queue lengths from Redis, and pushes everything to a
Wolf site over HTTPS (see [`../docs/protocol.md`](../docs/protocol.md)).
Pure Python ≥ 3.9 standard library, no dependencies.

## Install

On the host, from a checkout of this repository:

```bash
sudo WOLF_URL=https://wolf.example.com WOLF_TOKEN=<registration token> ./agent/install.sh
```

This creates a venv in `/opt/wolf-agent`, a system user `wolf-agent` (added to
`adm`, `systemd-journal` and `frappe` when those groups exist, so it can read
auth/nginx/bench logs without root), registers the host, writes
`/etc/wolf-agent/config.json` (mode 0600) and starts `wolf-agent.service`.
Set `WOLF_BENCHES="/home/frappe/frappe-bench /srv/other-bench"` to skip auto-discovery.

## Commands

| command | what it does |
|---------|--------------|
| `wolf-agent register --url URL --token TOKEN [--bench PATH]...` | register the host, discover benches, write the config |
| `wolf-agent run` | the daemon loop (what systemd runs) |
| `wolf-agent check [--from-start]` | collect once and print the JSON payload, nothing is sent |
| `wolf-agent discover [--bench PATH] [--platform auto\|press\|generic]` | show what bench discovery finds |
| `wolf-agent mark-deployment --bench NAME [--site S] [--note TEXT]` | put a deployment marker on the Wolf timeline |

Global options: `--config PATH` (or `$WOLF_AGENT_CONFIG`), `-v`.

## Press servers

On a server managed by Press (the Frappe Cloud stack), the agent detects
`/home/frappe/agent/config.json` and switches to Press mode. It lists benches under
the Press `benches_directory` and rescans them every minute, since each deploy creates
a new bench. It maps custom domains in the shared `/var/log/nginx/access.log` to sites,
and it reads queue lengths on each bench's `rq_port`. It also turns the Press agent's
deploy/update/migrate jobs (`jobs.sqlite3`, read-only) into `deployment` events, plus an
`error` event when one fails. Details, source references and limitations are in
[`../docs/press.md`](../docs/press.md).

## Pilot benches

Benches created by [Frappe Pilot](https://github.com/frappe/pilot) (`~/pilot/benches/*`,
`bench.toml` with `[bench] name` and `python`) are read with Pilot's real schema.
Wolf tails Pilot's process logs (`web`, `schedule`, `worker_*` and their stderr files) and
`logs/nginx-access.log` (in the `pilot_access` format), and it reads queues on
`[redis] queue_port`. It turns Pilot tasks (update, migrate, switch-branch, app install,
build, restart, restore) into `deployment` events, plus `error` events for failed tasks.
Tasks are read from the bench's Admin API (`GET /api/v1/tasks`), or from `<bench>/tasks/`
when the agent runs as the bench user. The API token is `pilot.token`, or one Wolf mints
from `pilot.jwt_secret` / `bench.toml` `[admin] jwt_secret`. See
[`../docs/pilot.md`](../docs/pilot.md).

## What is collected

- **Host**: `cpu_usage`, `memory_used_percent`, `swap_used_percent`, `load_1m/5m/15m`,
  `load_per_core`, `disk_used_percent` per real mount.
- **Per bench** (`<bench>/logs/`): tracebacks from `frappe.web.log`, `web.error.log`,
  `bench.log`; failed scheduled jobs from `scheduler.log`; worker crashes, OOM kills
  and failed jobs from `worker.log` / `worker.error.log`. Site-scoped
  `sites/<site>/logs/frappe.web.log` and `scheduler.log` are tailed too.
- **nginx** access log: `http_requests`, `http_5xx`, `http_502` per site per push
  interval, plus one aggregated event per (site, status, route). The site comes
  from `$host` when the log format includes it (leading vhost field or a trailing
  quoted field), else from a per-site log file name, else the bench's only site.
- **sshd** (`/var/log/auth.log`, `/var/log/secure`, or `journalctl -u ssh -u sshd`):
  failed logins aggregated per source IP, root logins.
- **Redis**: `queue_length` for every `rq:queue:*` list in the bench's `redis_queue`.

Logs are read from the end on first start; offsets are kept in
`/var/lib/wolf-agent/spool/tail-state.json` so restarts and log rotation lose nothing.
Pushes that fail are spooled to `/var/lib/wolf-agent/spool/outbox/` (capped at 50 MB)
and replayed in order with exponential backoff. If the Wolf site rejects the
credentials (401/403) the agent exits with status 2 and systemd stops restarting it.

## Config

`register` writes this; edit it to add logs or override discovery.

```json
{
  "url": "https://wolf.example.com",
  "agent_id": "WAG-0001",
  "agent_secret": "…",
  "hostname": "bench-01",
  "push_interval": 30,
  "spool_dir": "/var/lib/wolf-agent/spool",
  "verify_tls": true,
  "auth_log": null,
  "collect_host_metrics": true,
  "platform": null,
  "press": {"agent_dir": "/home/frappe/agent", "nginx_access_log": "/var/log/nginx/access.log",
            "rediscover_interval": 60, "deployments": true},
  "pilot": {"deployments": true, "api_url": null, "token": null, "jwt_secret": null},
  "benches": [
    {"name": "frappe-bench", "path": "/home/frappe/frappe-bench", "sites": ["a.example.com"],
     "logs_dir": "/home/frappe/frappe-bench/logs", "nginx_access_log": "/var/log/nginx/access.log",
     "redis_queue": "redis://127.0.0.1:11000"}
  ],
  "extra_logs": [{"path": "/var/log/myapp/error.log", "parser": "traceback", "bench": null, "site": null}]
}
```

`platform`: `null` auto-detects Press, `"press"` forces it, `"generic"` disables it.
`auth_log`: `null` auto-detects, a path forces a file, `"journal"` forces journalctl,
`false` disables. `nginx_access_log` may be a list. Parsers for `extra_logs`:
`traceback`, `scheduler`, `worker`, `nginx`, `auth`.

## Tests

```bash
python3 -m unittest discover -s agent/tests
```
