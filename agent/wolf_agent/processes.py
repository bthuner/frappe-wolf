"""State of each bench's processes, from supervisor or systemd.

`supervisorctl status` is used when it can talk to supervisord (the socket is usually
group-readable for the bench user's group after `bench setup supervisor`). Otherwise
`systemctl list-units` is read for service units whose name contains the bench name.
A process belongs to the bench whose name is its longest matching prefix (supervisor:
`<bench>-web`, `<bench>-workers:<bench>-frappe-short-worker-0`, ...).
Press benches run their processes inside containers and are skipped.
"""

import logging
import shutil
import subprocess

from .util import event, fingerprint, metric, utc_iso

log = logging.getLogger(__name__)

# supervisor states that mean "not serving". STARTING/STOPPING are transitions.
SUPERVISOR_DOWN = {"STOPPED", "EXITED", "BACKOFF", "FATAL", "UNKNOWN"}
SUPERVISOR_STATES = SUPERVISOR_DOWN | {"RUNNING", "STARTING", "STOPPING"}
# states worth an event (STOPPED is usually someone running `bench stop` / a deploy)
ALERT_STATES = {"EXITED", "BACKOFF", "FATAL", "UNKNOWN", "failed", "auto-restart"}


def parse_supervisor(text):
	"""`supervisorctl status` → [(name, state, detail)]."""
	out = []
	for line in (text or "").splitlines():
		parts = line.split(None, 2)
		if len(parts) >= 2 and parts[1] in SUPERVISOR_STATES:
			out.append((parts[0], parts[1], parts[2].strip() if len(parts) > 2 else ""))
	return out


def parse_systemd(text):
	"""`systemctl list-units --plain --no-legend` → [(unit, state, detail)].

	state is the ACTIVE column, or `auto-restart` for a unit stuck in a restart loop.
	"""
	out = []
	for line in (text or "").splitlines():
		parts = line.lstrip("●* ").split(None, 4)
		if len(parts) < 4 or not parts[0].endswith(".service"):
			continue
		unit, _load, active, sub = parts[:4]
		state = "auto-restart" if active == "activating" and sub == "auto-restart" else active
		out.append((unit, state, parts[4] if len(parts) > 4 else sub))
	return out


def is_down(manager, state):
	if manager == "supervisor":
		return state in SUPERVISOR_DOWN
	return state in ("failed", "auto-restart")


def owner(name, bench_names, manager):
	"""Bench a process belongs to: longest bench name that prefixes it (systemd: contains it)."""
	best = None
	for bench in bench_names:
		if manager == "supervisor":
			match = name == bench or name.startswith(bench + "-") or name.startswith(bench + ":")
		else:
			match = bench in name
		if match and (best is None or len(bench) > len(best)):
			best = bench
	return best


class ProcessWatcher:
	def __init__(self, host, manager="auto", which=shutil.which, run=subprocess.run, timeout=10):
		self.host = host
		self.manager = manager or "auto"
		self.which = which
		self.run = run
		self.timeout = timeout
		self.prev = {}
		self.warned = False

	def _cmd(self, args):
		proc = self.run(args, capture_output=True, text=True, timeout=self.timeout)
		return proc.stdout or ""

	def _supervisor(self):
		cli = self.which("supervisorctl")
		if not cli:
			return None
		try:
			rows = parse_supervisor(self._cmd([cli, "status"]))
		except (OSError, subprocess.SubprocessError):
			return None
		return rows or None  # no socket access prints an error and no status line

	def _systemd(self):
		cli = self.which("systemctl")
		if not cli:
			return None
		try:
			text = self._cmd([cli, "list-units", "--type=service", "--all", "--plain", "--no-legend", "--no-pager"])
		except (OSError, subprocess.SubprocessError):
			return None
		return parse_systemd(text)

	def states(self, bench_names):
		"""(manager, [(bench, name, state, detail)]) for processes of the given benches."""
		order = ("supervisor", "systemd") if self.manager == "auto" else (self.manager,)
		for manager in order:
			rows = self._supervisor() if manager == "supervisor" else self._systemd()
			if not rows:
				continue
			matched = []
			for name, state, detail in rows:
				bench = owner(name, bench_names, manager)
				if bench:
					matched.append((bench, name, state, detail))
			if matched:
				return manager, matched
		if not self.warned:
			self.warned = True
			log.info("no supervisor or systemd process found for benches %s; process states are not collected", bench_names)
		return None, []

	def collect(self, benches, now=None):
		ts = utc_iso(now)
		names = [b["name"] for b in benches if b.get("platform") != "press"]
		if not names:
			return [], []
		manager, rows = self.states(names)
		if not manager:
			return [], []
		metrics, events = [], []
		down = dict.fromkeys(names, 0)
		current = {}
		for bench, name, state, detail in rows:
			current[name] = state
			if not is_down(manager, state):
				continue
			down[bench] += 1
			if state in ALERT_STATES and self.prev.get(name) != state:
				events.append(
					event(
						"worker_failure", "process_down", "critical", ts, self.host,
						f"{name} is {state} ({manager})",
						fingerprint(self.host, "process_down", bench, name, state),
						{"process": name, "state": state, "detail": detail[:500], "manager": manager},
						source=manager, bench=bench,
					)
				)
		self.prev = current
		for bench in sorted({b for b, *_ in rows}):
			metrics.append(metric("bench_processes_down", down[bench], ts, self.host, bench=bench, labels={"manager": manager}))
		return metrics, events
