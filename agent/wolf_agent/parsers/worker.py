"""RQ / Frappe worker log parser: crashes, OOM kills and failed jobs → `worker_failure`."""

import re

from ..util import event, fingerprint, parse_log_timestamp, utc_iso

_RULES = [
	# (category, severity, regex)
	("worker_crash", "critical", re.compile(r"Work-horse terminated unexpectedly", re.IGNORECASE)),
	(
		"worker_killed",
		"critical",
		re.compile(r"\bKilled\b|\bsignal 9\b|\bOut of memory\b|\boom-kill", re.IGNORECASE),
	),
	("job_failed", "warning", re.compile(r"exception raised while executing \(?(?P<method>[^)\s]+)")),
	("job_failed", "warning", re.compile(r"\bjob\b.*\bfailed\b", re.IGNORECASE)),
]
_JOB_ID_RE = re.compile(r"\[?Job (?P<job_id>[\w-]+)\]?")
_DOTTED_RE = re.compile(r"\b[a-z_]\w*(?:\.\w+){1,}\b")
MAX_SAMPLES = 3


def parse_line(line):
	"""Return `{category, severity, method, job_id, timestamp}` or None."""
	line = line.rstrip("\n")
	for category, severity, regex in _RULES:
		m = regex.search(line)
		if not m:
			continue
		method = m.groupdict().get("method")
		if not method and category == "job_failed":
			dotted = _DOTTED_RE.findall(line)
			method = dotted[0] if dotted else None
		job = _JOB_ID_RE.search(line)
		return {
			"category": category,
			"severity": severity,
			"method": method,
			"job_id": job.group("job_id") if job else None,
			"timestamp": parse_log_timestamp(line),
		}
	return None


class WorkerParser:
	def __init__(self, host, bench=None, site=None, log_file=None, **_):
		self.host, self.bench, self.site, self.log_file = host, bench, site, log_file
		self.groups = {}

	def feed(self, line):
		parsed = parse_line(line)
		if not parsed:
			return
		key = (parsed["category"], parsed["method"])
		g = self.groups.get(key)
		if not g:
			g = self.groups[key] = {"count": 0, "parsed": parsed, "samples": [], "job_ids": []}
		g["count"] += 1
		if parsed["job_id"] and len(g["job_ids"]) < 10:
			g["job_ids"].append(parsed["job_id"])
		if len(g["samples"]) < MAX_SAMPLES:
			g["samples"].append(line.strip()[:1000])

	def drain(self, now=None):
		ts = utc_iso(now)
		events = []
		titles = {
			"worker_crash": "Worker crashed (work-horse terminated)",
			"worker_killed": "Worker process killed",
			"job_failed": "Background job failed",
		}
		for (category, method), g in self.groups.items():
			p = g["parsed"]
			title = titles[category] + (f": {method}" if method else "")
			events.append(
				event(
					"worker_failure",
					category,
					p["severity"],
					p["timestamp"] or ts,
					self.host,
					title[:140],
					fingerprint(self.bench, category, method),
					{
						"method": method,
						"count": g["count"],
						"job_ids": g["job_ids"],
						"sample_lines": g["samples"],
						"log_file": self.log_file,
					},
					source="worker",
					bench=self.bench,
					site=self.site,
				)
			)
		self.groups = {}
		return [], events
