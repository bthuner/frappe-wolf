# Local demo: the 2AM scenario from CLAUDE.md

One Dokos bench with `frappe_wolf` on the site `wolf.localhost`, plus:

- an nginx front that logs the site name;
- the Wolf agent, tailing the bench and nginx logs and Redis;
- a webhook sink that prints every alert.

Everything runs locally; nothing leaves your machine.

```bash
cd dev/demo
# Any Dokos image works as base; reusing a local one avoids a multi-GB pull.
WOLF_DEMO_BASE=registry.gitlab.com/dokos/dokos:latest docker compose up -d --build
docker compose logs -f agent          # wait for "wolf-agent 0.1.0 started"
python3 scenario.py                   # ~5 minutes
docker compose logs catcher           # the alerts Wolf sent
```

Desk: <http://localhost:8099/app/wolf-overview>, login `Administrator` / `admin`.

## What `scenario.py` does

1. Sets the site time zone to one where it is currently between 01:00 and 04:00. This moves
   the site clock "into the night" without faking the system clock. Events stored earlier in
   the old zone will look shifted.
2. Adds a webhook channel (`http://catcher:9999/`) to the 502 and scheduler preset rules.
3. Marks a deployment with `wolf-agent mark-deployment`.
4. Stops the backend for 60 s while sending traffic, so nginx answers 502.
5. Starts the backend again and reloads nginx.
6. Adds a Server Script scheduled job that fails every minute ("Nightly ledger sync"), lets it
   fail, then disables it.
7. Prints the incidents.

Expected:

- **502 spike after deployment**: critical, with the deployment linked.
- **Nightly cron failure (01:00-04:00)**: critical. It is reported by the site itself, through
  Wolf's `Scheduled Job Type` override. The agent's `scheduler.log` parser stays quiet here:
  Frappe logs "Scheduled Job Failed" at INFO, below the default logger level.
- One webhook per new incident.

Presets like **Disk almost full** may fire too, because the agent measures the machine
running Docker. **Agent silent** fires once as an artefact of step 1: `last_seen` was stored
in the old time zone, so the agent looks hours stale for one check.

![Incident timeline after the scenario](incident-timeline.jpg)

## Email and Raven channels

```bash
WOLF_DEMO_RAVEN=1 docker compose up -d --build     # adds Raven (backend only) to the image
docker compose exec backend bench --site wolf.localhost install-app raven
python3 channels.py
```

`channels.py` sets up the following on the **Scheduler failure** rule, then makes a job fail:

- an outgoing Email Account pointed at Mailpit;
- a Raven workspace and a `wolf-alerts` channel;
- Email, Raven and Webhook channels on the rule.

Mail shows up at <http://localhost:8025> within one email-queue flush, a few minutes at most.
Raven is installed without `blurhash-python`, which has no Python 3.14 wheel; that only
affects image previews.

Reset: `docker compose down -v`.
