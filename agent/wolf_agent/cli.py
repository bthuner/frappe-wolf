"""`wolf-agent` command line: register, run, check, discover, mark-deployment, test-fallback."""

import argparse
import json
import logging
import os
import platform
import signal
import socket
import sys
import threading

from . import __version__
from .collector import Collector
from .config import DEFAULTS, config_path, load_config, save_config
from .discovery import discover_benches
from .fallback import FallbackError
from .shipper import AuthError, RejectedError, Shipper, TransportError, register
from .watchdog import STATE_FILE, Watchdog, assess

log = logging.getLogger("wolf_agent")
EXIT_AUTH = 2  # systemd unit sets RestartPreventExitStatus=2


def _os_name():
	try:
		with open("/etc/os-release") as f:
			for line in f:
				if line.startswith("PRETTY_NAME="):
					return line.split("=", 1)[1].strip().strip('"')
	except OSError:
		pass
	return platform.platform()


def _load(args):
	path = config_path(args.config)
	try:
		return path, load_config(path)
	except FileNotFoundError:
		sys.exit(f"config not found: {path} (run `wolf-agent register` first)")
	except ValueError as e:
		sys.exit(f"invalid config {path}: {e}")


def cmd_discover(args):
	print(json.dumps(discover_benches(args.bench or None), indent=2))


def cmd_register(args):
	path = config_path(args.config)
	try:
		cfg = load_config(path)
	except (FileNotFoundError, ValueError):
		cfg = dict(DEFAULTS)
	cfg["url"] = args.url.rstrip("/")
	cfg["hostname"] = args.hostname or cfg.get("hostname") or socket.getfqdn() or socket.gethostname()
	cfg["verify_tls"] = not args.insecure
	cfg["benches"] = discover_benches(args.bench or None)
	if not cfg["benches"]:
		log.warning("no bench found; only host metrics and auth logs will be collected")

	try:
		resp = register(cfg["url"], args.token, cfg["hostname"], cfg["benches"], _os_name(), cfg["verify_tls"])
	except (AuthError, RejectedError, TransportError) as e:
		sys.exit(f"registration failed: {e}")
	if not resp.get("agent_id") or not resp.get("agent_secret"):
		sys.exit(f"registration failed: unexpected response {resp!r}")

	cfg["agent_id"] = resp["agent_id"]
	cfg["agent_secret"] = resp["agent_secret"]
	cfg["push_interval"] = int(resp.get("push_interval") or cfg.get("push_interval") or 30)
	save_config(path, cfg)
	print(f"registered as {cfg['agent_id']} with {cfg['url']}; config written to {path}")
	for bench in cfg["benches"]:
		print(f"  bench {bench['name']} at {bench['path']}: {len(bench['sites'])} site(s)")


def cmd_check(args):
	path = config_path(args.config)
	cfg = _load(args)[1] if os.path.exists(path) else _ephemeral_config(args)
	if args.bench:
		cfg["benches"] = discover_benches(args.bench)
	collector = Collector(cfg, state_path=None, from_start=args.from_start)
	metrics, events = collector.collect()
	collector.close()
	body = {"hostname": cfg["hostname"], "agent_version": __version__, "metrics": metrics, "events": events}
	print(json.dumps(body, indent=2))
	# stdout stays the payload; the watchdog report goes to stderr
	for line in _watchdog_report(cfg, live=args.status):
		print(line, file=sys.stderr)


def _watchdog(cfg):
	return Watchdog(cfg, state_path=os.path.join(cfg["spool_dir"], STATE_FILE))


def _watchdog_report(cfg, live=False):
	try:
		watchdog = _watchdog(cfg)
		lines = watchdog.summary()
	except Exception as e:
		return [f"watchdog: cannot read state: {e}"]
	if live and not (cfg.get("url") and cfg.get("agent_id") and cfg.get("agent_secret")):
		lines.append("live status: not registered")
	elif live:
		try:
			health = Shipper(cfg).status().get("health")
		except (AuthError, RejectedError, TransportError) as e:
			lines.append(f"live status: unavailable ({e})")
		else:
			if isinstance(health, dict):
				problems = assess(health, watchdog.stale_minutes)
				lines.append(f"live status: {'; '.join(problems) if problems else 'healthy'}")
				lines.append("health: " + json.dumps(health, sort_keys=True))
			else:
				lines.append("live status: the Wolf site returned no health")
	return lines


