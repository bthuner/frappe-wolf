Frappe Wolf Observability — MVP
One-line summary

First-class observability for Frappe benches, ERPNext sites, Linux hosts, and internal services — owned by Dokos, not Datadog.
Vision

Give Dokos and self-hosted Frappe operators a unified, privacy-first observability plane that:

    Treats Frappe, ERPNext, Pilot benches, Linux, and internal services as first-class citizens.

    Surfaces 5xx/502 windows, cron/scheduler failures, and security anomalies before customers notice.

    Stays vendor-free by using open standards (OpenTelemetry, syslog, JSON logs) and self-hosted storage.

    Is simple enough to adopt in one afternoon on a new Dokos stack.

MVP goals

    Detect and alert on:

        HTTP 5xx/502 spikes per site and per bench.

        Failed scheduler/cron jobs (including overnight jobs like 2AM tasks).

        Worker crashes and queue backlogs.

        Suspicious access patterns (multiple failed logins, admin login from unusual IP, unexpected new users with high roles).

    Provide a single UI inside Frappe ("Frappe Wolf") that shows:

        Timeline of incidents for a given site/bench.

        Error snapshots with context (route, user, traceback, deployment window).

        Host metrics for core resources (CPU, RAM, disk, load) with simple thresholds.

    Keep configuration dead-simple:

        YAML or DocType-driven alert rules ("when X > Y for Z minutes, send alert").

        Sensible presets for Dokos (e.g., 502 spike after deployment; cron failures between 1–4AM).

        One command to install the agent on a host and register it with a Wolf site.

Non-goals (MVP)

    Full Datadog replacement across all products.

    LLM observability, data observability, or complex SLO dashboards.

    Deep UX analytics or RUM.

The MVP focuses on:

    Operational safety for Dokos/Frappe: errors, cron, workers, hosts.

    Basic security signals.

Architecture sketch
Components

    Frappe Wolf App (server-side):

        Frappe app installed on Dokos/Frappe sites.

        Stores events, metrics, alerts, configurations in DocTypes.

        Exposes APIs for agents to send telemetry.

        Provides UI for timelines, dashboards, and alert configuration.

    Wolf Agent (host-side):

        Lightweight daemon (Rust or Python) running on Linux hosts.

        Collects:

            System metrics (CPU, RAM, disk, load).

            Logs from:

                Frappe bench logs (bench.log, worker.log, scheduler.log, frappe.web.log).

                Nginx/access logs for 5xx/502.

                System logs (journalctl, /var/log/auth.log) for security signals.

            Optional OpenTelemetry traces from Frappe apps.

        Ships normalized events/metrics to the Wolf App over HTTPS.

    Pilot integration:

        Use bench.toml and Pilot Admin API to auto-discover benches, sites, processes, and log paths.

        Attach Wolf configuration to benches in Pilot ("enable Wolf for this bench" toggle).

Data model (DocTypes)

    Wolf Event:

        type: error, cron_failure, worker_failure, security, host_metric_alert.

        site, bench, host.

        timestamp.

        severity: info, warning, critical.

        payload (JSON with details).

    Wolf Metric Sample:

        host, site, bench.

        metric_name (e.g., cpu_usage, load_1m, disk_used_percent).

        value.

        timestamp.

    Wolf Alert Rule:

        name, enabled.

        signal_type: metric, event.

        query / filter DSL (site, bench, host, type).

        condition (e.g., value > 0 for 5 minutes, count >= 5 in 10 minutes).

        channels: email, webhook, Raven channel, etc.

    Wolf Incident:

        Aggregation of events that share a root cause.

        status: open, acknowledged, resolved.

        Links to deployments (manual or Pilot events).

Key flows
1. Deployment → 502 → cron failure at 2AM

    Admin runs deployment on a Dokos/Pilot bench.

    Pilot exposes task logs and status.

    Nginx starts returning 502 for some routes; nginx/access.log and frappe.web.log capture 502s.

    Nightly cron at 2AM fails due to migration issue; scheduler.log and Frappe Error Log show the stack trace.

    Wolf Agent:

        Tails nginx and Frappe logs, counts 502 and 5xx responses per site/route.

        Parses scheduler logs for failed jobs.

        Sends events to Wolf App.

    Wolf App:

        Groups events into an Incident.

        Fires alerts via email/Raven if 502s exceed threshold or cron failures occur.

