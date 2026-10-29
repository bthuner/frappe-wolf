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
| `wolf-agent discover [--bench PATH]` | show what bench discovery finds |
| `wolf-agent mark-deployment --bench NAME [--site S] [--note TEXT]` | put a deployment marker on the Wolf timeline |

Global options: `--config PATH` (or `$WOLF_AGENT_CONFIG`), `-v`.

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
  "benches": [
    {"name": "frappe-bench", "path": "/home/frappe/frappe-bench", "sites": ["a.example.com"],
     "logs_dir": "/home/frappe/frappe-bench/logs", "nginx_access_log": "/var/log/nginx/access.log",
     "redis_queue": "redis://127.0.0.1:11000"}
  ],
  "extra_logs": [{"path": "/var/log/myapp/error.log", "parser": "traceback", "bench": null, "site": null}]
}
```

`auth_log`: `null` auto-detects, a path forces a file, `"journal"` forces journalctl,
`false` disables. `nginx_access_log` may be a list. Parsers for `extra_logs`:
`traceback`, `scheduler`, `worker`, `nginx`, `auth`.

## Tests

```bash
python3 -m unittest discover -s agent/tests
```
