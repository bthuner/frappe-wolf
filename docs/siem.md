# SIEM export: settings per collector

Wolf Settings → SIEM export sends Wolf's security events and its signed journal to a collector
(`wolf/siem.py`, `api/siem.py`; how it works: [cyber.md](cyber.md), step 6). This page gives the
settings that work with each collector. Each recipe below was tried against the real collector
(November 2026) by exporting Wolf events and reading what the collector stored.

| Collector | Format | Message header | Transport | Framing | What you get |
|---|---|---|---|---|---|
| rsyslog 8.2404 | OCSF or CEF | rfc5424 | UDP, TCP or TLS | octet-counting (TCP/TLS) | messages written as sent; MSGID is the event category |
| Wazuh 4.9.2 manager | OCSF | none | TCP | newline | one Wazuh alert per finding, level by severity, with the [rules file](siem/wazuh_frappe_wolf_rules.xml) |
| Graylog 6.1 | CEF | none | TCP | newline | the CEF input extracts every field (source address, ATT&CK, site, severity) |
| Graylog 6.1 | OCSF or CEF | rfc5424 | UDP, TCP or TLS | octet-counting | the Syslog input stores the messages, but does not split the JSON into fields |

## Settings

- **Format**: OCSF (JSON, Detection Finding class 2004) or CEF.
- **Message header**:
  - `rfc5424`: a syslog header before each message: priority, timestamp, the site as host
    name, `frappe-wolf` as application and the category as MSGID.
  - `none`: the bare JSON or CEF. Use it for collectors that decode the message itself, such as
    Wazuh's JSON decoder and Graylog's CEF input.
- **Transport**:
  - TCP, or TLS (RFC 5425).
  - UDP (RFC 5426) sends one datagram per message, cut at 8 KB. Wolf cannot tell whether the
    collector got anything, so the cursor moves anyway. Prefer TCP or TLS.
- **Framing** (TCP and TLS, RFC 6587):
  - `octet-counting` (the default): each message is preceded by its length. rsyslog,
    syslog-ng and Graylog's Syslog input read it.
  - `newline`: one message per line, with any line break inside a message turned into a space.
    Wazuh and Graylog's CEF input need it.
- **TLS**:
  - The collector's certificate is checked against the system CAs, or against **Collector CA
    file** (a PEM file, absolute or relative to the site) when a private CA signed it.
  - The name you give as host must be in the certificate.
  - Python 3.13+ verifies strictly: the CA needs `basicConstraints=CA:TRUE` and
    `keyUsage=keyCertSign`, and the server certificate needs an Authority Key Identifier.
  - Turning verification off works, but anyone on the path can then read the export.

**Send test** (Wolf Settings) sends one message with the current settings. The status shows the
last error. When a collector is down, nothing is skipped: the cursor waits.

## rsyslog

```
module(load="imptcp")                     # TCP, octet-counted or newline
input(type="imptcp" port="514")
module(load="imtcp" StreamDriver.Name="gtls" StreamDriver.Mode="1" StreamDriver.AuthMode="anon")
input(type="imtcp" port="6514")           # TLS (global DefaultNetstreamDriver* point at the cert)
module(load="imudp")
input(type="imudp" port="514")
```

## Wazuh

1. On the manager, in `/var/ossec/etc/ossec.conf`, add a syslog input:

   ```xml
   <remote>
     <connection>syslog</connection>
     <port>514</port>
     <protocol>tcp</protocol>
     <allowed-ips>WOLF_SITE_ADDRESS/32</allowed-ips>
   </remote>
   ```

2. Copy [`siem/wazuh_frappe_wolf_rules.xml`](siem/wazuh_frappe_wolf_rules.xml) to
   `/var/ossec/etc/rules/` and restart the manager. It needs Wazuh 4.6 or later.
3. Set Wolf to: format OCSF, message header none, transport TCP, framing newline.

The rules give:

| Wolf finding | Wazuh rule | Level |
|---|---|---|
| info | 100901 | 3 |
| warning | 100902 | 7 |
| critical | 100903 | 12 |
| journal entry | 100910 | 3 |

The OCSF fields become Wazuh fields: `src_endpoint.ip`, `finding_info.title`,
`finding_info.attacks`, `unmapped.site`, and so on.

What does not work with Wazuh:
- **The RFC 5424 header:** Wazuh does not parse it, so no decoder matches and only a generic
  rule fires.
- **CEF:** Wazuh has no decoder for it.
- **Octet-counted frames:** they are dropped.
- **TLS:** Wazuh's syslog input has no TLS. Put a local relay in front (rsyslog, stunnel) when
  the network between Wolf and Wazuh is not trusted.

## Graylog

1. **Fields:** create a **CEF TCP** input, then set Wolf to: format CEF, message header none,
   transport TCP, framing newline.
   - The input extracts every field: `sourceAddress`, `deviceHostName` (the site),
     `mitre_attack`, `site`, `severity`, `event_class_id` (the category) and the journal's
     `digest` / `mac`.
   - `source` is the address of the Wolf site.
   - The RFC 5424 header confuses this input: `source` becomes `-`.
2. **Storage only:** the Syslog TCP, UDP or TLS inputs accept the default settings
   (rfc5424, octet-counting).
   - Graylog keeps MSGID and the structured-data dash at the start of `message`, and does not
     split the JSON.
   - Add a JSON extractor or a pipeline rule if you take this road.

## UniSOC and other SIEMs

UniSOC has not been tried, for lack of an instance. Any collector that takes syslog with
RFC 5424, CEF or JSON lines should work with one of the combinations above.
