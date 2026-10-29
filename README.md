<p align="center"><img src="docs/logo.png" width="96" alt="Frappe Wolf"></p>

# Frappe Wolf

[![CI](https://github.com/bthuner/frappe-wolf/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/bthuner/frappe-wolf/actions/workflows/ci.yml)

Self-hosted observability for Frappe benches, ERPNext/Dokos sites and Linux hosts.
It catches 502 windows, failed nightly crons, worker crashes and suspicious logins
before customers notice, and keeps all data on your own Frappe site.

- **Wolf App** (`frappe_wolf/`): a Frappe app that stores events, metrics, rules and incidents as
  DocTypes, evaluates alert rules every minute, and shows a timeline in Desk.
- **Wolf Agent** (`agent/`): a stdlib-only Python daemon on each host. It tails nginx, bench and
  auth logs, reads `/proc`, and ships batches over HTTPS.
- Wire protocol: [`docs/protocol.md`](docs/protocol.md). The design story is in the [making-of](https://bthuner.github.io/frappe-wolf/).

## Install the app

```bash
cd ~/frappe-bench
bench get-app https://github.com/bthuner/frappe-wolf
bench --site wolf.example.com install-app frappe_wolf
bench build --app frappe_wolf   # links the static assets
```

Installing creates the roles **Wolf Manager** and **Wolf User**, a random registration token and
the Dokos preset rules. Open **Wolf Settings → Agent install command** to copy the one-liner.

## Install the agent on a host

```bash
curl -fsSL https://raw.githubusercontent.com/bthuner/frappe-wolf/main/agent/install.sh \
  | sudo WOLF_URL=https://wolf.example.com WOLF_TOKEN=<registration token> bash
```

The agent finds benches automatically (via `bench.toml`, or `sites/` + `apps/frappe`). On
servers managed by a self-hosted Press (Frappe Cloud stack), it reads the Press agent's
layout, and it turns Press deploy/update/migrate jobs into deployment events. See
[`docs/press.md`](docs/press.md). See [`agent/README.md`](agent/README.md) for
configuration, `wolf-agent check` and `wolf-agent mark-deployment`.

## Alert rules

A rule reads like the brief: *when X > Y for Z minutes, send alert*.

| Field | Meaning |
|---|---|
| Signal | `metric` (aggregate Wolf Metric Samples) or `event` (count Wolf Events) |
| Aggregation | `avg`, `min`, `max`, `sum`, `count`, `last`. Use `min` for "stayed above for Z minutes" and `sum` for counters such as `http_5xx` |
| Window | look-back in minutes |
| Group by | `host`, `bench`, `site`, `host_label` (for example per disk mount) or `none`: one incident per group |
| Active from/to | only evaluate in this window. It may wrap midnight (`04:00 → 01:00`) |
| Require recent deployment | only fire if a deployment hit the same bench/site/host recently |
| Cooldown | re-fire into the just-resolved incident instead of opening a new one |
| Fire / resolve after evaluations | consecutive breached (or clear) one-minute evaluations before an incident opens (or auto-resolves). Default 1 |
| Separate resolve threshold | hysteresis: e.g. fire above 90, resolve only once at or below 80 |
| Channels | Email, Webhook (Slack/Mattermost-compatible JSON), Raven channel. Each channel can use an **hourly digest**: info/warning alerts are batched into one message per destination; critical alerts are always immediate |
| Notify on resolve | also tell the channels when the incident auto-resolves (default on) |
| Escalate after minutes | if still Open (not acknowledged) this long after the first alert, notify the escalation channels once |

Alerts for unresolved incidents carry signed **Acknowledge / Resolve** links (valid 24 h, see Wolf
Settings → Action Link Hours). Opening a link shows a confirmation page; only its button acts, so
mail scanners cannot acknowledge for you.

**Maintenance windows**: a **Wolf Silence** (host, bench, site and/or rule, start and end) keeps
incidents opening, so the timeline stays complete, but holds back their notifications. Incidents
still open when the window ends are notified then. Create one in Desk, through
`/api/resource/Wolf Silence`, or with the deployment marker:
`wolf-agent mark-deployment --bench main --silence 15m`.

Presets installed on install/migrate (edit or disable freely, they are never overwritten):
502 spike after deployment · 5xx spike · Nightly cron failure (01:00–04:00) · Scheduler failure ·
Worker crash · Worker queue backlog · SSH brute force · Repeated failed Frappe logins ·
Admin login from new IP · Privileged role granted · Disk almost full · High CPU · High memory ·
High load · Agent silent · Scheduler stalled.

Presets have no channels: they open incidents but notify nobody until you add a channel.

## Signals captured on the Wolf site itself

Without any agent, the site running Frappe Wolf records failed Scheduled Job Logs (`cron_failure`),
Error Logs, failed desk logins, privileged logins from a new IP, privileged role grants and each
`bench migrate` (as a deployment). Each signal can be turned off in Wolf Settings.

## Every site on a bench

Some signals can only be seen from inside a site. Frappe logs "Scheduled Job Failed" at INFO,
which the default logger level drops, and it often writes no Scheduled Job Log, so the agent
cannot see a failing cron on a site from `scheduler.log`. Install `frappe_wolf` on every site
and keep one of them as the **central** Wolf site. Set the others to **satellite** mode:

```bash
bench --site b.example.com install-app frappe_wolf
bench --site b.example.com execute frappe_wolf.satellite.register \
  --kwargs '{"url": "https://wolf.example.com", "token": "<central registration token>"}'
# add "verify_tls": 0 for a self-signed central site
```

The same is available in Desk: **Wolf Settings → Register with central site**.

A satellite:

- keeps capturing its local signals (failed scheduled jobs, Error Logs, logins, privileged role
  grants, `bench migrate`), with the same Wolf Settings switches;
- stores them in **Wolf Outbox** and pushes them every minute to the central site through the
  agent ingest API, with the same host/bench/site scope, so central rules and incidents see them
  like any other event;
- retries with backoff (1, 2, 4, 8, then every 15 minutes) while the central site is unreachable,
  keeps at most 10,000 pending events (drops the oldest) and shows the forwarding status on Wolf
  Overview and Wolf Settings (**Forward now** pushes immediately);
- evaluates no rules, opens no incidents and stores no Wolf Events.

On the central site the satellite is a **Wolf Agent** with kind `satellite`, named after its
site. Its push runs every minute even when there is nothing to send, so the **Agent silent**
preset also fires when a satellite's scheduler stops. Disable the Wolf Agent to stop accepting
its events. Setting Mode back to `central` makes the site a standalone Wolf again.

## Development

```bash
python3 -m unittest discover -s tests -t .           # app pure-python tests
python3 -m unittest discover -s agent/tests          # agent tests
```

### CI checks

Every pull request and every push to `main` runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml):
lint (ruff, shellcheck), the app tests on Python 3.12, the agent tests on Python 3.9 and 3.14, and a
bench integration job on MariaDB 10.6 and on Postgres 16. To run the same checks locally:

```bash
pip install ruff==0.17.3                             # ruff rules are in pyproject.toml
ruff check .
shellcheck agent/install.sh ci/integration.sh
python3 -m unittest discover -s tests -t .
python3.9 -m unittest discover -s agent/tests        # the agent must keep working on 3.9
```

The integration job installs this checkout into the public Dokos image, creates a site and runs
[`ci/db_smoke.py`](ci/db_smoke.py), which exercises every Wolf SQL path. With Docker:

```bash
docker network create wolf-ci
docker run -d --name db --network wolf-ci -e MARIADB_ROOT_PASSWORD=admin mariadb:10.6 \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
docker run -d --name redis --network wolf-ci redis:6.2-alpine
docker run --rm --network wolf-ci --entrypoint bash -v "$PWD:/src:ro" \
  registry.gitlab.com/dokos/dokos:latest /src/ci/integration.sh mariadb
```

For Postgres, start `postgres:16` as `db` with `-e POSTGRES_PASSWORD=admin` and pass `postgres`.
On an existing bench, run `../env/bin/python /path/to/ci/db_smoke.py <site>` from `sites/`.

## Status

MVP, tested on a Dokos (Dodock 5.18) bench, on MariaDB 10.6 and on Postgres 17
(see `ci/db_smoke.py`).

## License

MIT
