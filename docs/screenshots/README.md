# Wolf UI screenshots

Taken on 2026-11-01 on the demo bench ([`dev/demo`](../../dev/demo/README.md): Dodock 5.18.8,
MariaDB 10.6, Frappe's socket.io server behind nginx), after `scenario.py`. The data is the
scenario's, not a production site: one deployment, 60 s of backend downtime (180 responses 502),
and a scheduled job failing at night. Chromium at 1440 × 900, light and dark themes as the system
asks; times are the site's (Australia/Brisbane, the night zone the scenario picked).

| View | Light | Dark |
|---|---|---|
| Overview, last hour: exact counts, the 502 window after the deployment | [light](overview-light.jpg) | [dark](overview-dark.jpg) |
| Incident "502 spike after deployment", open: the preceding change, evidence and context apart | [light](incident-light.jpg) | [dark](incident-dark.jpg) |
| Incident "Nightly cron failure", owner set, acknowledged, commented and resolved in the Wolf UI | [light](incident-resolved-light.jpg) | [dark](incident-resolved-dark.jpg) |
| Metrics: `http_502` summed per minute | [light](metrics-light.jpg) | [dark](metrics-dark.jpg) |
| Monitoring: rules, scheduler pace, queue, rollups, agent | [light](monitoring-light.jpg) | [dark](monitoring-dark.jpg) |
| Security: score with unknown checks shown as unknown, journal anchor | [light](security-light.jpg) | [dark](security-dark.jpg) |

The views that update live (overview, incidents, incident, events, dashboards) said **Live**: their
socket was accepted by Frappe's socket.io server, and a change made on the bench reached the open
incident page in about a second. Metrics, Monitoring and Security refresh on demand.
