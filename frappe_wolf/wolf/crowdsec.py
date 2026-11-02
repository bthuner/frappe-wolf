"""CrowdSec Local API (LAPI): read its decisions, send it Wolf's detections and approved blocks
(no frappe imports; api/crowdsec.py runs it).

- **Decisions** (bouncer API key, `cscli bouncers add`): `GET /v1/decisions/stream`. The first
  call (`startup=true`) returns every active decision, the next ones what was added and deleted
  since. That covers CrowdSec's own detections, `cscli` bans, and the community blocklist when
  the LAPI is enrolled. `Decisions` keeps them by IP and range for the event enrichment.
- **Alerts** (machine login and password, `cscli machines add`): `POST /v1/alerts`.
  - A Wolf detection becomes an alert with `remediation` false by default. CrowdSec records it
    and shows it in `cscli alerts list`, but its profiles (default: `Alert.Remediation == true`)
    make no decision from it. With remediation on, the profiles decide, and the default one
    bans the IP for 4 h.
  - A block a Wolf Manager approved becomes an alert carrying its own `ban` decision, for the
    approved duration, so every bouncer of that LAPI applies it.

The LAPI refuses a login whose User-Agent is not `name/version`: the client always sends one.
"""

import ipaddress
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SCENARIO_PREFIX = "frappe-wolf/"
ORIGIN = "frappe-wolf"
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


class CrowdSecError(Exception):
	pass


def _iso(dt):
	if dt is None:
		dt = datetime.now(timezone.utc)
	if isinstance(dt, str):
		return dt
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=timezone.utc)
	return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ip_of(event):
	"""The event's source address when it is a valid IP, else None."""
	payload = event.get("payload") or {}
	value = payload.get("ip") if isinstance(payload, dict) else None
	try:
		return str(ipaddress.ip_address(str(value).strip())) if value else None
	except ValueError:
		return None


def detection_alert(event, remediation=False):
	"""A LAPI alert for a Wolf security event (`event`: name, category, severity, title,
	timestamp, site, host, payload with `ip`). None when the event has no IP."""
	ip = ip_of(event)
	if not ip:
		return None
	at = _iso(event.get("timestamp"))
	meta = [{"key": k, "value": str(v)[:200]} for k, v in (
		("wolf_event", event.get("name")), ("wolf_category", event.get("category")), ("wolf_severity", event.get("severity")),
		("site", event.get("site")), ("host", event.get("host")),
	) if v]  # fmt: skip
	return {
		"scenario": SCENARIO_PREFIX + (event.get("category") or "security"),
		"scenario_hash": "", "scenario_version": "", "simulated": False, "capacity": 0, "leakspeed": "0",
		"message": f"Frappe Wolf: {event.get('title') or event.get('category')}"[:500],
		"events_count": max(int(event.get("count") or 1), 1),
		"start_at": at, "stop_at": at,
		"events": [{"timestamp": at, "meta": meta}],
		"source": {"scope": "Ip", "value": ip, "ip": ip},
		"remediation": bool(remediation),
	}  # fmt: skip


def block_alert(ip, hours, reason, at=None):
	"""A LAPI alert carrying a `ban` decision: a block a Wolf Manager approved."""
	at = _iso(at)
	scenario = SCENARIO_PREFIX + "approved_block"
	return {
		"scenario": scenario, "scenario_hash": "", "scenario_version": "", "simulated": False, "capacity": 0,
		"leakspeed": "0", "message": f"Frappe Wolf: {reason}"[:500], "events_count": 1, "start_at": at, "stop_at": at,
		"events": [{"timestamp": at, "meta": [{"key": "wolf_reason", "value": str(reason)[:200]}]}],
		"source": {"scope": "Ip", "value": ip, "ip": ip}, "remediation": True,
		"decisions": [{"type": "ban", "duration": f"{int(hours)}h", "scope": "Ip", "value": ip, "origin": ORIGIN,
		               "scenario": scenario}],
	}  # fmt: skip


def parse_duration(text):
	"""Go durations as the LAPI writes them ('3h59m58.5s', '-1m', '24h0m0s') → timedelta."""
	text = str(text or "").strip()
	if not text:
		raise ValueError("empty duration")
	sign = -1 if text.startswith("-") else 1
	total, number = 0.0, ""
	units = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
	i = 0
	text = text.lstrip("+-")
	while i < len(text):
		ch = text[i]
		if ch.isdigit() or ch == ".":
			number += ch
			i += 1
			continue
		unit = "ms" if text[i : i + 2] == "ms" else ch
		if unit not in units or not number:
			raise ValueError(f"bad duration {text!r}")
		total += float(number) * units[unit]
		number = ""
		i += len(unit)
	if number:
		raise ValueError(f"bad duration {text!r}")
	return timedelta(seconds=sign * total)