The team wakes up with a clear incident timeline, not a surprise email from the customer at 9AM.
2. Security: suspicious access

    Linux host logs multiple failed SSH logins, or auth.log shows brute-force attempts.

    Frappe site logs repeated failed logins or an admin login from a new country/IP.

    Wolf Agent ships security events to Wolf App.

    Wolf App raises a security alert and shows correlation with infrastructure metrics.

Technical feasibility notes
Frappe side

    Frappe already has:

        Error Snapshots for 500+ responses.

        Desk logs (Error Log, Activity Log).

        Bench/server logs (bench.log, scheduler.log, worker.log, frappe.web.log).

        Configurable logging via enable_frappe_logger and site-level logs.

    Wolf App can:

        Reuse frappe.log_error() to create structured events when app code detects anomalies.

        Add server scripts or hooks to emit events on key operations (deployments, migrations, scheduler failures).

        Define DocTypes for Wolf Event, Metric Sample, Alert Rule, Incident.

[Logging docs reference: Frappe logging separates Desk logs (Error Log, etc.) from server logs at bench/site level, including bench.log, scheduler.log, and worker.log.]
Pilot integration

    Pilot already exposes:

        Admin UI and Admin API for benches, sites, processes, logs, tasks, database tools, updates, and settings.

        bench.toml as the source of truth for apps, database, Redis, workers, nginx, S3, and monitoring settings.

    Wolf can:

        Read bench.toml to discover log paths and process definitions.

        Use Pilot’s Admin API for status (e.g., deployment tasks) and annotate incidents.

Press / Frappe Cloud alignment

    Press implements full-service cloud hosting for the Frappe stack, with dashboards for apps, servers, sites, billing, backups, and updates.

    Long term, Wolf should be compatible with Press deployments, but the MVP can start with Dokos-managed benches and vanilla Pilot setups.

Agent feasibility (Rust vs Python)

    Rust:

        Pros: low overhead, safe concurrency, good for long-running daemon, native system metrics.

        Cons: slower iteration for Frappe devs, fewer contributors comfortable with Rust.

    Python:

        Pros: matches Frappe ecosystem, easier contribution and packaging, reuses existing libraries.

        Cons: slightly higher resource cost; needs care for async IO when tailing logs.

MVP recommendation: start with Python agent (fastest path for Dokos and Frappe contributors), keep protocol and packaging generic enough to allow a Rust agent later.
Inspiration from SigNoz & Datadog

    SigNoz:

        Unified observability with logs, metrics, traces, and exceptions in one platform.

        OpenTelemetry-native instrumentation; ClickHouse-backed storage for fast queries and cost-effective retention.

        Alerting on any telemetry signal with anomaly detection and Apdex-based detection.

    Datadog:

        Deep correlations between metrics, logs, traces, security events, and jobs, with ML-powered anomaly detection.

        Observability Pipelines for in-flight filtering, sampling, and routing of logs, metrics, traces.

        Rich alerting and incident views for job failures, long-running tasks, and data observability.

Wolf MVP will not copy all features but will:

    Correlate Frappe app logs, bench/server logs, and host metrics in one Frappe-native UI.

    Provide simple alerting rules inspired by SigNoz/Datadog, but tailored to Frappe and Dokos.

    Keep data in Dokos-controlled storage and avoid opaque usage-based pricing.

MVP backlog (high-level)

    Frappe Wolf App

        Define DocTypes: Wolf Event, Wolf Metric Sample, Wolf Alert Rule, Wolf Incident.

        Build basic UI: list views, incident timeline, per-site/bench overview.

        Implement alert delivery: email + Raven channel webhook.

    Wolf Agent (Python)

        CLI to register agent with Wolf site (API key/secret).

        Collect host metrics (CPU, RAM, disk, load) using standard libraries.

        Tail key logs and parse events:

            nginx/access logs for 5xx/502.

            frappe.web.log.

            scheduler.log, worker.log, bench.log.

            /var/log/auth.log and/or journalctl for security.

        Send batched telemetry to Wolf App periodically.

    Pilot integration

        Read bench.toml to auto-configure log paths.

        Optional: small Pilot plugin or config section to enable Wolf.

    Preset alert rules for Dokos

        502 spike after deployment window.

        Cron/scheduler failure between 01:00–04:00.

        Worker queue length above threshold.

        Multiple failed logins or admin login from new IP.

First demo scenario

    Single Dokos bench with ERPNext and a few sites.

    Install Wolf App on one site.

    Deploy Python agent on the host.

    Trigger:

        Simulated deployment + nginx reload causing a short 502 window.

        A failing cron job at 2AM.

    Show:

        Incident timeline in Wolf.

        Alerts emitted to email/Raven.
