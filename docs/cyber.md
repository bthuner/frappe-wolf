# Wolf cyber: benchmark and roadmap

Frappe Wolf started as an observability tool (5xx windows, failed crons, workers, hosts) with a
few security signals. This page compares it with a French sovereign SOC offer, SYLink / UniSOC,
says what Wolf should take from it, and sets out the roadmap that gives Wolf a strong security
focus. The first slice is built (see [What is built](#what-is-built)).

## 1. What SYLink and UniSOC offer (November 2026)

The facts below come from the public sites, [sylink.fr](https://sylink.fr) and
[unisoc.fr](https://unisoc.fr). Figures such as accuracy rates and flows per second are the
vendor's own claims; we have not checked them.

**SYLink** is a French cybersecurity vendor based in Clermont-Ferrand. It sells products and
services:

- **Network**: the SYLink Box (firewall, IDS/IPS, DPI probe, Wi-Fi segmentation).
- **Endpoints**: EDR.
- **Supervision**: a French 24/7 SOC and NOC, CERT/CSIRT, dark-web monitoring and
  "sovereign" threat intelligence.
- **Deception**: honeypots (SSH, RDP, SMB, AD).
- **Offensive**: audit and pentest, driven by an on-premise 27B AI model.
- **Other**: awareness training and an MSP/MSSP programme (white-label multi-tenant console).

Customers range from sole traders to government bodies and the defence sector (OIV/OSE).
Deployment is on-premise, SaaS hosted in France, or hybrid, including air-gapped setups. SYLink
lists ISO 27001, PASSI and HDS as certifications, and says it is aligned with NIS2, the LPM and
SecNumCloud.

**UniSOC** is SYLink's AI-driven SOC platform. Its engine, "SYLink AI", correlates events from 16
modules:

| UniSOC module | What it does |
|---|---|
| DPI probe | full packet capture L2–L7, exfiltration / C2 detection |
| EDR agent, mobile XDR | Windows/macOS/Linux (ETW, YARA, Sigma, DLP); iOS/Android |
| HoneyPot | 14 decoy services in the LAN |
| Continuous pentest | 7 automated modules |
| AI firewall, Safe Browsing, WAF | L7 filtering/IPS, DNS/URL filtering, Coraza + OWASP CRS |
| AD agents, hypervisor plugin | Active Directory, M365, Exchange; read-only hypervisor APIs |
| Mapping & IT inventory | assets in a NetBox model |
| Attack surface | external exposure, DNS/DMARC gaps |
| Phishing | AI-generated simulation campaigns |
| AI assistant | local model; it proposes a remediation and a human validates it |
| NIS2 tracking | 14 axes, Article 23 reporting deadlines, evidence from telemetry |
| Cyber score | continuous posture score (AD, M365, exposure, third parties), MITRE ATT&CK |

UniSOC also claims HMAC-signed audit records, more than 1,500 connectors (third-party firewalls,
SIEMs, EDRs, M365, LDAP), French hosting (SecNumCloud/HDS through Unitel Group), and a
human-in-the-loop response: the AI proposes an action and the analyst approves it.

## 2. Where Wolf stands

UniSOC covers an organisation's whole IT estate: network, endpoints, identity and people. Wolf
should not try to match that breadth. A DPI probe, a Windows EDR, phishing simulation or AI
pentesting are separate products, and the Frappe operators who run Wolf already buy them
(or should).

What none of those tools see is the **inside of a Frappe bench**. They do not know DocTypes,
roles and DocPerms, API keys, Server Scripts and the System Console, `site_config.json`, the
bench Redis, Frappe's scheduler, or who exported 40,000 customers from a report. That is Wolf's
ground, and nobody else covers it well. The goal is:

> Wolf is the security layer of Frappe/ERPNext/Dokos: it detects attacks on the app and its
> bench, scores how exposed each site is, helps meet NIS2/GDPR reporting duties, and forwards
> everything to the organisation's SOC (UniSOC, Wazuh, any SIEM) in a standard form.

What Wolf takes from UniSOC, adapted to a Frappe app:

| UniSOC idea | Wolf, Frappe-native |
|---|---|
| MITRE ATT&CK everywhere | every security event carries ATT&CK technique IDs (**built**) |
| WAF / web attack detection | probes and injections read from nginx access logs, alert when a secret file is served (**built**) |
| EDR "suspicious behaviour" | auth-log signals: login after brute force, sudo abuse, new accounts, admin groups (**built**); then file integrity on the bench |
| HoneyPot | Frappe honeytokens: decoy user, API key and route (a single touch is critical) |
| Attack surface | bench-specific exposure: Redis/MariaDB listening publicly, `developer_mode` in production, open `/api` to guests, SPF/DMARC of the site's mail domain |
| Cyber score | a security posture score per site, from checks Wolf can prove |
| Threat intelligence | offline IP reputation lists (FireHOL, abuse.ch, Spamhaus DROP), optional CrowdSec |
| Human-validated response | signed "Block IP / Disable user / Revoke sessions" actions, applied by the agent or the site after an analyst approves them |
| NIS2 Art. 23 tracking | NIS2 and GDPR Art. 33 deadlines on incidents (24 h / 72 h / 1 month) and an exportable report |
| HMAC-signed audit | a hash-chained, tamper-evident journal of security events and incident actions |
| 1,500 connectors | standard output instead: syslog/CEF and OCSF export, so any SOC can ingest Wolf |

What Wolf does not build: DPI, Windows/mobile EDR, phishing campaigns, an AI pentester, or a 24/7
human SOC. Wolf ingests or forwards to them instead (Suricata `eve.json`, Wazuh, CrowdSec, UniSOC
through syslog).

## 3. What is built

This first slice needs no new DocType: it adds parsers, enrichment and preset rules.

**Agent: web attacks** (`agent/wolf_agent/parsers/web_attacks.py`, read from the nginx access
logs the agent already tails):

- Requests that probe for secrets (`.env`, `.git/`, `site_config.json`, keys, dumps), CMS and
  admin panels, path traversal, SQL/command/template injection or XSS, or that come from a
  scanner User-Agent. Each (site, client) pair gets one `security` / `web_attack_probe` event
  per push interval, with the rules hit, the statuses, up to 10 targets and the ATT&CK IDs. The
  event is critical once a client sends 50 or more probes.
- `web_sensitive_file_exposed` (critical): a secret-bearing file answered with a 2xx. The file
  was probably leaked.
- No false positives on normal Frappe traffic: attachments (`/files`, `/private/files`) and
  authorized backup downloads (`/backups`) are excluded, URL-encoded targets are decoded twice
  before matching, and the client address is taken from `X-Forwarded-For` only when a local
  proxy logged it.

**Agent: host intrusion signals** (auth log or journal):

- `ssh_login_after_failures` (critical): a successful login from an address that failed 5 or
  more times in the hour before. Failure counts survive push intervals.
- `sudo_failure`: wrong sudo passwords and users outside sudoers.
- `account_created`: a `useradd` creates an account.
- `admin_group_change` (critical): a user is added to `sudo`, `wheel`, `admin`, `root`, `adm`,
  `docker` or `lxd`.
- The journal follower now reads `sshd`, `sshd-session` (OpenSSH 9.8+), `sudo` and the
  shadow-utils tools, in `short-iso` format, so lines carry their real timestamps.

**App: ATT&CK enrichment** (`frappe_wolf/wolf/attack.py`): at ingest, every `security` event
gets a `mitre_attack` list of `{id, name, tactic}` in its payload. It merges the category's
mapping with the technique IDs the agent sent. Events forwarded by satellites keep theirs.

**Presets** (installed with no channels, like the others): SSH login after brute force,
Repeated sudo failures, New system account, User added to admin group, Web attack scan (100+
probes in 10 min per site), Sensitive file served.

## 4. Roadmap

Each step stands on its own, so a step can ship without the ones after it.

### Step 2: Frappe application security (built)

Site signals (`local_signals.py`, rules in `app_security.py`), behind **Wolf Settings → Flag
risky application changes** (on by default):

- **Code execution surfaces** (T1059.006 / T1059.007): a Server or Client Script created,
  enabled, or with its code, type or trigger changed (`server_script_change`,
  `client_script_change`); each System Console run (`console_execution`). Editing a
  description or disabling a script raises nothing.
- **Permission drift** (T1098), all as `permission_change`:
  - Role Permission Manager add, update, remove and reset. Older Frappe versions write `update`,
    `remove` and `reset` with raw SQL, so Wolf reads them from the request through the
    `after_request` hook.
  - Custom DocPerm saved or deleted anywhere else: Data Import, the REST API, code
    (`payload.kind` `custom_docperm`, `via` says how). Each event lists the rights granted and
    removed. A new rule is compared with the DocType's standard rule, so the copies Frappe makes
    before the first custom change grant nothing. A right given to `Guest` or `All` is critical.
  - A User Permission that lets its user see more (`user_permission`). User Permissions are
    allow lists, so the first one for a DocType only restricts (ERPNext creates those for
    employees) and raises nothing. A further value, an edit that allows another value, user or
    DocType, or removing the last one (which lifts the restriction) raise one.
  - A Website user (portal: customer, supplier) given a desk role, which makes them a System
    User (`desk_access`).
  - A Role Profile created or granting more roles.
- **Weakened settings** (T1562.001, critical): System Settings saves that turn 2FA, the password
  policy, one session per user, SSO-only login or logout on password reset off, lower the
  password score or the lockout duration, allow more failed logins (or no lockout), lengthen
  sessions or let guests upload files (`security_settings_weakened`).
- **API keys** (T1098.001): a new API secret written for a user (`api_key_generated`), critical
  for privileged users.
- **Outbound webhooks** (T1567): a Webhook created, enabled or pointed at another host
  (`webhook_external`).
- **Unusual data exports** (T1567 / T1030, `data_exfiltration`), behind its own setting,
  **Flag unusual data exports** (on by default):
  - **What counts:** report exports, Data Export, Data Import templates with records, bulk PDFs
    and every list read (`frappe.client.get_list`, the list and report views,
    `/api/resource/<DocType>`), whatever the page size. A script paging through a table in small
    pages adds up like one big read.
  - **How:** the response size of each counts towards the user's total for the hour, kept in
    Redis by the `after_request` hook (`api/exfiltration.py`, rules in `wolf/exfiltration.py`).
    Every hour, those totals become `user_export_bytes` samples, one series per user
    (`labels.user`). The series names users: whoever may read the site's metrics in Wolf sees
    how much each user took out per hour.
  - **When:** past a floor (50 MB per hour by default), the hour is compared with the user's
    largest hour over the last 14 days. Warning at 5x (Export Factor), critical at 20x. A user
    without any history is critical, except while the site itself has less than 14 days of
    history: then every user lacks one, and it is a warning.
  - **How often:** at most one event per user, hour and severity, so a warning can still be
    followed by a critical. The payload has the hour's bytes, requests and rows (for list
    reads), the baseline, the ratio, the last request (what, which DocType or report) and the
    IP.
- Eight presets, one per signal.

Not built yet: API key *use* from an IP never seen for it needs request logs Wolf does not keep
yet.

Limits: changes made with raw SQL (a patch, `bench mariadb`) fire no document event and are not
seen; patches and migrations are skipped on purpose. A User Permission restricted to one DocType
(`applicable_for`) is counted with the user's others for the same allowed DocType. Script events
keep the first 300 characters of the code.

Limits of export counting:
- **Hours:** totals are per clock hour, so an export spread across two hours is split in two.
- **Background exports:** the list view's export in the background emails a link. Its request is
  counted, but not the file.
- **What is not counted:** files sent as they are (private files, backups) and printing one
  document.
- **Satellites:** they keep their raw samples for 2 days, so their baseline covers 2 days, not 14.
- **Bytes:** the size of the response sent, compressed or not by the proxy afterwards.

### Step 3: posture score and exposure (built)

**Wolf → Security** (`/wolf/security`) shows a score out of 100 per site, with every check,
what Wolf read and the fix (`frappe_wolf/wolf/posture.py`, `api/security.py`):

| Check | Weight | Read from |
|---|---|---|
| Two-factor authentication for System Managers (global switch and the role) | 3 | System Settings, Role (Wolf site only) |
| Password policy, minimum score 3 | 2 | System Settings (Wolf site only) |
| Lockout after 1–10 failed logins | 1 | System Settings (Wolf site only) |
| Guests cannot upload files | 1 | System Settings (Wolf site only) |
| No development settings in site_config.json | 3 | the site config on the Wolf site, else the agent's `site_config_risks` |
| Bench services not listening beyond loopback | 3 | the agent's `exposed_services` on the site's hosts |
| A backup newer than 26 h | 2 | `backup_age_hours` |
| TLS certificates valid for 14 days or more | 2 | `tls_days_left` |

- A check Wolf cannot read is **unknown** and counts as not passed; `coverage` says how much of
  the weight Wolf could read. Agent samples older than 3 h are not used.
- The agent's exposure collector (`agent/wolf_agent/exposure.py`, every 10 min) reads
  `/proc/net/tcp{,6}` and each site's config. It sends `service_exposed` and
  `insecure_site_config` events when what it finds changes.
- The score is stored hourly as `security_score`. Three presets: Security score low (< 60),
  Bench service exposed, Development settings in production.

Not built yet: HSTS, SPF/DMARC of the mail domain, Frappe/ERPNext versions against GitHub
Security Advisories, and the System Settings checks of sites other than the Wolf site (they
need Wolf installed there; satellites forward events, not settings). A listening socket is not
proof of reachability: a firewall may block it.

### Step 4: deception and threat intelligence (built, except CrowdSec)

Wolf Settings → **Threat intelligence and deception** (`frappe_wolf/api/threat.py`,
`wolf/reputation.py`):

- **Honeytokens**, each touch a critical `honeytoken_triggered` event (preset *Honeytoken
  touched*), once per token and address every 10 minutes:
  - **Decoy users**: any login attempt on them, failed or not (Activity Log).
  - **Decoy API keys**: any request whose `Authorization` header (`token key:secret` or Basic)
    carries one. The check runs in `before_request`, before Frappe rejects the unknown key, and
    commits the event at once. Do not create the key on a real user.
  - **Decoy paths**: any request for a path linked from nowhere, such as `/app/backup-admin`.
  Nobody legitimate knows them, so they produce no false positives.
- **IP reputation** (off by default): once a day `refresh_reputation` downloads the listed
  blocklists (`name url` or a file path per line; FireHOL level 1 and Spamhaus DROP prefilled)
  into `private/wolf/ip_reputation.json`. At ingest a security event whose payload `ip` is on a
  list gets `reputation: {listed, lists}` and one severity step more. Lookups never leave the
  site; a list that fails to download keeps its last copy. Check its terms of use before
  enabling a list.
- Not built: **CrowdSec** (sending Wolf's detections and reading its decisions back).

### Step 5: human-validated response (built)

The incident page of the Wolf UI has a **Response** panel (`wolf/response.py`,
`api/response.py`). Wolf proposes; a Wolf Manager approves each action with a second click;
nothing runs on its own. Every approval is a **Wolf Response Action** row (who, when, what,
result; read-only for everyone) and a comment on the incident.

- **Block IP for N hours** (1 to 168, default 24): proposed for the public addresses of the
  incident's security events, for the agent that saw each one. The agent receives it with its
  next push, runs `sudo -n /usr/local/sbin/wolf-block IP SECONDS` (`agent/wolf-block.sh`: an
  nftables `inet wolf` set whose entries expire by themselves) and reports the result. The agent
  only does this with `response.enabled` in its own config (off by default) and the helper
  installed with a one-line sudoers rule (agent/README.md). It refuses private, loopback and
  reserved addresses and durations over a week, whatever the site sends.
- **Disable user / log out all sessions / revoke API keys**: proposed for the users named by
  the site's own security events (failed logins, new IP or country, role granted, API key
  generated), applied at once on the Wolf site. Administrator, Guest and the approver's own
  account are refused, and only a System Manager can act on a System Manager.
- Only what Wolf proposed from the incident's own events can be approved: the API refuses any
  other action, target or agent.
- Blocks are marked *Expired* once their time is over (hourly).
- **From the alert** (`api/actions.py`, `response_action`): alerts for unresolved incidents
  carry a signed link for each action Wolf proposes (up to 5). The link covers the incident,
  the action, its target, agent and duration, the expiry and the recipient, under another
  context than acknowledge / resolve.
  - It only says what to confirm. Opening it shows the proposal; a guest is sent to the login
    page and back; only a logged-in Wolf Manager or System Manager gets the button; only the
    POST acts.
  - The confirmation goes through the same `approve` as the panel, with all its rules, and
    records the manager as the approver and the recipient in the reason. A forwarded email or
    a shared channel alone cannot act.
  - A link opened again after the action says so instead of acting twice.
- Not built: fail2ban instead of nftables, and user actions on other sites than the Wolf site.

### Step 6: compliance and evidence

- **NIS2 Article 23 and GDPR Article 33 on Wolf Incident (built)**. The section *Regulatory
  reporting* on the incident has two flags. **Significant incident (NIS2)** starts three clocks
  from *Aware since*: early warning within 24 h, incident notification within 72 h, final
  report one month after the notification (counted from the notification's deadline, and
  marked *estimated*, until it is sent). **Personal data breach (GDPR)** starts one: the
  notification to the supervisory authority (CNIL) within 72 h. Ticking a flag sets *Aware
  since* to now; it can be corrected. The sending times and the authority's reference are
  recorded on the incident (`frappe_wolf/wolf/compliance.py`, `api/compliance.py`).
  - Every 5 minutes `check_deadlines` records one `regulatory_deadline` event per obligation
    when it is due within 6 h, and again when it is overdue, linked to the incident (so it is
    on its timeline) with a comment. The **Regulatory deadline** preset alerts on them: give it
    the channels of whoever reports to the CSIRT / CNIL.
  - **Regulatory report** (Desk → Actions, or *Report draft* in the Wolf UI) builds a Markdown
    draft: facts, deadlines, timeline (events and comments), postmortem impact, root cause and
    measures. It is a starting point for the authority's form.
  - Wolf does not decide whether an incident is significant or a breach, and sends nothing to
    an authority. Not built: the intermediate report an authority may ask for (NIS2
    Art. 23(4)(c)), DORA's timelines.
- **Tamper-evident journal (built)** (`wolf/journal.py`, `api/journal.py`, DocType *Wolf
  Journal Entry*). Captured, in the transaction that made the change: security events (their
  payload by SHA-256), incident decisions (status, owner, merge, NIS2 / GDPR fields, including
  auto-resolve and reopen), response actions and their results, comments on incidents, and Wolf
  Settings changes (the names of the fields changed and who changed them, never the values).
  - Every minute `chain_journal` signs the waiting entries in creation order, under a lock:
    `mac = HMAC-SHA256(key, seq|at|kind|record|digest|prev_mac)`. Signing in one job means two
    requests can never fork the chain; an entry waits a minute at most.
  - The key is `wolf_journal_key` in the site config, created on first use, never in the
    database: someone who can only write the database cannot forge a chain. Back it up with the
    site config; without it the chain cannot be verified.
  - `bench --site <site> wolf-verify-journal [--anchor seq:mac]` (or *Verify* on Wolf →
    Security) recomputes every digest and mac and finds an edited, removed or reordered entry.
    Deleting the newest entries leaves a valid shorter chain: copy the **anchor** (newest
    `seq:mac`, shown on the Security page) somewhere Wolf cannot write, and check it with
    `--anchor`. The SIEM export below sends every entry out, which is the best anchor.
  - Entries are read-only through Frappe (edits and deletions are refused).
  - **Retention** (Wolf Settings → Journal Retention Days, 0 by default: kept forever). Every
    night, `purge_journal` removes the signed entries older than the retention, as the oldest
    part of the chain:
    - first it writes and signs a `journal_purge` entry with where the purge stopped
      (`through_seq` and its mac), then it deletes, all under the signing lock;
    - it always keeps the newest entry, and never one the SIEM export has not sent yet (when it
      sends the journal);
    - verification then requires a journal that does not start at entry 1 to start right after
      the newest purge record's cut, chained from its mac. Deleting more of the oldest entries,
      or any of them without a purge, breaks it. Before retention existed, a journal missing its
      oldest entries still verified;
    - an anchor in the purged part is reported as purged (copy a newer one), except the cut
      itself, whose mac the purge record keeps.
- **SOC export (built)** (`wolf/siem.py`, `api/siem.py`, Wolf Settings → SIEM export).
  Every minute, the security events (or the types you list) at or above a minimum severity,
  and every signed journal entry, go to a syslog collector: RFC 5424 (facility local0)
  carrying **OCSF** JSON (Detection Finding, class 2004, ATT&CK techniques in
  `finding_info.attacks`, Wolf's site / bench / payload under `unmapped`) or **CEF** (ATT&CK
  in `cs4`, reputation lists in `cs5`), over TCP, TLS (octet-counted frames, RFC 6587) or UDP.
  UniSOC, Wazuh, Graylog or any SIEM ingests it with no custom connector.
  - A cursor moves only after a batch is sent: a collector that is down delays the export,
    nothing is skipped. A failure mid-batch can send a message twice (at least once). Events
    younger than 2 minutes wait for the next run, so a transaction that commits a little after
    it inserted is not jumped over. The cursor is a global default (`wolf_siem_cursor`), not a
    settings field, so the settings form is never touched by the export.
  - The journal entries (seq, digest, prev_mac, mac) give the SIEM an external copy of the chain:
    the anchor Wolf cannot rewrite.
  - Payloads go out as they are stored (already scrubbed by the agent), cut at 4 KB. The OCSF
    mapping is Wolf's reading of the schema, not a certified profile.

## 5. Mapping reference

| Category | Raised by | ATT&CK |
|---|---|---|
| `ssh_failed_login` | agent (auth) | T1110 Brute Force |
| `ssh_root_login` | agent (auth) | T1078.003 Local Accounts |
| `ssh_login_after_failures` | agent (auth) | T1110, T1078 |
| `sudo_failure` | agent (auth) | T1548.003 Sudo and Sudo Caching |
| `account_created` | agent (auth) | T1136.001 Create Local Account |
| `admin_group_change` | agent (auth) | T1098 Account Manipulation |
| `web_attack_probe` | agent (nginx) | T1595.003, plus the technique of each rule hit (T1190, T1189, T1552, T1595.002…) |
| `web_sensitive_file_exposed` | agent (nginx) | T1552.001 Credentials In Files |
| `frappe_failed_login` | site | T1110 |
| `new_admin_ip`, `admin_login_new_country` | site | T1078 Valid Accounts |
| `privileged_user` | site | T1098 |
| `server_script_change`, `console_execution` | site | T1059.006 Python |
| `client_script_change` | site | T1059.007 JavaScript |
| `permission_change` | site | T1098 Account Manipulation |
| `security_settings_weakened` | site | T1562.001 Disable or Modify Tools |
| `api_key_generated` | site | T1098.001 Additional Cloud Credentials |
| `webhook_external` | site | T1567 Exfiltration Over Web Service |
| `data_exfiltration` | site | T1567 Exfiltration Over Web Service, T1030 Data Transfer Size Limits |
