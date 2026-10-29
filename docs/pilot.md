# Wolf on Pilot benches

**Which Pilot this is.** Earlier Wolf notes called Pilot "Dokos' bench manager" and guessed
its `bench.toml` schema. The primary sources say otherwise. Pilot is **Frappe's**
server manager, published at [github.com/frappe/pilot](https://github.com/frappe/pilot)
("Frappe Server Manager"). Dokos' public GitLab group (`gitlab.com/dokos`) has no Pilot
repository. The Dokos documentation mentions Pilot only through the `pilot_endpoint` /
`pilot_auth_token` site keys used by the Cloud Settings panel for Frappe Cloud v2 sites
([dokos/documentation `content/2.next/3.dodock/configuration-cloud.md`](https://gitlab.com/dokos/documentation/-/blob/main/content/2.next/3.dodock/configuration-cloud.md)).
Those are the same keys frappe/pilot writes into `site_config.json`. Everything below is
read from frappe/pilot at commit
[`34613ea`](https://github.com/frappe/pilot/tree/34613eade8008a15a9ae61d28fca2627db52e7b9) (2026-10-29).

## Summary

| Area | What Wolf does | Status |
|------|----------------|--------|
| Discovery | Searches `/home/*/pilot/benches/*` in addition to the old globs. A `bench.toml` with `[bench] name` and `python` is treated as a Pilot bench and read with its real keys | done, unit tested |
| Sites | Reads `sites/*/site_config.json`, plus `domains` (plain strings or `{"domain": …, "route": …}`) for host→site mapping | done, unit tested |
| Process logs | Tails `web`, `schedule`, `worker_<queues>_<i>` and their stderr files, for both the systemd and the supervisor naming | done, unit tested |
| nginx | Tails `<bench>/logs/nginx-access.log`, which uses the `pilot_access` format (a new parser branch) | done, unit tested |
| Redis queue | Reads `queue_length`, the RQ failed/started registries, live workers and Redis memory/evictions from `redis://127.0.0.1:<[redis] queue_port>`. `common_site_config.json`'s `redis_queue` takes precedence when present. | done |
| Backups, TLS, MariaDB | Same as on any bench: `sites/<site>/private/backups/`, each site and `domains` entry, the site's database credentials | done (generic) |
| Processes | `supervisorctl status`, else systemd units whose name contains the bench name (`bench_processes_down`). Pilot's exact unit/program names were not checked: if they do not contain the `[bench] name`, nothing is reported | unverified |
| Deployments | Emits a `deployment` event (category `pilot`) when an update, migrate, branch switch, app install, build, restart or restore task **starts**, and an `error` event (`pilot_task_failed`) when one fails. Read from the Admin API, or from the task files. | done, unit tested |
| "Enable Wolf for this bench" toggle in Pilot | Needs a change in frappe/pilot | **not done** (see [What Pilot would need](#what-pilot-would-need)) |
| Real Pilot host | **Not tested yet** | — |

## Layout (from the sources)

- **Install and benches.** `install.sh` installs into `PILOT_DIR="$HOME/pilot"`
  ([install.sh#L18](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/install.sh#L18)). Benches live in
  `cli_root()/benches` ([pilot/utils.py#L63-L70](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/utils.py#L63-L70)), so by
  default `~/pilot/benches/<name>/`. On a root-only VPS the installer creates a non-root
  bench user, hence the `/home/*/pilot/benches/*` glob. Each bench holds `apps/ sites/ env/ logs/ config/ pids/ bench.toml`
  ([docs/architecture.md#state-layout](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/docs/architecture.md#state-layout)).
- **bench.toml.** The schema is documented in
  [docs/configuration.md](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/docs/configuration.md) and modelled in
  [pilot/config/](https://github.com/frappe/pilot/tree/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/config). Wolf reads:
  - `[bench] name` (required, together with `python`). Wolf uses it as the bench name and as the Pilot marker.
  - `[redis] queue_port` (default 11000, [pilot/config/redis.py#L11-L15](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/config/redis.py#L11-L15)).
  - `[admin] port` (default 7000). The Admin backend binds `127.0.0.1:<port + 1>`
    ([pilot/config/admin.py#L77-L79](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/config/admin.py#L77-L79),
    [pilot/managers/gunicorn/__init__.py#L35](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/gunicorn/__init__.py#L35)).
  - `[admin] jwt_secret`, read at runtime only and never written to the Wolf config.
  - `[production] enabled`, kept for information.

  `bench.toml` has **no** nginx or logs keys: "Nginx uses compiled-in defaults; it has no
  `[nginx]` table" ([docs/configuration.md#L178](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/docs/configuration.md#L178)).
  `[monitor]` only names Pilot's own metric log. A `bench.toml` that is not Pilot's (no
  `[bench] python`) still goes through the old defensive parser.
- **Process logs** go to `<bench>/logs/`. The names come from
  [pilot/managers/processes/definitions.py#L200-L370](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/processes/definitions.py#L200-L370):
  `web.log`, `worker_<queue-slug>_<i>.log` and `schedule.log`. The slug is the queue list with
  every non-alphanumeric run replaced by `_` ([definitions.py#L69-L71](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/processes/definitions.py#L69-L71)). stderr goes to
  `<log>.error.log` under systemd (`web.log.error.log`,
  [systemd_render.py#L65-L66](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/processes/systemd_render.py#L65-L66))
  and to `<name>.error.log` under supervisor
  ([supervisor.py#L77-L78](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/processes/supervisor.py#L77-L78)).
  Wolf tails both spellings. Missing files cost nothing. Frappe's own `frappe.web.log`,
  `scheduler.log` and `worker.log` are tailed as on every bench. Worker log names are
  globbed at registration. Re-run `wolf-agent register` after changing `[[workers]]`.
- **nginx.** Each site vhost logs `location /` to `<bench>/logs/nginx-access.log` in this format
  ([server.conf.template#L15](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/nginx/templates/server.conf.template#L15),
  [bench.conf.template#L349](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/nginx/templates/bench.conf.template#L349)):

  ```
  log_format pilot_access '$pilot_client_addr [$time_local] "$request_method $uri" $status "$host" $request_time';
  ```

  This is not the combined format: it has no ident/user, no byte count and no protocol.
  Wolf's nginx parser now accepts it. Pilot's own access-log reader uses the same shape
  ([admin/backend/providers/site_access_log.py#L14-L18](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/admin/backend/providers/site_access_log.py#L14-L18)).
  `$host` is the site or one of its `domains`.
- **Tasks.** Long operations are tasks stored as `<bench>/tasks/<YYYYMMDD-HHMMSS-hex>/`, with
  `meta.json` (`task_id`, `command`, `args`, `queued_at`, `started_at`, `finished_at` as UTC
  ISO-8601, `exit_code`, `failure`) and a `status` file (`queued`, `running`, `success`,
  `failed`, `killed`) ([pilot/internal/tasks/store.py#L33-L37](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/internal/tasks/store.py#L33-L37),
  [files.py#L55-L66](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/internal/tasks/files.py#L55-L66),
  [pilot/managers/task/reader.py#L210-L245](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/task/reader.py#L210-L245),
  [pilot/internal/tasks/models.py#L9-L14](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/internal/tasks/models.py#L9-L14)).
  The directories are created with mode `0700`
  ([pilot/utils.py#L18](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/utils.py#L18), [#L47-L52](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/utils.py#L47-L52)), so only the bench
  user can read them.

## Deployment events from Pilot tasks

Task commands and their arguments come from `command: ClassVar[str]` in
[pilot/tasks/](https://github.com/frappe/pilot/tree/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/tasks). Wolf reports
`update`, `retry-update`, `complete-migration`, `migrate`, `revert-migration`, `revert-apps`, `revert-site`,
`switch-branch`, `get-and-install-app`, `install-app`, `uninstall-app`, `remove-app`, `build`,
`restart-services`, `switch-lite-mode`, `setup-nginx`, `restore-site` and `reinstall-site`.
The site comes from the task's `site` / `sites` arguments. The bench is the Pilot bench
itself, since tasks are per bench.

Wolf tries two sources, in this order:

1. **Admin API**: `GET http://127.0.0.1:<admin.port+1>/api/v1/tasks?limit=100`
   ([admin/backend/api/v1/tasks.py#L43-L75](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/admin/backend/api/v1/tasks.py#L43-L75),
   mounted at `/api/v1/tasks` in [admin/backend/app.py#L86](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/admin/backend/app.py#L86)).
   It returns `TaskInfo.as_dict()` rows
   ([pilot/managers/task/models.py#L45-L62](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/pilot/managers/task/models.py#L45-L62)).
   The endpoint needs a **bench-scoped** session: `Authorization: Bearer <JWT>`
   ([admin/backend/middleware.py#L35-L46](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/admin/backend/middleware.py#L35-L46), [#L95-L101](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/admin/backend/middleware.py#L95-L101)).
   Pilot signs these tokens locally with HS256 over `{"sub": "admin", "iat", "exp", "scope": "bench", "jti"}`
   using `[admin] jwt_secret`, and verifies them with `require: ["exp"]`
   ([admin/backend/internal/session.py#L336-L354](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/admin/backend/internal/session.py#L336-L354)).
   Wolf uses `pilot.token` when you set one. Otherwise it mints a one-hour token with that
   same claim shape, from `pilot.jwt_secret` or from `bench.toml` when Wolf can read it.
   Each minted token has a `wolf-…` jti, so it appears in Pilot's active sessions and can be revoked.
2. **Task files**: `<bench>/tasks/*/meta.json` + `status`. This only works when the Wolf agent
   runs as the bench user, because of the `0700` mode.

Both sources follow the same rules, the same ones as for Press. A `deployment` event is sent
when the task starts, timestamped at `started_at`, so an incident during a long migrate
links to it. An `error` event is sent if the task ends `failed`. It carries `failure.code` /
`message` and `exit_code`. Reported task ids are kept for 3 days in
`<spool_dir>/pilot-tasks.json`. The first poll skips history and only picks up queued/running tasks.

This replaces manual `wolf-agent mark-deployment --source pilot` markers on Pilot benches.

## Configuration

```json
{
  "pilot": {
    "deployments": true,
    "api_url": null,
    "token": null,
    "jwt_secret": null
  }
}
```

- `api_url`: overrides the per-bench `http://127.0.0.1:<admin.port+1>`. It applies to every
  Pilot bench on the host, so set it only on single-bench hosts.
- `token` / `jwt_secret`: one of them is needed unless the `wolf-agent` user can read
  `bench.toml`. Pilot's own file modes for `bench.toml` were not checked.
  The config file is written with mode 0600.
- `deployments: false` turns task polling off.

## Wolf next to Pilot's own monitoring

From the Pilot docs:

- `pilot-monitor.service` collects host, database and bench metrics every 10 s and pings
  production sites every 5 s. Its logs go to `system/logs/`
  ([docs/production.md#monitoring](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/docs/production.md#monitoring)).
- `[resource_limits]` raises CPU, memory, disk and uptime alerts after five minutes. They go to
  Central (on Central-managed hosts), to `webhook_endpoints` (POST with a Bearer header) and
  to mail ([docs/configuration.md#alert-delivery](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/docs/configuration.md#alert-delivery)).
- `[telemetry]` ships metrics and logs (Fluent Bit tails `benches/*/logs/*.log`) to a
  region's "Datum" endpoint with a Central-issued token
  ([docs/configuration.md#telemetry](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/docs/configuration.md#telemetry),
  [docs/architecture.md](https://github.com/frappe/pilot/blob/34613eade8008a15a9ae61d28fca2627db52e7b9/docs/architecture.md#integration-points)).

Wolf reads the same local files and does not interfere with any of these. What it adds on a
self-hosted Pilot host: tracebacks, failed scheduled jobs and worker crashes as events,
5xx/502 per site, SSH signals, and incidents tied to the Pilot task that preceded them.
All of it is kept on your own Wolf site. Pilot's resource alerts could be pointed at Wolf
through `webhook_endpoints`, but their payload is not documented, so Wolf does not accept them yet.

## What Pilot would need

These items from issue #17 need changes or answers from the Pilot maintainers:

1. **"Enable Wolf for this bench" toggle.** This means a `[wolf]` table in `bench.toml`
   (`enabled`, `url`) plus an Admin settings switch, which would install and register the
   agent with the bench's paths. It belongs in frappe/pilot, and nothing like it exists there today.
2. **A machine token for the Admin API.** Today a bench-scoped session is a 24 h
   password session, or a JWT signed with `jwt_secret` / issued by the configured JWKS.
   A long-lived, read-only `tasks:read` token would avoid sharing `jwt_secret` with Wolf.
3. **A task event feed** (webhook or SSE across tasks) instead of polling `/tasks`.
4. **File modes.** Confirmation that `bench.toml` and `logs/` stay readable for a monitoring
   group, since tasks are `0700` by design.

## Unverified

These points come from reading the sources and running unit tests against fixtures built
from them. Nothing has been run against a live Pilot host. In particular, these are untested:
the minted token against a real Admin backend, `/api/v1/tasks` through Pilot's rate limits,
file permissions for a non-bench user, and the exact worker queue-slug spelling on a running bench.
