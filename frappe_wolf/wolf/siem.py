"""SIEM export formats: RFC 5424 syslog carrying CEF or OCSF JSON (no frappe imports).

- `cef(event)`: ArcSight Common Event Format, one line, escaped as the CEF spec asks
  (`\\` and `|` in the header; `\\`, `=` and newlines in extensions). Severity 0-10.
- `ocsf(event)`: OCSF 1.3 Detection Finding (class 2004, category 2), with the ATT&CK
  techniques in `finding_info.attacks` and Wolf's own fields (site, bench, payload) under
  `unmapped`. The mapping is Wolf's reading of the schema, not a certified profile.
- `journal_cef` / `journal_json`: the journal entries, so the SIEM keeps an external copy of the
  chain (seq, digest, mac): the best anchor there is.
- `syslog(...)`: an RFC 5424 message (facility local0); `frame(...)`: RFC 6587 framing for TCP
  and TLS, octet counting (the default) or one message per line (non-transparent framing, which
  Wazuh and other line-based collectors expect).
- `Sender`: delivery over UDP (one message per datagram, RFC 5426), TCP or TLS (RFC 5425), the
  collector's certificate checked against the system CAs or a CA file.
"""

import json
import socket
import ssl
from datetime import datetime, timezone

VENDOR, PRODUCT = "Dokos", "Frappe Wolf"
FACILITY = 16  # local0
SYSLOG_SEVERITY = {"critical": 2, "warning": 4, "info": 6}
CEF_SEVERITY = {"critical": 9, "warning": 5, "info": 2}
OCSF_SEVERITY = {"critical": 5, "warning": 3, "info": 1}
OCSF_VERSION = "1.3.0"
MAX_PAYLOAD = 4000
MAX_UDP = 8192


def _epoch_ms(dt):
	if not dt:
		return None
	if isinstance(dt, str):
		dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=timezone.utc)
	return int(dt.timestamp() * 1000)


