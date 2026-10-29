"""sshd auth log parser (auth.log, /var/log/secure or `journalctl -o cat`) → `security` events."""

import re

from ..util import event, fingerprint, parse_log_timestamp, utc_iso

_IP = r"(?P<ip>[0-9a-fA-F:.]+)"
_FAILED_RE = re.compile(
	r"Failed (?:password|publickey|keyboard-interactive/pam|none) for (?P<invalid>invalid user )?(?P<user>\S+) from " + _IP
)
_INVALID_RE = re.compile(r"Invalid user (?P<user>\S*) from " + _IP)
_ROOT_ACCEPTED_RE = re.compile(r"Accepted (?P<method>\S+) for root from " + _IP)
_REPEATED_RE = re.compile(r"message repeated (?P<n>\d+) times")
CRITICAL_THRESHOLD = 20
MAX_SAMPLES = 3


def parse_line(line):
	"""Return `{kind, ip, user, count, timestamp}` for interesting sshd lines, else None."""
	line = line.rstrip("\n")
	repeated = _REPEATED_RE.search(line)
	count = int(repeated.group("n")) if repeated else 1
	ts = parse_log_timestamp(line)

	m = _ROOT_ACCEPTED_RE.search(line)
	if m:
		return {"kind": "ssh_root_login", "ip": m.group("ip"), "user": "root", "count": count, "timestamp": ts}

	# "Invalid user X from IP" is logged before "Failed password for invalid user X";
	# count only the former so an attempt isn't counted twice.
	m = _INVALID_RE.search(line)
	if m:
		return {"kind": "ssh_failed_login", "ip": m.group("ip"), "user": m.group("user"), "count": count, "timestamp": ts}

	m = _FAILED_RE.search(line)
	if m and not m.group("invalid"):
		return {"kind": "ssh_failed_login", "ip": m.group("ip"), "user": m.group("user"), "count": count, "timestamp": ts}
	return None


class AuthParser:
	def __init__(self, host, log_file=None, **_):
		self.host, self.log_file = host, log_file
		self.groups = {}

	def feed(self, line):
		parsed = parse_line(line)
		if not parsed:
			return
		key = (parsed["kind"], parsed["ip"])
		g = self.groups.get(key)
		if not g:
			g = self.groups[key] = {"count": 0, "users": [], "timestamp": parsed["timestamp"], "samples": []}
		g["count"] += parsed["count"]
		if parsed["user"] not in g["users"] and len(g["users"]) < 10:
			g["users"].append(parsed["user"])
		if len(g["samples"]) < MAX_SAMPLES:
			g["samples"].append(line.strip()[:1000])

	def drain(self, now=None):
		ts = utc_iso(now)
		events = []
		for (kind, ip), g in self.groups.items():
			if kind == "ssh_root_login":
				title = f"Root SSH login from {ip}"
				severity = "warning"
			else:
				title = f"{g['count']} failed SSH login(s) from {ip}"
				severity = "critical" if g["count"] >= CRITICAL_THRESHOLD else "warning"
			events.append(
				event(
					"security",
					kind,
					severity,
					g["timestamp"] or ts,
					self.host,
					title,
					fingerprint(self.host, kind, ip),
					{
						"ip": ip,
						"count": g["count"],
						"users": g["users"],
						"sample_lines": g["samples"],
						"log_file": self.log_file,
					},
					source="sshd",
				)
			)
		self.groups = {}
		return [], events
