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

### Step 2: Frappe application security (built, except exfiltration)

Site signals (`local_signals.py`, rules in `app_security.py`), behind **Wolf Settings → Flag
risky application changes** (on by default):

- **Code execution surfaces** (T1059.006 / T1059.007): a Server or Client Script created,
  enabled, or with its code, type or trigger changed (`server_script_change`,
  `client_script_change`); each System Console run (`console_execution`). Editing a
  description or disabling a script raises nothing.
- **Permission drift** (T1098): Role Permission Manager add, update, remove and reset
  (`permission_change`). Frappe writes `update`, `remove` and `reset` with raw SQL, so Wolf reads
  them from the request through the `after_request` hook; also a Role Profile created or granting
  more roles.
- **Weakened settings** (T1562.001, critical): System Settings saves that turn 2FA, the password
  policy, one session per user, SSO-only login or logout on password reset off, lower the
  password score or the lockout duration, allow more failed logins (or no lockout), lengthen
  sessions or let guests upload files (`security_settings_weakened`).
- **API keys** (T1098.001): a new API secret written for a user (`api_key_generated`), critical
  for privileged users.
- **Outbound webhooks** (T1567): a Webhook created, enabled or pointed at another host
  (`webhook_external`).
- Seven presets, one per signal.

Not built yet: **exfiltration** (T1567/T1030): report exports, Data Export or large
`get_list` page sizes above a per-user baseline. It needs a baseline per user. API key *use*
from an IP never seen for it also needs request logs Wolf does not keep yet.

Limits: permission rules changed outside the Role Permission Manager (Data Import, the REST API
on Custom DocPerm) are not seen. Script events keep the first 300 characters of the code.

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

### Step 5: human-validated response

Wolf reuses the signed action links that already acknowledge and resolve incidents. Each action
is proposed on the incident, applied only after a Wolf Manager confirms it, and audit-logged:

- **Block IP for N hours**: the agent applies an `nftables` set entry or a fail2ban ban, with an
  expiry.
- **Disable user / log out all sessions / revoke API keys**: applied on the Frappe site.

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
- **Tamper-evident journal**: each security event and incident action is chained with an HMAC
  of the previous entry. A `bench --site … wolf-verify-journal` command proves nothing was
  removed or edited.
- **SOC export**: syslog (RFC 5424) with CEF or OCSF JSON per event, so UniSOC, Wazuh or any
  SIEM can ingest Wolf with no custom connector.

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
