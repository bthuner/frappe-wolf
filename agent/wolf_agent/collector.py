"""Wires config → log sources + parsers, and runs one collection cycle."""

import logging
import os
from datetime import datetime, timezone

from .config import resolve_auth_log
from .metrics import HostMetrics
from .parsers import PARSERS, NginxParser
from .redis_queue import RedisError, queue_metrics
from .tailer import FileTailer, JournalTailer, TailState

log = logging.getLogger(__name__)

# bench logs/ file → parsers applied to it
BENCH_LOGS = {
	"frappe.web.log": ["traceback"],
	"web.error.log": ["traceback"],
	"bench.log": ["traceback"],
	"scheduler.log": ["scheduler"],
	"scheduler.error.log": ["scheduler", "traceback"],
	"worker.log": ["worker"],
	"worker.error.log": ["worker", "traceback"],
}
# sites/<site>/logs/ file → parsers (site-scoped)
SITE_LOGS = {
	"frappe.web.log": ["traceback"],
	"scheduler.log": ["scheduler"],
}


class Source:
	def __init__(self, tailer, parsers):
		self.tailer = tailer
		self.parsers = parsers

	def poll(self):
		lines = self.tailer.read_lines()
		for line in lines:
			for parser in self.parsers:
				parser.feed(line)
		return len(lines)


class Collector:
	def __init__(self, cfg, state_path=None, from_start=False, host_metrics=None):
		self.cfg = cfg
		self.host = cfg["hostname"]
		self.state = TailState(state_path)
		self.from_start = from_start
		self.host_metrics = host_metrics or (HostMetrics(self.host) if cfg.get("collect_host_metrics", True) else None)
		self.sources = []
		self._build()

	def _file_source(self, path, parser_names, **ctx):
		parsers = [PARSERS[name](self.host, log_file=path, **ctx) for name in parser_names]
		tailer = FileTailer(path, self.state.data, from_start=self.from_start)
		self.sources.append(Source(tailer, parsers))

	def _build(self):
		benches = self.cfg.get("benches") or []
		nginx_logs = {}  # path -> {site: bench}; one parser per file even if benches share it

		for bench in benches:
			name = bench["name"]
			logs_dir = bench.get("logs_dir") or os.path.join(bench["path"], "logs")
			for filename, parser_names in BENCH_LOGS.items():
				self._file_source(os.path.join(logs_dir, filename), parser_names, bench=name)
			for site in bench.get("sites") or []:
				site_logs = os.path.join(bench["path"], "sites", site, "logs")
				for filename, parser_names in SITE_LOGS.items():
					self._file_source(os.path.join(site_logs, filename), parser_names, bench=name, site=site)

			access_logs = bench.get("nginx_access_log") or []
			if isinstance(access_logs, str):
				access_logs = [access_logs]
			for path in access_logs:
				mapping = nginx_logs.setdefault(path, {"sites": {}, "benches": set()})
				mapping["benches"].add(name)
				for site in bench.get("sites") or []:
					mapping["sites"][site] = name

		for path, mapping in nginx_logs.items():
			sites = mapping["sites"]
			only_bench = next(iter(mapping["benches"])) if len(mapping["benches"]) == 1 else None
			parser = NginxParser(
				self.host,
				site_benches=sites,
				default_site=next(iter(sites)) if len(sites) == 1 else None,
				default_bench=only_bench,
				log_file=path,
			)
			self.sources.append(Source(FileTailer(path, self.state.data, from_start=self.from_start), [parser]))

		for extra in self.cfg.get("extra_logs") or []:
			parser_name = extra.get("parser", "traceback")
			if parser_name not in PARSERS:
				log.warning("unknown parser %r for %s", parser_name, extra.get("path"))
				continue
			ctx = {k: extra.get(k) for k in ("bench", "site")}
			if parser_name == "nginx":
				ctx = {"default_site": extra.get("site"), "default_bench": extra.get("bench")}
			self._file_source(extra["path"], [parser_name], **ctx)

		auth = resolve_auth_log(self.cfg.get("auth_log"), JournalTailer.available())
		if auth == "journal":
			self.sources.append(Source(JournalTailer(), [PARSERS["auth"](self.host, log_file="journal")]))
		elif auth:
			self._file_source(auth, ["auth"])

	def collect(self, now=None):
		"""Poll every source once; return `(metrics, events)` for this interval."""
		now = now or datetime.now(timezone.utc)
		metrics, events = [], []
		for source in self.sources:
			try:
				source.poll()
			except Exception:
				log.exception("error reading %s", getattr(source.tailer, "path", source.tailer))
			for parser in source.parsers:
				m, e = parser.drain(now)
				metrics += m
				events += e

		if self.host_metrics:
			metrics += self.host_metrics.collect(now)

		for bench in self.cfg.get("benches") or []:
			url = bench.get("redis_queue")
			if not url:
				continue
			try:
				metrics += queue_metrics(url, self.host, bench["name"], now)
			except (OSError, RedisError, ValueError) as e:
				log.warning("queue length for bench %s (%s): %s", bench["name"], url, e)

		self.state.save()
		return metrics, events

	def close(self):
		for source in self.sources:
			source.tailer.close()
