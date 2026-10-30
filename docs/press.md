# Wolf on Press (Frappe Cloud) servers

[Press](https://github.com/frappe/press) is the Frappe app behind Frappe Cloud. It
manages servers through the **Press agent** ([frappe/agent](https://github.com/frappe/agent)),
a Flask + RQ service that runs on each server. This page covers what the Wolf agent
does on an *app server* managed that way, which source files each behaviour relies on,
and what is still unverified.

Sources were read at these commits. Every link below is pinned to them:

- frappe/agent [`64b9812`](https://github.com/frappe/agent/tree/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a) (2026-10-27)
- frappe/press [`aedd7d1`](https://github.com/frappe/press/tree/aedd7d1237e49153e4852a7ca693b3a50c6028b2) (2026-10-29)


## Summary

| Area | What Wolf does | Status |
|------|----------------|--------|
| Detection | Press mode turns on when `/home/frappe/agent/config.json` has a `benches_directory` key, or when `"platform": "press"` is set | done, unit tested |
| Benches | Lists `<benches_directory>/*` that have `config.json` with a `docker_image` and a `sites/` directory | done, unit tested |
| Rediscovery | Rescans every `press.rediscover_interval` seconds (default 60), because each deploy creates a new bench directory | done, unit tested |
| Bench logs | Tails `<bench>/logs/*` (bind-mounted from the container) and `sites/<site>/logs/*`, using the same parsers as other benches | done |
| nginx | Tails `/var/log/nginx/access.log` (Press's `main` format, which logs `$host`) and maps custom domains to sites | done, unit tested |
| Redis queue | Reads `queue_length`, the RQ failed/started registries, live workers and Redis memory/evictions from `redis://127.0.0.1:<rq_port>` (bench `config.json`) | done |
| TLS expiry | Checks every site and custom domain like on other benches. On Press the certificates live on the proxy server, so the public handshake measures what customers see | done (generic) |
| Backups | **Skipped** by default (`backups.press: false`): Press takes backups through its agent and uploads them offsite; whether local copies stay in `sites/<site>/private/backups` has not been checked | off |
| Processes | **Skipped**: supervisor runs inside each bench container and Wolf never runs `docker` | — |
| MariaDB | Uses the site's `db_host`, which on Press points at the separate database server; it works only if the app server can reach it with the site user and has a `mariadb` client | unverified |
| Deploy correlation | Emits a `deployment` event for each deploy, update or migrate job in the Press agent's `jobs.sqlite3`, and an `error` event when one fails | done, unit tested |
| Fallback | If `jobs.sqlite3` cannot be read, emits a `deployment` event when a new bench appears or a site moves to another bench | done, unit tested |
| Press credentials | None are needed. Everything is read from local files. | — |
| Real Press server | **Not tested yet** (see [Unverified](#unverified)) | — |

## Server layout (from the sources)

### Press agent directory and config

- Ansible clones the agent into `/home/frappe/agent/repo`, creates a venv at
  `/home/frappe/agent/env`, and runs `agent setup config` / `agent setup database`
  from `/home/frappe/agent` ([`press/playbooks/roles/agent/tasks/main.yml#L1-L33`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/roles/agent/tasks/main.yml#L1-L33)).
- `agent setup config` writes `config.json` with `benches_directory: /home/<user>/benches`,
  `nginx_directory: /home/<user>/agent/nginx`, `redis_port: 25025`, `web_port: 25052`, …
  ([`agent/cli.py#L70-L101`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/cli.py#L70-L101)). The `Server` class reads these keys
  ([`agent/server.py#L70-L79`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L70-L79)). Wolf detects Press through this file and its
  `benches_directory` key.
- The Press playbook also creates `/home/frappe/benches`
  ([`press/playbooks/roles/bench/tasks/main.yml#L1-L7`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/roles/bench/tasks/main.yml#L1-L7)).

### Benches

- `Server.bench_init` creates `<benches_directory>/<name>/` with `logs/`, `sites/` and `config/`,
  writes the bench config to `<bench>/config.json`, renders `docker-compose.yml`, and copies
  `config/` and `sites/` out of the bench's Docker image ([`agent/server.py#L116-L148`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L116-L148)).
  The apps live **inside the image**. There is no `apps/frappe` on the host, so Wolf's
  generic check (`sites/` + `apps/frappe`) misses Press benches. A Press bench is identified
  by `config.json` with a `docker_image` key.
- `Server.benches` treats every entry of `benches_directory` as a bench
  ([`agent/server.py#L889-L894`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L889-L894)). A `Bench` requires `sites/`, `sites/common_site_config.json`
  and `config.json` ([`agent/bench.py#L45-L69`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L45-L69)).
- Bench names follow the pattern `bench-<candidate>-<server>`
  ([`press/press/doctype/bench/bench.py#L222-L236`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/press/doctype/bench/bench.py#L222-L236)). Wolf uses the directory name.
- Bench config keys come from Press's `Bench` doctype: `docker_image`, `web_port` (18000+offset),
  `socketio_port`, `rq_port` (11000+offset), `rq_cache_port`, `single_container: true`, …
  ([`press/press/doctype/bench/bench.py#L347-L384`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/press/doctype/bench/bench.py#L347-L384)).
- **Containers.** With `single_container`, `Bench.start` runs one container named after the bench.
  It mounts `<bench>/sites`, `<bench>/logs` and `<bench>/config` into
  `/home/frappe/frappe-bench/…` and publishes `-p 0.0.0.0:<rq_port>:11000`
  ([`agent/bench.py#L839-L882`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L839-L882)). Without `single_container`, the agent runs `docker stack deploy` with
  `templates/bench/docker-compose.yml.jinja2`, which bind-mounts the same `logs` and `sites`
  ([`agent/templates/bench/docker-compose.yml.jinja2`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/templates/bench/docker-compose.yml.jinja2)). In both cases the bench logs are
  ordinary files on the host. Wolf never runs `docker`.
- **Sites** are `sites/<site>/` directories with a `site_config.json`. A site's custom domains
  are listed in its `site_config.json` under `domains`. The bench nginx config maps them back
  to the site ([`agent/bench.py#L1442-L1447`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L1442-L1447),
  [`agent/templates/bench/nginx.conf.jinja2#L9-L15`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/templates/bench/nginx.conf.jinja2#L9-L15)). Wolf reads the same key, so a 502 logged
  for `erp.customer.com` is counted against the right site.
- Inside the container, supervisor writes `web.log`/`web.error.log`, `schedule.log`/`schedule.error.log`
  and `worker.log`/`worker.error.log` into `logs/`
  ([`agent/build_configs/config/supervisor.conf#L7-L29`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/build_configs/config/supervisor.conf#L7-L29)). Frappe's own loggers write
  `frappe.web.log`, `scheduler.log` and the other files there as on any bench. Wolf now also
  tails `schedule.error.log` for tracebacks, on every bench (the file is simply absent where
  nothing writes it).

### Redis (queue lengths)

`sites/common_site_config.json` holds the **in-container** address `redis://localhost:11000`
([`agent/build_configs/common_site_config.json#L1-L9`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/build_configs/common_site_config.json#L1-L9)). From the host, the queue Redis is
reachable on the published `rq_port`. The Press agent's own RQ exporter connects there
(`/benches/metrics`, [`agent/web.py#L405-L431`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/web.py#L405-L431) and [`agent/exporter.py#L8-L11`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/exporter.py#L8-L11)).
Wolf uses `redis://127.0.0.1:<rq_port>`. When a bench has no `rq_port`, Wolf skips its queue metrics.

### nginx

- The root `nginx.conf` is rendered to `/home/frappe/agent/nginx/nginx.conf`
  ([`agent/server.py#L1229-L1244`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L1229-L1244)) and symlinked to `/etc/nginx/nginx.conf`
  ([`press/playbooks/roles/agent/tasks/main.yml#L85-L97`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/roles/agent/tasks/main.yml#L85-L97)). It includes every
  `<benches_directory>/*/nginx.conf`.
- Every vhost uses one log ([`agent/templates/nginx/nginx.conf.jinja2#L45-L48`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/templates/nginx/nginx.conf.jinja2#L45-L48)):

  ```
  log_format main '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent" "$http_x_forwarded_for" "$host" $request_time';
  access_log /var/log/nginx/access.log main;
  ```

  The site name (or a custom domain) **is** in the log, as the last quoted field `"$host"`.
  Wolf's nginx parser already reads trailing quoted fields. Press mode adds the
  domain→site map. The bench templates set no per-bench `access_log`
  ([`agent/templates/bench/nginx.conf.jinja2`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/templates/bench/nginx.conf.jinja2)). Behind a Press proxy, `$remote_addr` is
  the proxy (`set_real_ip_from <proxy_ip>`, same file L59-L62).
- Proxy servers (`n*` hosts) use the same root template, but they have no benches. To count
  5xx there, add `/var/log/nginx/access.log` as an `extra_logs` entry with `"parser": "nginx"`.

### Job history (deploys, updates, migrations)

- Every Press agent job is stored in the SQLite database `jobs.sqlite3`, relative to the
  agent's working directory (`/home/frappe/agent`, the supervisor `directory=`). It runs in
  WAL mode ([`agent/job.py#L45-L54`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/job.py#L45-L54),
  [`agent/templates/agent/supervisor.conf.jinja2#L1-L36`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/templates/agent/supervisor.conf.jinja2#L1-L36)). The tables come from
  peewee models ([`agent/job.py#L301-L344`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/job.py#L301-L344), created by `agent setup database`,
  [`agent/cli.py#L171-L175`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/cli.py#L171-L175)):
  - `jobmodel(id, name, status ∈ Pending/Running/Success/Failure, agent_job_id, data, enqueue, start, end, duration)`
  - `stepmodel(id, name, job_id, status, data, start, end, duration)`
- `agent_job_id` is the `X-Agent-Job-Id` header Press sends, which is the name of the
  **Agent Job** document in Press ([`press/agent.py#L960-L966`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/agent.py#L960-L966)). Wolf copies it into the
  event payload so you can open the same job in the Press dashboard.
- At enqueue time, `data` is `{"function", "args", "kwargs"}` ([`agent/job.py#L124-L138`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/job.py#L124-L138)).
  When the job ends, it is **overwritten** with the result or `{"traceback": …}`
  ([`agent/job.py#L78-L91`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/job.py#L78-L91)). Step `data` keeps each step's command result, including the
  `docker exec … <bench> bench --site <site> …` command line
  ([`agent/base.py#L65-L111`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/base.py#L65-L111), [`agent/bench.py#L177-L200`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L177-L200), [`agent/site.py#L61-L62`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/site.py#L61-L62)).
- Timestamps are naive `datetime.now()`, i.e. server local time. Wolf converts them to UTC
  as local time.

Wolf treats these job names as deployments. The names and argument orders come from the
`@job` decorators:

| Press agent job | Source | bench / site taken from |
|-----------------|--------|--------------------------|
| New Bench | [`agent/server.py#L157-L174`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L157-L174) | `args[0]` (bench) |
| Update Site Pull, Update Site Migrate | [`agent/server.py#L558-L600`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L558-L600) | `args[0]` site, `args[2]` target bench |
| Recover Failed Site Migrate / Pull, Move Site to Bench | [`agent/server.py#L658-L705`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L658-L705) | same |
| Recover Failed Site Update | [`agent/server.py#L729-L733`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L729-L733) | `args[0]` site, `args[1]` bench |
| Update Bench In Place | [`agent/bench.py#L1086-L1092`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L1086-L1092) | `args[0]` sites; bench from steps |
| Patch App, Bench Restart, Rebuild Bench Assets | [`agent/bench.py#L592-L602`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L592-L602), [`agent/bench.py#L1026-L1032`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L1026-L1032) | steps |
| Migrate Site, Install / Uninstall App on Site | [`agent/site.py#L282-L322`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/site.py#L282-L322) | steps |

`Bench` and `Site` methods carry no bench or site name in their arguments, because `self`
is not stored. For those jobs, Wolf searches the job and step `data` for a known bench name
and for `--site <name>`.

## How deployment events are produced

- Wolf opens `jobs.sqlite3` with `file:…?mode=ro` (read-only, needs the agent's `-wal`/`-shm`
  files to be readable). If that fails, it retries with `immutable=1`. Wolf never writes to
  the database.
- **One `deployment` event per job**, sent as soon as the job has started. Its timestamp is
  the job's `start`, so Wolf's "502 spike after deployment" rule links incidents that happen
  *during* a long migrate. If the bench cannot be determined yet, the event waits up to
  2 minutes for a step to name it, then ships host-scoped. Wolf's incident correlation also
  matches host-scoped deployments (`scope_clause` in `frappe_wolf/wolf/events.py`).
  Fields: `category: "press"`, `source: "press-agent"`, and
  `payload.press_job / agent_job_id / local_job_id / sites`.
- **One `error` event (`category: "press_job_failed"`, warning)** when such a job ends in
  `Failure`. Its payload holds the failed step names and the tail of the traceback/output.
- **Deduplication.** Emitted job ids and a floor id are saved in
  `<spool_dir>/press-jobs.json`. Restarts never emit an event twice. On the first run,
  history is skipped and only jobs still Pending/Running are picked up. A job stuck in
  Pending/Running for more than 24 h (e.g. a crashed RQ worker) is dropped, so it cannot
  block progress.
- **Fallback without database access**: `<spool_dir>/press-benches.json` keeps a
  bench→site snapshot. A new bench directory, or a site whose directory moved to another
  bench (`Server.move_site`, [`agent/server.py#L735-L750`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L735-L750)), produces a `deployment` event
  stamped with the time it was noticed.

Rediscovery rebuilds the log sources when the set of benches or sites changes. Logs of a
bench created while Wolf runs are read from the start, because they are new files.
A moved site's logs move with its directory, so they are read from the end to avoid replaying them.

## Configuration

Press mode is auto-detected. To force it, or to change the paths:

```json
{
  "platform": "press",
  "press": {
    "agent_dir": "/home/frappe/agent",
    "nginx_access_log": "/var/log/nginx/access.log",
    "rediscover_interval": 60,
    "deployments": true
  }
}
```

`"platform": "generic"` disables Press handling. `wolf-agent discover --platform press` and
`wolf-agent register --platform press` show or record the Press layout. In Press mode the
`benches` list in the config is refreshed from disk at start and on every rediscovery.

Permissions: the `wolf-agent` user must be able to read `/home/frappe/benches/*/logs`,
`sites/*/site_config.json`, `/home/frappe/agent/config.json`, `jobs.sqlite3` (plus `-wal`/`-shm`)
and `/var/log/nginx/access.log`. `install.sh` already adds the user to the `frappe` and `adm`
groups. Whether Press's directory modes allow group read has **not been checked** on a real server.

## Wolf next to Press's own monitoring

Press already ships a monitoring stack. Wolf does not replace it and does not touch it:

- **Monitor server** (`monitor.yml`): Prometheus, Alertmanager, Blackbox exporter and Grafana
  ([`press/playbooks/monitor.yml`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/monitor.yml)). Prometheus probes sites, domains and TLS with
  blackbox `http_2xx` and scrapes the Press-managed servers
  ([`press/playbooks/roles/prometheus/templates/prometheus.yml`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/roles/prometheus/templates/prometheus.yml)). Press pushes
  alert rules and routes to it through the agent's `Monitor` class
  ([`agent/monitor.py#L11-L40`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/monitor.py#L11-L40)). These come from Press doctypes such as *Prometheus Alert Rule*,
  and the Alertmanager webhook lands in *Alertmanager Webhook Log*.
  Press's *Incident* doctype ([`press/press/doctype/incident/incident.py`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/press/doctype/incident/incident.py)) escalates
  outages by phone (Twilio) to Frappe staff and customers.
- **App servers** (`server.yml`): node_exporter, cAdvisor, statsd_exporter (the bench
  config sets `statsd_host`), filebeat and fail2ban, among other roles
  ([`press/playbooks/server.yml`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/server.yml)). Filebeat ships `/var/log/*.log`, the nginx module and
  each bench's `logs/monitor.json.log` (Frappe's request/job monitor, enabled through
  `"monitor": true`) to a central Elasticsearch/Kibana log server
  ([`press/playbooks/roles/filebeat/templates/inputs.d/monitor.yml`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/roles/filebeat/templates/inputs.d/monitor.yml),
  [`press/playbooks/roles/filebeat/templates/modules.d/nginx.yml`](https://github.com/frappe/press/blob/aedd7d1237e49153e4852a7ca693b3a50c6028b2/press/playbooks/roles/filebeat/templates/modules.d/nginx.yml)).
  The agent also moves `monitor.json.log` aside for Press's analytics
  (`Bench.fetch_monitor_data`, [`agent/bench.py#L300-L330`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/bench.py#L300-L330)). Wolf does not tail that file.
- The agent exposes RQ metrics per bench at `/benches/metrics` ([`agent/web.py#L405-L431`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/web.py#L405-L431)).

Wolf adds what that stack does not provide on a self-hosted Press install: Frappe-level
tracebacks, failed scheduled jobs and worker crashes read from bench logs, 5xx/502 per
site, SSH signals, and **incidents correlated with the Press job that caused them**. All of
it lives on your own Wolf site, without access to the Press monitor server.
The two stacks overlap on host metrics (node_exporter vs Wolf's `/proc` sampling) and on
queue lengths. Wolf's are light, and you can turn them off with `collect_host_metrics: false`.

## Limitations

- Only app servers with benches are supported directly. Proxy and database servers have no
  bench, so add their logs through `extra_logs`.
- Bench and site detection for `Bench`/`Site` jobs is heuristic (step command lines). A job
  that finishes without any step naming its bench is reported host-scoped.
- The filesystem fallback cannot tell a deploy from a manual move, and it has no failure
  signal.
- Jobs are matched by display name. If Press renames a job, it is no longer recognised;
  add the new name to `DEPLOY_JOBS` in `agent/wolf_agent/press.py`.
- Shared-FS benches (`Run Benches on Shared FS`, [`agent/server.py#L312-L370`](https://github.com/frappe/agent/blob/64b9812a8e4dc82188d3cbf3053dd4f2eb5f4f4a/agent/server.py#L312-L370)) change
  `benches_directory`. Wolf rereads it from the agent config on every rediscovery, but this
  path has not been tested.
- Frappe Cloud's hosted servers are not open to customers. This is for **self-hosted Press**
  deployments, where you control the server.

## Unverified

Everything above comes from reading the two repositories. These points still need a real Press server:

1. File and directory modes under `/home/frappe` and on `jobs.sqlite3*`, i.e. whether `mode=ro` works for a
   group member or `immutable=1` is needed.
2. Actual contents of `jobmodel.data` / `stepmodel.data` on a production agent (job names, the
   command strings Wolf searches for bench names).
3. That `/var/log/nginx/access.log` lines carry the site in `$host` behind the Press proxy for every
   request type (including the agent's own vhost on 443).
4. That `rq_port` is published on every bench (older benches may predate it).
