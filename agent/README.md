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
| `wolf-agent discover [--bench PATH] [--platform auto\|press\|generic]` | show what bench discovery finds |
| `wolf-agent check [--from-start] [--status] [--platform …]` | collect once and print the JSON payload, nothing is sent; the watchdog state goes to stderr (`--status` also asks the site for its health) |
| `wolf-agent mark-deployment --bench NAME [--site S] [--note TEXT] [--silence 15m]` | put a deployment marker on the Wolf timeline; `--silence` also opens a maintenance window that mutes notifications for that bench/site |
| `wolf-agent test-fallback` | send a test message through the watchdog fallback channel(s) |

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
- **Redis / RQ** (the bench's `redis_queue`): `queue_length` for every `rq:queue:*` list;
  `rq_failed_jobs` (failed-job registry size) and `rq_failed_jobs_added` (its increase per push);
  `rq_long_running_jobs` (started jobs older than `rq.long_job_minutes`, default 30);
  `rq_workers` (live workers); `redis_used_memory_percent` and `redis_evicted_keys` from `INFO`.
  Only the queue Redis is watched: the cache Redis is an LRU cache by design (Frappe's
  `redis_cache.conf` sets `maxmemory` with `allkeys-lru`), so a full cache and evictions are normal.
- **Backups**: `backup_age_hours` per site, from the newest
  `sites/<site>/private/backups/*-database.sql*` (every `backups.interval`, 10 min). A site that
  never had a backup sends nothing, so "No backup for 26 h" cannot fire for it. Press benches are
  skipped (`backups.press`), since Press uploads backups offsite and its local copies are not a
  reliable signal. Offsite-only setups can point `backups.dirs` at a mounted directory per site.
- **TLS**: `tls_days_left` for every public site name and custom domain (`site_config.json`
  `domains`), every `tls.interval` (1 h), in a background thread. The agent connects to
  `<domain>:443` with SNI (or to `tls.connect_host`, e.g. `127.0.0.1`, to check the local nginx),
  reads the leaf certificate without verifying it, so expired and self-signed certificates are
  measured too, and times out after `tls.timeout` (5 s). `localhost`, `*.localhost`, `.local`,
  `.test`, bare names, IP addresses and the RFC 2606 `example.*` names are skipped; unreachable
  domains are logged and skipped. Add names with `tls.domains`, drop some with `tls.exclude`.
- **MariaDB** through the `mariadb` (or `mysql`) client, with a site's own `db_name` /
  `db_password` and `db_host` / `db_port` from the bench (written to a private temporary option
  file, never on the command line): `db_threads_connected`, `db_connections_used_percent`,
  `db_slow_queries` (increase per push) and, on a replica, `db_replication_lag_seconds` and
  `db_replication_running`. A site user may not read replica status (REPLICATION CLIENT /
  SLAVE MONITOR); then it is skipped, unless `mariadb.user`/`password` or `mariadb.defaults_file`
  names a monitoring user. Without a client on the host, or for Postgres benches, nothing is sent.
- **Processes**: `supervisorctl status` (the socket is group-accessible for the bench user's
  group after `bench setup supervisor`), else `systemctl list-units` for service units whose name
  contains the bench name. `bench_processes_down` per bench counts processes that are STOPPED,
  EXITED, BACKOFF, FATAL or UNKNOWN (systemd: `failed` or a restart loop). A process that enters
  FATAL/UNKNOWN (systemd `failed`) also sends one `worker_failure` / `process_down` event. Press
  benches run their processes inside containers and are skipped.

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
  "extra_logs": [{"path": "/var/log/myapp/error.log", "parser": "traceback", "bench": null, "site": null}],
  "rq": {"long_job_minutes": 30},
  "backups": {"enabled": true, "interval": 600, "press": false, "dirs": {}},
  "tls": {"enabled": true, "interval": 3600, "port": 443, "timeout": 5, "connect_host": null,
          "domains": ["status.example.com"], "exclude": []},
  "mariadb": {"enabled": true, "cli": null, "host": null, "port": null, "user": null,
              "password": null, "defaults_file": null},
  "processes": {"enabled": true, "manager": "auto"},
  "watchdog": {
    "enabled": true,
    "stale_minutes": 5,
    "webhook_url": "https://chat.example.com/hooks/xxxx",
    "smtp": {"host": "smtp.example.com", "port": 587, "starttls": true, "from": "wolf@example.com",
             "to": ["ops@example.com"], "user": "wolf", "password": "…"}
  }
}
```

`platform`: `null` auto-detects Press, `"press"` forces it, `"generic"` disables it.
`auth_log`: `null` auto-detects, a path forces a file, `"journal"` forces journalctl,
`false` disables. `nginx_access_log` may be a list. Parsers for `extra_logs`:
`traceback`, `scheduler`, `worker`, `nginx`, `auth`.
`rq`, `backups`, `tls`, `mariadb`, `processes`: every key is optional (defaults above), and
`"tls": false` (same for the others) turns a check off. `processes.manager` is `auto`,
`supervisor` or `systemd`. `wolf-agent check` runs every check once, inline, TLS included.

## Watchdog

Wolf alerts are sent by the Wolf site. When that site, its database or its scheduler
is down, nobody would hear about it, so the agent watches the site and alerts through
its own **fallback channel**, which does not go through the site:

- every push response carries the site's `health` ([protocol §4](../docs/protocol.md));
- the agent raises an outage when pushes have failed for `stale_minutes` (site, proxy or
  database down), or when the site reports its alert rules (or, with no rule enabled, its
  scheduled jobs) have not run for `stale_minutes`: scheduler stopped or paused, no
  worker, Redis down, or stored times pushed into the future by a time-zone change;
- one alert per outage, then one recovery notice when pushes work and rules run again.
  The state lives in `<spool_dir>/watchdog-state.json`, so a restart neither repeats the
  alert nor forgets the outage. A delivery that fails is retried every minute.
- If the site rejects the agent's credentials, a last alert is sent before the agent exits.

Channels (`watchdog` in the config, both optional, both used when set):

- `webhook_url`: POSTs `{"text": "..."}`, which Slack, Mattermost and Rocket.Chat
  incoming webhooks accept.
- `smtp`: `host`, `port` (default 25, 587 with `starttls`, 465 with `ssl`), `from`, `to`
  (list or comma-separated), `starttls` (default on for port 587), `ssl`, `user`,
  `password`, `verify_tls`.

With no channel configured the watchdog only logs (journal) at ERROR level. Set
`"enabled": false` to turn it off. Run `wolf-agent test-fallback` after editing the config,
and `wolf-agent check` to see the current watchdog state.

While its rules are stale, the site also records a `scheduler_stalled` event on each push;
the **Scheduler stalled** preset turns it into an incident once the scheduler runs again,
so the outage shows up on the Wolf timeline afterwards.

## Tests

```bash
python3 -m unittest discover -s agent/tests
```