class Decisions:
	"""Active decisions by scope value: `{value: {type, origin, scenario, until}}` for IPs and
	ranges. Kept as JSON between runs."""

	def __init__(self, items=None):
		self.items = dict(items or {})
		self._nets = None

	def apply(self, stream, now):
		"""Merge a `/v1/decisions/stream` answer (new and deleted)."""
		for d in stream.get("deleted") or []:
			self.items.pop(str(d.get("value")), None)
		for d in stream.get("new") or []:
			if (d.get("scope") or "").lower() not in ("ip", "range") or not d.get("value"):
				continue
			try:
				until = now + parse_duration(d.get("duration"))
			except ValueError:
				until = now + timedelta(hours=4)
			self.items[str(d["value"])] = {"type": d.get("type"), "origin": d.get("origin"), "scenario": d.get("scenario"),
			                               "until": until.strftime("%Y-%m-%dT%H:%M:%S")}  # fmt: skip
		self._nets = None
		return self

	def prune(self, now):
		stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
		self.items = {k: v for k, v in self.items.items() if v.get("until", "") > stamp}
		self._nets = None
		return self

	def lookup(self, address, now=None):
		"""The decision covering `address` (an exact IP first, then the narrowest range), or None."""
		try:
			ip = ipaddress.ip_address(str(address).strip())
		except ValueError:
			return None
		if getattr(ip, "ipv4_mapped", None):
			ip = ip.ipv4_mapped
		stamp = (now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
		found = self.items.get(str(ip))
		if found and found.get("until", "") > stamp:
			return {"value": str(ip), **found}
		if self._nets is None:
			self._nets = []
			for value in self.items:
				if "/" in value:
					try:
						self._nets.append((ipaddress.ip_network(value, strict=False), value))
					except ValueError:
						continue
			self._nets.sort(key=lambda n: n[0].prefixlen, reverse=True)
		for net, value in self._nets:
			if net.version == ip.version and ip in net and self.items[value].get("until", "") > stamp:
				return {"value": value, **self.items[value]}
		return None

	def to_json(self):
		return self.items

	@classmethod
	def from_json(cls, data):
		return cls(data if isinstance(data, dict) else {})


def tag(payload, severity, decisions, now=None):
	"""(payload, severity) with the CrowdSec decision on `payload["ip"]`: one severity step more,
	except for Wolf's own blocks (they are already the answer to this event's kind)."""
	if not isinstance(payload, dict) or not payload.get("ip") or decisions is None or "crowdsec" in payload:
		return payload, severity
	found = decisions.lookup(payload["ip"], now)
	if not found:
		return payload, severity
	tagged = {**payload, "crowdsec": {k: found.get(k) for k in ("type", "origin", "scenario", "until", "value")}}
	if found.get("origin") == ORIGIN:
		return tagged, severity
	return tagged, {"info": "warning", "warning": "critical"}.get(severity, severity)


class Client:
	def __init__(self, url, bouncer_key=None, machine_id=None, password=None, version="0", timeout=10,
	             opener=urllib.request.urlopen):  # fmt: skip
		self.url = (url or "").rstrip("/")
		self.bouncer_key, self.machine_id, self.password = bouncer_key, machine_id, password
		self.agent = f"frappe-wolf/{version or '0'}"  # the LAPI wants name/version
		self.timeout, self.opener = timeout, opener
		self.token = None

	def _call(self, method, path, body=None, headers=None):
		req = urllib.request.Request(self.url + path, method=method,
		                             data=json.dumps(body).encode() if body is not None else None,
		                             headers={"Content-Type": "application/json", "User-Agent": self.agent, **(headers or {})})  # fmt: skip
		try:
			with self.opener(req, timeout=self.timeout) as resp:
				raw = resp.read()
		except urllib.error.HTTPError as e:
			try:
				detail = e.read().decode("utf-8", "replace")[:200]
			finally:
				e.close()
			raise CrowdSecError(f"{method} {path}: HTTP {e.code} {detail}") from e
		except (OSError, ValueError) as e:
			raise CrowdSecError(f"{method} {path}: {e}") from e
		try:
			return json.loads(raw or b"null")
		except ValueError as e:
			raise CrowdSecError(f"{method} {path}: not JSON") from e

	def decisions_stream(self, startup=False):
		if not self.bouncer_key:
			raise CrowdSecError("no bouncer API key")
		answer = self._call("GET", "/v1/decisions/stream" + ("?startup=true" if startup else ""),
		                    headers={"X-Api-Key": self.bouncer_key})  # fmt: skip
		return answer if isinstance(answer, dict) else {"new": None, "deleted": None}

	def login(self):
		if not (self.machine_id and self.password):
			raise CrowdSecError("no machine login")
		answer = self._call("POST", "/v1/watchers/login", {"machine_id": self.machine_id, "password": self.password})
		if not isinstance(answer, dict) or not answer.get("token"):
			raise CrowdSecError("login: no token")
		self.token = answer["token"]
		return self.token

	def push_alerts(self, alerts):
		"""POST the alerts (logging in first); returns the LAPI's alert ids."""
		alerts = [a for a in alerts if a]
		if not alerts:
			return []
		if not self.token:
			self.login()
		try:
			return self._call("POST", "/v1/alerts", alerts, {"Authorization": "Bearer " + self.token}) or []
		except CrowdSecError as e:
			if "HTTP 401" not in str(e):
				raise
			self.login()  # the token expired: once more
			return self._call("POST", "/v1/alerts", alerts, {"Authorization": "Bearer " + self.token}) or []


def wanted(event, min_severity="warning"):
	"""Whether a Wolf event goes to CrowdSec: a security event with an IP, at or above the
	minimum severity, that did not come from CrowdSec itself."""
	if event.get("event_type") != "security" or not ip_of(event):
		return False
	if (event.get("source") or "") == "crowdsec":
		return False
	return SEVERITY_RANK.get(event.get("severity"), 0) >= SEVERITY_RANK.get(min_severity, 1)
