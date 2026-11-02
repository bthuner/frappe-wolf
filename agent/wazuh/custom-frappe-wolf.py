"""Wazuh → Frappe Wolf integration (standard library only).

Wazuh's integration daemon runs it for each alert at or above the <level> of its
<integration> block, with: the alert file, the <api_key> ("AGENT_ID:AGENT_SECRET" of a Wolf
Agent registered with kind "integration") and the <hook_url> (the Wolf site's URL). It posts the
alert as it is to frappe_wolf.api.wazuh.alert, which maps it to a hids_alert event.

Alerts of the frappe_wolf group (Wazuh's alerts on Wolf's own SIEM export) are not sent back.
Exit code 0 when Wolf took the alert, 1 otherwise; errors go to integrations.log.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "/api/method/frappe_wolf.api.wazuh.alert"
TIMEOUT = 10
LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "integrations.log")


def log(message):
	line = f"{time.strftime('%Y/%m/%d %H:%M:%S')} custom-frappe-wolf: {message}\n"
	try:
		with open(LOG, "a") as f:
			f.write(line)
	except OSError:
		sys.stderr.write(line)


def url_for(hook_url):
	hook_url = hook_url.rstrip("/")
	return hook_url if hook_url.endswith(ENDPOINT) else hook_url + ENDPOINT


def send(alert, api_key, hook_url, opener=urllib.request.urlopen):
	"""(ok, detail)."""
	agent_id, sep, secret = (api_key or "").partition(":")
	if not sep or not agent_id or not secret:
		return False, "api_key must be AGENT_ID:AGENT_SECRET"
	if "frappe_wolf" in ((alert.get("rule") or {}).get("groups") or []):
		return True, "skipped: an alert on Wolf's own export"
	req = urllib.request.Request(
		url_for(hook_url), data=json.dumps(alert).encode("utf-8"), method="POST",
		headers={"Content-Type": "application/json", "User-Agent": "frappe-wolf-wazuh/1",
		         "X-Wolf-Agent-Id": agent_id, "X-Wolf-Agent-Secret": secret},
	)  # fmt: skip
	try:
		with opener(req, timeout=TIMEOUT) as resp:
			body = json.loads(resp.read() or b"{}")
	except urllib.error.HTTPError as e:
		try:
			detail = e.read().decode("utf-8", "replace")[:200]
		finally:
			e.close()
		return False, f"HTTP {e.code}: {detail}"
	except (OSError, ValueError) as e:
		return False, str(e)
	message = body.get("message") or {}
	return True, f"stored as {message.get('event')}" if message.get("stored") else f"not stored: {message.get('reason')}"


def main(argv):
	if len(argv) < 4:
		log("usage: custom-frappe-wolf <alert file> <api_key> <hook_url>")
		return 1
	try:
		with open(argv[1]) as f:
			alert = json.load(f)
	except (OSError, ValueError) as e:
		log(f"cannot read the alert {argv[1]}: {e}")
		return 1
	ok, detail = send(alert, argv[2], argv[3])
	if not ok:
		log(f"rule {(alert.get('rule') or {}).get('id')}: {detail}")
	return 0 if ok else 1


if __name__ == "__main__":
	sys.exit(main(sys.argv))