def _cef_header(value):
	return str(value if value is not None else "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _cef_ext(value):
	text = str(value if value is not None else "")
	return text.replace("\\", "\\\\").replace("=", "\\=").replace("\r", "\\r").replace("\n", "\\n")


def _ext(pairs):
	return " ".join(f"{k}={_cef_ext(v)}" for k, v in pairs if v not in (None, ""))


def cef(event, version="", device=None):
	"""One CEF line for a Wolf Event dict (timestamp as aware datetime or ISO string, in UTC)."""
	payload = event.get("payload") or {}
	attacks = ",".join(t.get("id", "") for t in payload.get("mitre_attack") or [] if isinstance(t, dict))
	ext = _ext([
		("rt", _epoch_ms(event.get("timestamp"))), ("dvchost", device), ("externalId", event.get("name")), ("cat", event.get("event_type")),
		("msg", event.get("title")), ("src", payload.get("ip")), ("dhost", event.get("host")),
		("suser", payload.get("user")), ("cnt", event.get("count")),
		("cs1Label", "site"), ("cs1", event.get("site")), ("cs2Label", "bench"), ("cs2", event.get("bench")),
		("cs3Label", "incident"), ("cs3", event.get("incident")), ("cs4Label", "mitre_attack"), ("cs4", attacks or None),
		("cs5Label", "reputation"), ("cs5", ",".join((payload.get("reputation") or {}).get("lists") or []) or None),
	])  # fmt: skip
	head = [VENDOR, PRODUCT, version, event.get("category") or event.get("event_type"), event.get("title"),
	        CEF_SEVERITY.get(event.get("severity"), 3)]  # fmt: skip
	return "CEF:0|" + "|".join(_cef_header(h) for h in head) + "|" + ext


def ocsf(event, version=""):
	"""An OCSF Detection Finding (dict) for a Wolf Event."""
	payload = event.get("payload") or {}
	attacks = [
		{"technique": {"uid": t.get("id"), "name": t.get("name")}, **({"tactic": {"name": t["tactic"]}} if t.get("tactic") else {})}
		for t in payload.get("mitre_attack") or [] if isinstance(t, dict) and t.get("id")
	]  # fmt: skip
	text = json.dumps(payload, default=str, sort_keys=True)
	finding = {
		"class_uid": 2004, "class_name": "Detection Finding", "category_uid": 2, "category_name": "Findings",
		"activity_id": 1, "activity_name": "Create", "type_uid": 200401,
		"severity_id": OCSF_SEVERITY.get(event.get("severity"), 0), "severity": (event.get("severity") or "").title() or None,
		"time": _epoch_ms(event.get("timestamp")), "message": event.get("title"), "count": event.get("count"),
		"finding_info": {"uid": event.get("name"), "title": event.get("title"), "types": [event.get("category") or event.get("event_type")],
		                 **({"attacks": attacks} if attacks else {})},
		"metadata": {"version": OCSF_VERSION, "product": {"name": PRODUCT, "vendor_name": VENDOR, "version": version},
		             "log_name": event.get("source")},
		"device": {"hostname": event.get("host")} if event.get("host") else None,
		"src_endpoint": {"ip": payload["ip"]} if payload.get("ip") else None,
		"unmapped": {"site": event.get("site"), "bench": event.get("bench"), "incident": event.get("incident"),
		             "event_type": event.get("event_type"),
		             "payload": payload if len(text) <= MAX_PAYLOAD else {"truncated": True, "preview": text[:MAX_PAYLOAD]}},
	}  # fmt: skip
	return {k: v for k, v in finding.items() if v is not None}


def journal_json(entry):
	return {"wolf_journal": {k: entry.get(k) for k in ("seq", "at", "kind", "ref_doctype", "ref_name", "summary", "digest",
	                                                   "prev_mac", "mac")}}  # fmt: skip


def journal_cef(entry, version="", device=None):
	ext = _ext([("dvchost", device), ("externalId", entry.get("seq")), ("rt", _epoch_ms(entry.get("at"))), ("msg", entry.get("summary")),
	            ("cs1Label", "record"), ("cs1", f"{entry.get('ref_doctype')}/{entry.get('ref_name')}"),
	            ("cs2Label", "digest"), ("cs2", entry.get("digest")), ("cs3Label", "prev_mac"), ("cs3", entry.get("prev_mac")),
	            ("cs4Label", "mac"), ("cs4", entry.get("mac"))])  # fmt: skip
	head = [VENDOR, PRODUCT, version, f"journal:{entry.get('kind')}", entry.get("summary"), 1]
	return "CEF:0|" + "|".join(_cef_header(h) for h in head) + "|" + ext


def syslog(message, hostname, severity="info", msgid="-", timestamp=None, app="frappe-wolf"):
	"""An RFC 5424 message: <PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG."""
	pri = FACILITY * 8 + SYSLOG_SEVERITY.get(severity, 6)
	ts = timestamp or datetime.now(timezone.utc)
	if isinstance(ts, datetime):
		if ts.tzinfo is None:
			ts = ts.replace(tzinfo=timezone.utc)
		ts = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

	def token(value, limit):
		text = "".join(ch for ch in str(value or "-") if 33 <= ord(ch) <= 126)  # PRINTUSASCII, no spaces
		return (text or "-")[:limit]

	return f"<{pri}>1 {ts} {token(hostname, 255)} {token(app, 48)} - {token(msgid, 32)} - {message}"


def frame(message, framing="octet-counting"):
	"""RFC 6587: octet counting (`LEN SP MSG`, LEN in bytes), or `MSG LF` for "newline" (a line
	break inside the message would split it, so it becomes a space)."""
	if framing == "newline":
		return message.replace("\r", " ").replace("\n", " ").encode("utf-8") + b"\n"
	data = message.encode("utf-8")
	return f"{len(data)} ".encode("ascii") + data


def datagram(message, limit=MAX_UDP):
	"""A message cut to `limit` bytes without splitting a UTF-8 character."""
	data = message.encode("utf-8")
	if len(data) <= limit:
		return data
	return data[:limit].decode("utf-8", "ignore").encode("utf-8")


def unframe(data):
	"""Split octet-counted frames back into messages (tests and debugging)."""
	out, i = [], 0
	while i < len(data):
		space = data.index(b" ", i)
		length = int(data[i:space])
		out.append(data[space + 1 : space + 1 + length].decode("utf-8"))
		i = space + 1 + length
	return out


def render(kind, record, fmt, hostname, version="", header="rfc5424"):
	"""The message for an event (`kind` 'event') or a journal entry ('journal'): an RFC 5424
	syslog line, or with `header` "none" the bare CEF or JSON (Wazuh decodes bare JSON lines,
	not the RFC 5424 header)."""
	if kind == "journal":
		body = journal_cef(record, version, hostname) if fmt == "cef" else json.dumps(journal_json(record), default=str, sort_keys=True)
		return body if header == "none" else syslog(body, hostname, "info", "journal", record.get("at"))
	body = cef(record, version, hostname) if fmt == "cef" else json.dumps(ocsf(record, version), default=str, sort_keys=True)
	if header == "none":
		return body
	return syslog(body, hostname, record.get("severity") or "info", record.get("category") or record.get("event_type"),
	              record.get("timestamp"))  # fmt: skip


class Sender:
	"""One batch per connection: TCP and TLS open, send every frame and close; UDP sends one
	datagram per message and cannot tell whether the collector got them."""

	def __init__(self, host, port, transport="tcp", verify_tls=True, ca_file=None, framing="octet-counting", timeout=10):
		self.host, self.port, self.transport = host, int(port), transport or "tcp"
		self.verify_tls, self.ca_file, self.framing, self.timeout = verify_tls, ca_file or None, framing, timeout

	def context(self):
		context = ssl.create_default_context(cafile=self.ca_file)
		if not self.verify_tls:
			context.check_hostname = False
			context.verify_mode = ssl.CERT_NONE
		return context

	def send(self, messages):
		if not messages:
			return
		if self.transport == "udp":
			family, kind, proto, _, address = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)[0]
			with socket.socket(family, kind, proto) as s:
				for m in messages:
					s.sendto(datagram(m), address)
			return
		context = self.context() if self.transport == "tls" else None  # a bad CA file fails before connecting
		raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
		conn = raw
		try:
			if context:
				conn = context.wrap_socket(raw, server_hostname=self.host)
			conn.sendall(b"".join(frame(m, self.framing) for m in messages))
			if conn is not raw:
				try:
					conn = conn.unwrap()  # close_notify: the collector sees a clean end of stream
				except (OSError, ssl.SSLError):
					pass
		finally:
			conn.close()
			raw.close()