def cmd_test_fallback(args):
	_, cfg = _load(args)
	watchdog = _watchdog(cfg)
	try:
		delivered = watchdog.test()
	except FallbackError as e:
		sys.exit(f"fallback failed: {e}")
	if not delivered:
		sys.exit("no fallback channel configured: set watchdog.webhook_url and/or watchdog.smtp in the config")
	print(f"test message sent via {', '.join(delivered)}")


def _ephemeral_config(args):
	cfg = dict(DEFAULTS)
	cfg["hostname"] = socket.gethostname()
	cfg["benches"] = discover_benches(args.bench or None)
	return cfg


def cmd_mark_deployment(args):
	_, cfg = _load(args)
	shipper = Shipper(cfg)
	try:
		shipper.mark_deployment(args.bench, args.site, args.note, args.source)
	except (AuthError, RejectedError, TransportError) as e:
		sys.exit(f"could not record deployment: {e}")
	print(f"deployment recorded for bench {args.bench}")


def cmd_run(args):
	_, cfg = _load(args)
	if not (cfg.get("url") and cfg.get("agent_id") and cfg.get("agent_secret")):
		sys.exit("config is missing url/agent_id/agent_secret; run `wolf-agent register`")
	state_path = os.path.join(cfg["spool_dir"], "tail-state.json")
	collector = Collector(cfg, state_path=state_path)
	shipper = Shipper(cfg)
	watchdog = _watchdog(cfg)

	stop = threading.Event()
	for sig in (signal.SIGTERM, signal.SIGINT):
		signal.signal(sig, lambda *_: stop.set())

	log.info("wolf-agent %s started: %d log source(s), pushing to %s", __version__, len(collector.sources), cfg["url"])
	try:
		while not stop.is_set():
			metrics, events = collector.collect()
			try:
				ok = shipper.send(metrics, events)
			except AuthError as e:
				log.error("Wolf App rejected agent %s credentials (%s); re-register this host", cfg["agent_id"], e)
				watchdog.fatal(f"the Wolf site rejected agent {cfg['agent_id']} credentials ({e}); re-register this host")
				return EXIT_AUTH
			watchdog.observe(ok, shipper.health)
			stop.wait(shipper.push_interval)
	finally:
		collector.close()
	log.info("wolf-agent stopped")
	return 0


def build_parser():
	p = argparse.ArgumentParser(prog="wolf-agent", description="Frappe Wolf host agent")
	p.add_argument("--config", help="config path (default $WOLF_AGENT_CONFIG or /etc/wolf-agent/config.json)")
	p.add_argument("-v", "--verbose", action="store_true")
	p.add_argument("--version", action="version", version=__version__)
	sub = p.add_subparsers(dest="command", required=True)

	r = sub.add_parser("register", help="register this host with a Wolf site and write the config")
	r.add_argument("--url", required=True, help="Wolf site URL, e.g. https://wolf.example.com")
	r.add_argument("--token", required=True, help="registration token from Wolf Settings")
	r.add_argument("--bench", action="append", help="bench path (repeatable; default: auto-discover)")
	r.add_argument("--hostname", help="override the reported hostname")
	r.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
	r.set_defaults(func=cmd_register)

	sub.add_parser("run", help="run the agent loop").set_defaults(func=cmd_run)

	c = sub.add_parser("check", help="collect once and print the payload without sending")
	c.add_argument("--from-start", action="store_true", help="parse logs from the beginning instead of EOF")
	c.add_argument("--bench", action="append", help="bench path when no config exists")
	c.add_argument("--status", action="store_true", help="also ask the Wolf site for its health (read-only)")
	c.set_defaults(func=cmd_check)

	d = sub.add_parser("discover", help="print benches found on this host")
	d.add_argument("--bench", action="append", help="bench path to inspect")
	d.set_defaults(func=cmd_discover)

	m = sub.add_parser("mark-deployment", help="record a deployment on the Wolf timeline")
	m.add_argument("--bench", required=True)
	m.add_argument("--site")
	m.add_argument("--note")
	m.add_argument("--source", default="manual", choices=["manual", "pilot", "migrate"])
	m.set_defaults(func=cmd_mark_deployment)

	sub.add_parser(
		"test-fallback", help="send a test message through the watchdog fallback channel(s)"
	).set_defaults(func=cmd_test_fallback)
	return p


def main(argv=None):
	args = build_parser().parse_args(argv)
	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.INFO,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)
	return args.func(args) or 0


if __name__ == "__main__":
	sys.exit(main())
