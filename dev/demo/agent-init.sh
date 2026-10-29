#!/usr/bin/env bash
# Register the demo agent with the local Wolf site (once), point it at the demo logs, run it.
set -euo pipefail
cd /home/frappe/frappe-bench
CONFIG="$WOLF_AGENT_CONFIG"

until [ -s sites/.wolf-demo-token ] && curl -fsS http://frontend:8080/api/method/ping >/dev/null 2>&1; do
	echo "waiting for wolf.localhost"; sleep 5
done

if [ ! -f "$CONFIG" ]; then
	token=$(cat sites/.wolf-demo-token)
	until env/bin/wolf-agent register --url http://frontend:8080 --token "$token" \
		--bench /home/frappe/frappe-bench; do
		echo "register failed, retrying"; sleep 5
	done
	env/bin/python - "$CONFIG" <<'PY'
import json, sys
path = sys.argv[1]
cfg = json.load(open(path))
cfg["auth_log"] = False
cfg["push_interval"] = 15
cfg["spool_dir"] = "/home/frappe/wolf-agent/spool"
for bench in cfg["benches"]:
    bench["nginx_access_log"] = "/var/log/wolf-nginx/access.log"
json.dump(cfg, open(path, "w"), indent=2)
PY
fi

exec env/bin/wolf-agent run -v
