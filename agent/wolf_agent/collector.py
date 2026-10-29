"""Wires config → log sources + parsers, and runs one collection cycle."""

import logging
import os
import time
from datetime import datetime, timezone

from . import pilot, press
from .config import pilot_settings, press_settings, resolve_auth_log
from .discovery import discover_benches, read_bench_toml, resolve_platform
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
	"schedule.error.log": ["traceback"],  # supervisor stderr of `bench schedule`
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
	def __init__(self, cfg, state_path=None, from_start=False, host_metrics=None, clock=time.monotonic):
		self.cfg = cfg
		self.host = cfg["hostname"]
		self.state = TailState(state_path)
		self.from_start = from_start
		self.host_metrics = host_metrics or (HostMetrics(self.host) if cfg.get("collect_host_metrics", True) else None)
		self.sources = []
		self.clock = clock
		self.press = None
		self.press_jobs = None
		self.press_fs = None
		self.benches = list(cfg.get("benches") or [])

		press_cfg = press_settings(cfg)
		if resolve_platform(cfg.get("platform"), press_cfg["agent_dir"]) == "press":
			self._setup_press(press_cfg, state_path)
		self.pilot_tasks = self._setup_pilot(state_path)
		self._build()

	def _setup_pilot(self, state_path):
		settings = pilot_settings(self.cfg)
		if not settings.get("deployments", True):
			return []
		spool = os.path.dirname(state_path) if state_path else None
		watchers = []
		for bench in self.benches:
			if bench.get("platform") != "pilot":
				continue
			watchers.append(
				pilot.PilotTaskWatcher(
					self.host,
					bench,
					state_path=os.path.join(spool, "pilot-tasks.json") if spool else None,
					api_url=settings.get("api_url"),
					token=settings.get("token"),
					jwt_secret=settings.get("jwt_secret"),
					toml_reader=read_bench_toml,
				)
			)
		return watchers

	def _setup_press(self, press_cfg, state_path):
		"""Press benches come and go with every deploy: rediscover them at runtime."""
		self.press = press_cfg
		found = self._discover_press()
		if found or not self.benches:
			self.benches = found
		self._next_discovery = self.clock() + float(press_cfg.get("rediscover_interval") or 60)
		if press_cfg.get("deployments", True):
			spool = os.path.dirname(state_path) if state_path else None
			self.press_jobs = press.PressJobWatcher(
				self.host,
				agent_dir=press_cfg["agent_dir"],
				state_path=os.path.join(spool, "press-jobs.json") if spool else None,
				known_benches=lambda: {b["name"] for b in self.benches},
			)
			self.press_fs = press.PressBenchWatcher(
				self.host, state_path=os.path.join(spool, "press-benches.json") if spool else None
			)

	def _discover_press(self):
		return discover_benches(
			platform="press",
			search_globs=(),
			nginx_default=self.press.get("nginx_access_log"),
			press_agent_dir=self.press["agent_dir"],
		)

	def _rediscover(self):
		if not self.press or self.clock() < self._next_discovery:
			return
		self._next_discovery = self.clock() + float(self.press.get("rediscover_interval") or 60)
		found = self._discover_press()
		if _signature(found) == _signature(self.benches):
			self.benches = found  # same layout; refresh domains etc. without reopening files
			return
		new = {b["name"] for b in found} - {b["name"] for b in self.benches}
		log.info("Press benches changed (%d now, new: %s); rebuilding log sources", len(found), sorted(new) or "-")
		self.close()
		self.sources = []
		self.benches = found
		self._build(fresh_benches=new)

	def _file_source(self, path, parser_names, from_start=None, **ctx):
		parsers = [PARSERS[name](self.host, log_file=path, **ctx) for name in parser_names]
		start = self.from_start if from_start is None else from_start
		tailer = FileTailer(path, self.state.data, from_start=start)
		self.sources.append(Source(tailer, parsers))

	def _build(self, fresh_benches=()):
		benches = self.benches
		nginx_logs = {}  # path -> {site: bench}; one parser per file even if benches share it

		for bench in benches:
			name = bench["name"]
			logs_dir = bench.get("logs_dir") or os.path.join(bench["path"], "logs")
			# a bench created while we run has brand-new logs: read them from the start
			fresh = True if name in fresh_benches else None
			files = dict(BENCH_LOGS)
			files.update(bench.get("log_files") or {})  # e.g. Pilot's per-process logs
			for filename, parser_names in files.items():
				self._file_source(os.path.join(logs_dir, filename), parser_names, from_start=fresh, bench=name)
			for site in bench.get("sites") or []:
				site_logs = os.path.join(bench["path"], "sites", site, "logs")
				for filename, parser_names in SITE_LOGS.items():
					self._file_source(os.path.join(site_logs, filename), parser_names, bench=name, site=site)

			access_logs = bench.get("nginx_access_log") or []
			if isinstance(access_logs, str):
				access_logs = [access_logs]
			for path in access_logs:
				mapping = nginx_logs.setdefault(path, {"sites": {}, "benches": set(), "aliases": {}})
				mapping["benches"].add(name)
				for site in bench.get("sites") or []:
					mapping["sites"][site] = name
				mapping["aliases"].update(bench.get("site_domains") or {})

		for path, mapping in nginx_logs.items():
			sites = mapping["sites"]
			only_bench = next(iter(mapping["benches"])) if len(mapping["benches"]) == 1 else None
			parser = NginxParser(
				self.host,
				site_benches=sites,
				default_site=next(iter(sites)) if len(sites) == 1 else None,
				default_bench=only_bench,
				log_file=path,
				site_aliases=mapping["aliases"],
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
		self._rediscover()
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

		events += self._press_deployments(now)
		for watcher in self.pilot_tasks:
			try:
				events += watcher.poll(now) or []
			except Exception:
				log.exception("collecting Pilot tasks for bench %s", watcher.bench)

		for bench in self.benches:
			url = bench.get("redis_queue")
			if not url:
				continue
			try:
				metrics += queue_metrics(url, self.host, bench["name"], now)
			except (OSError, RedisError, ValueError) as e:
				log.warning("queue length for bench %s (%s): %s", bench["name"], url, e)

		self.state.save()
		return metrics, events

	def _press_deployments(self, now):
		if not self.press_jobs:
			return []
		try:
			events = self.press_jobs.poll(now)
			if events is None:
				# no readable job history: fall back to bench/site moves on disk
				events = self.press_fs.poll(self.benches, now)
			return events
		except Exception:
			log.exception("collecting Press deployments")
			return []

	def close(self):
		for source in self.sources:
			source.tailer.close()


def _signature(benches):
	return sorted((b["name"], tuple(b.get("sites") or ()), b.get("redis_queue") or "") for b in benches)
