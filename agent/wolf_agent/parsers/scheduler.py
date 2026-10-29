"""Frappe scheduler log parser: failed scheduled jobs → `cron_failure` events.

Handles the plain logger format used by Frappe v14/v15
(`%(asctime)s %(levelname)s scheduler %(message)s`, e.g.
`Scheduled Job Failed: erpnext.foo.bar for site1.local`) and JSON log lines.
"""

import json
import re

from ..util import event, fingerprint, parse_log_timestamp, utc_iso

_SCHEDULED_FAILED_RE = re.compile(
	r"Scheduled Job (?:Failed|failed|FAILED)\s*:?\s*(?P<method>[\w.]+)(?: for (?P<site>\S+))?"
)
_ENQUEUE_RE = re.compile(r"Exception in Enqueue Events for Site (?P<site>\S+)")
_GENERIC_RE = re.compile(r"\bERROR\b.*\bscheduler\b.*(?:Exception|\bfailed\b)", re.IGNORECASE)
_DOTTED_RE = re.compile(r"\b[a-z_]\w*(?:\.\w+){2,}\b")
_FAILED_WORD_RE = re.compile(r"\bfailed\b", re.IGNORECASE)
MAX_SAMPLES = 3


def _dotted_method(line):
	for candidate in _DOTTED_RE.findall(line):
		if not candidate.startswith(("frappe.utils.background_jobs", "frappe.utils.scheduler")):
			return candidate
	return None


def _parse_json(line):
	start = line.find("{")
	if start == -1:
		return None
	try:
		data = json.loads(line[start:])
	except ValueError:
		return None
	if not isinstance(data, dict):
		return None
	status = str(data.get("status") or data.get("level") or "").lower()
	if status not in ("failed", "failure", "error"):
		return None
	method = data.get("method") or data.get("job_name") or data.get("scheduled_job_type") or data.get("job")
	return {
		"method": method or "unknown",
		"site": data.get("site"),
		"timestamp": parse_log_timestamp(str(data.get("timestamp") or data.get("asctime") or "")),
		"error": data.get("exception") or data.get("error") or data.get("message"),
	}


def parse_line(line):
	"""Return `{method, site, timestamp, error}` for a failed-job line, else None."""
	line = line.rstrip("\n")
	if "{" in line:
		parsed = _parse_json(line)
		if parsed:
			parsed["timestamp"] = parsed["timestamp"] or parse_log_timestamp(line)
			return parsed

	m = _SCHEDULED_FAILED_RE.search(line)
	if m:
		return {"method": m.group("method"), "site": m.group("site"), "timestamp": parse_log_timestamp(line), "error": None}

	m = _ENQUEUE_RE.search(line)
	if m:
		return {"method": "enqueue_events", "site": m.group("site"), "timestamp": parse_log_timestamp(line), "error": None}

	if ("background_jobs" in line and _FAILED_WORD_RE.search(line)) or _GENERIC_RE.search(line):
		return {
			"method": _dotted_method(line) or "unknown",
			"site": None,
			"timestamp": parse_log_timestamp(line),
			"error": None,
		}
	return None


class SchedulerParser:
	def __init__(self, host, bench=None, site=None, log_file=None, **_):
		self.host, self.bench, self.site, self.log_file = host, bench, site, log_file
		self.failures = {}

	def feed(self, line):
		parsed = parse_line(line)
		if not parsed:
			return
		site = parsed["site"] or self.site
		key = (site, parsed["method"])
		f = self.failures.get(key)
		if not f:
			f = self.failures[key] = {"count": 0, "timestamp": parsed["timestamp"], "samples": [], "error": parsed["error"]}
		f["count"] += 1
		if len(f["samples"]) < MAX_SAMPLES:
			f["samples"].append(line.strip()[:1000])

	def drain(self, now=None):
		ts = utc_iso(now)
		events = []
		for (site, method), f in self.failures.items():
			payload = {
				"method": method,
				"job": method.rsplit(".", 1)[-1],
				"site": site,
				"count": f["count"],
				"sample_lines": f["samples"],
				"log_file": self.log_file,
			}
			if f["error"]:
				payload["error"] = str(f["error"])[:2000]
			events.append(
				event(
					"cron_failure",
					"scheduled_job",
					"critical",
					f["timestamp"] or ts,
					self.host,
					f"Scheduled job failed: {method}"[:140],
					fingerprint(self.bench, site, method),
					payload,
					source="scheduler",
					bench=self.bench,
					site=site,
				)
			)
		self.failures = {}
		return [], events
