"""Multi-line Python traceback grouping (frappe.web.log, web.error.log, bench.log...)."""

import re

from ..util import event, fingerprint, parse_log_timestamp, utc_iso

TRACEBACK_START = "Traceback (most recent call last):"
CHAIN_MARKERS = (
	"During handling of the above exception",
	"The above exception was the direct cause",
)
_EXC_RE = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")
_ROUTE_RE = re.compile(r"Error handling request (?P<route>\S+)")
MAX_BLOCK_LINES = 500
MAX_TRACEBACK_CHARS = 8000


class TracebackCollector:
	"""Groups lines into complete traceback blocks, following exception chains."""

	def __init__(self):
		self.block = None
		self.context = None
		self.pending = False  # exception line seen, waiting to see if a chain follows
		self.last_line = None

	def feed(self, line):
		"""Feed one line, return a list of finished blocks."""
		done = []
		if self.block is not None and self.pending:
			if not line.strip():
				self.block["lines"].append(line)
				return done
			if line.startswith(CHAIN_MARKERS):
				self.block["lines"].append(line)
				self.pending = False
				return done
			done.append(self._finish())

		if self.block is not None:
			self.block["lines"].append(line)
			if line.strip() and not line[:1].isspace() and not line.startswith(TRACEBACK_START):
				self.pending = True
			if len(self.block["lines"]) >= MAX_BLOCK_LINES:
				done.append(self._finish())
			return done

		if TRACEBACK_START in line:
			self.block = {"lines": [TRACEBACK_START], "prefix": line, "context": self.last_line}
			self.pending = False
		else:
			self.last_line = line
		return done

	def flush(self, force=False):
		"""Return blocks that are complete (or everything if `force`)."""
		if self.block is not None and (self.pending or force):
			return [self._finish()]
		return []

	def _finish(self):
		block = self.block
		self.block = None
		self.pending = False
		return analyze_block(block)


def analyze_block(block):
	lines = block["lines"]
	exc_type, message, last_frame = "UnknownError", "", None
	for line in lines:
		if line.startswith("  File "):
			last_frame = line.strip()
		elif line and not line[:1].isspace() and not line.startswith(TRACEBACK_START) and not line.startswith(CHAIN_MARKERS):
			m = _EXC_RE.match(line.strip())
			if m:
				exc_type, message = m.group("exc"), (m.group("msg") or "").strip()
			else:
				exc_type, message = "UnknownError", line.strip()

	route = None
	timestamp = None
	for candidate in (block.get("prefix"), block.get("context")):
		if not candidate:
			continue
		timestamp = timestamp or parse_log_timestamp(candidate)
		m = _ROUTE_RE.search(candidate)
		if m and not route:
			route = m.group("route").split("?", 1)[0]

	text = "\n".join(lines)
	if len(text) > MAX_TRACEBACK_CHARS:
		text = text[:2000] + "\n...\n" + text[-(MAX_TRACEBACK_CHARS - 2005):]
	return {
		"exception": exc_type,
		"message": message,
		"last_frame": last_frame,
		"route": route,
		"timestamp": timestamp,
		"traceback": text,
	}


class TracebackParser:
	"""Emits `error`/`traceback` events, one per distinct fingerprint per interval."""

	def __init__(self, host, bench=None, site=None, log_file=None, **_):
		self.host, self.bench, self.site, self.log_file = host, bench, site, log_file
		self.collector = TracebackCollector()
		self.groups = {}

	def feed(self, line):
		for block in self.collector.feed(line.rstrip("\n")):
			self._add(block)

	def _add(self, block):
		fp = fingerprint(block["exception"], block["last_frame"])
		group = self.groups.get(fp)
		if group:
			group["count"] += 1
			return
		self.groups[fp] = {"count": 1, "block": block}

	def drain(self, now=None):
		for block in self.collector.flush():
			self._add(block)
		now_iso = utc_iso(now)
		events = []
		for fp, group in self.groups.items():
			b = group["block"]
			title = f"{b['exception']}: {b['message']}" if b["message"] else b["exception"]
			payload = {
				"exception": b["exception"],
				"message": b["message"],
				"last_frame": b["last_frame"],
				"traceback": b["traceback"],
				"count": group["count"],
				"log_file": self.log_file,
			}
			if b["route"]:
				payload["route"] = b["route"]
			events.append(
				event(
					"error",
					"traceback",
					"warning",
					b["timestamp"] or now_iso,
					self.host,
					title[:140],
					fp,
					payload,
					source="frappe",
					bench=self.bench,
					site=self.site,
				)
			)
		self.groups = {}
		return [], events
