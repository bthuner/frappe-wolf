<p align="center"><img src="docs/logo.png" width="96" alt="Frappe Wolf"></p>

# Frappe Wolf

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
bench build --app frappe_wolf
```

Installing creates the roles **Wolf Manager** and **Wolf User**, a random registration token and
the Dokos preset rules. Open **Wolf Settings → Agent install command** to copy the one-liner.

## Install the agent on a host

```bash
curl -fsSL https://raw.githubusercontent.com/bthuner/frappe-wolf/main/agent/install.sh \
  | sudo WOLF_URL=https://wolf.example.com WOLF_TOKEN=<registration token> bash
```

The agent finds benches automatically (via `bench.toml`, or `sites/` + `apps/frappe`). See
[`agent/README.md`](agent/README.md) for configuration, `wolf-agent check` and
`wolf-agent mark-deployment`.

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
| Channels | Email, Webhook (Slack/Mattermost-compatible JSON), Raven channel |

Presets installed on install/migrate (edit or disable freely, they are never overwritten):
502 spike after deployment · 5xx spike · Nightly cron failure (01:00–04:00) · Scheduler failure ·
Worker crash · Worker queue backlog · SSH brute force · Repeated failed Frappe logins ·
Admin login from new IP · Privileged role granted · Disk almost full · High CPU · High memory ·
High load · Agent silent.

Presets have no channels: they open incidents but notify nobody until you add a channel.

## Signals captured on the Wolf site itself

Without any agent, the site running Frappe Wolf records failed Scheduled Job Logs (`cron_failure`),
Error Logs, failed desk logins, privileged logins from a new IP, privileged role grants and each
`bench migrate` (as a deployment). Each signal can be turned off in Wolf Settings.

## Development

```bash
python3 -m unittest discover -s tests -t .           # app pure-python tests
python3 -m unittest discover -s agent/tests          # agent tests
```

## Status

MVP. Not yet installed on a production bench. Some SQL is MariaDB-specific (`unix_timestamp`,
multi-table `DELETE`).

## License

MIT
