"""Response actions approved on the Wolf site, applied on this host (opt-in).

The push response may carry `actions` (protocol §2): `{"id", "action": "block_ip", "ip",
"seconds"}`, each confirmed by a Wolf Manager. With `response.enabled` (off by default) the
agent runs `response.command` for each one, with `{ip}` and `{seconds}` filled in, as a list of
arguments (never through a shell). The default command is `sudo -n /usr/local/sbin/wolf-block`
(agent/wolf-block.sh), the only privileged step. The agent checks every action itself too: a
private, loopback or reserved address, or a duration over a week, is refused whatever the site
says. Each result goes back with the next push as `action_results`.
"""

import ipaddress
import logging
import subprocess

log = logging.getLogger(__name__)

DEFAULT_COMMAND = ["sudo", "-n", "/usr/local/sbin/wolf-block", "{ip}", "{seconds}"]
MIN_SECONDS, MAX_SECONDS = 60, 7 * 24 * 3600


def refuse(action):
	"""None when the action may run here, else why not."""
	if action.get("action") != "block_ip":
		return f"unsupported action {action.get('action')!r}"
	try:
		ip = ipaddress.ip_address(str(action.get("ip") or "").strip())
	except ValueError:
		return "not an IP address"
	if getattr(ip, "ipv4_mapped", None):
		ip = ip.ipv4_mapped
	if not ip.is_global or ip.is_multicast:
		return f"{ip} is not a public address"
	try:
		seconds = int(action.get("seconds"))
	except (TypeError, ValueError):
		return "no duration"
	if not MIN_SECONDS <= seconds <= MAX_SECONDS:
		return f"duration must be {MIN_SECONDS} to {MAX_SECONDS} seconds"
	return None


class Responder:
	def __init__(self, cfg, run=subprocess.run):
		self.enabled = bool(cfg.get("enabled"))
		self.command = list(cfg.get("command") or DEFAULT_COMMAND)
		self.run = run
		self.done = set()  # ids handled in this process, until the site stops sending them

	def handle(self, actions):
		results = []
		for action in actions or []:
			if not isinstance(action, dict) or not action.get("id") or action["id"] in self.done:
				continue
			self.done.add(action["id"])
			results.append({"id": action["id"], **self._apply(action)})
		return results

	def _apply(self, action):
		if not self.enabled:
			return {"ok": False, "detail": "response actions are disabled on this host (agent config response.enabled)"}
		why = refuse(action)
		if why:
			return {"ok": False, "detail": f"refused by the agent: {why}"}
		ip = str(ipaddress.ip_address(str(action["ip"]).strip()))
		args = [part.replace("{ip}", ip).replace("{seconds}", str(int(action["seconds"]))) for part in self.command]
		try:
			done = self.run(args, capture_output=True, text=True, timeout=20)
		except (OSError, subprocess.SubprocessError) as e:
			return {"ok": False, "detail": f"{args[0]}: {e}"}
		output = ((done.stdout or "") + (done.stderr or "")).strip()[-400:]
		if done.returncode == 0:
			log.warning("response action %s applied: %s", action["id"], output)
			return {"ok": True, "detail": output or "applied"}
		return {"ok": False, "detail": f"exit {done.returncode}: {output}"}
