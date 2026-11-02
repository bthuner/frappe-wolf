#!/bin/sh
# wolf-block-fail2ban IP SECONDS: ban IP for SECONDS (60 to 604800) in fail2ban's `wolf` jail.
# wolf-block-fail2ban --sweep: unban the addresses whose time is over (after a reboot).
#
# The fail2ban variant of wolf-block (agent/README.md, "Response actions"). Install it as root,
# with the jail (agent/fail2ban/wolf.local), and allow the agent to run it, and nothing else:
#   install -m 0755 -o root -g root wolf-block-fail2ban.sh /usr/local/sbin/wolf-block-fail2ban
#   echo 'wolf-agent ALL=(root) NOPASSWD: /usr/local/sbin/wolf-block-fail2ban' > /etc/sudoers.d/wolf-block
# then in the agent config:
#   "response": {"enabled": true, "command": ["sudo", "-n", "/usr/local/sbin/wolf-block-fail2ban", "{ip}", "{seconds}"]}
#
# fail2ban has no per-ban duration: the jail's bantime is the longest (7 days), and the unban is
# scheduled here, with a transient systemd timer. Each expiry is also written to $STATE, so
# --sweep (from cron or a timer, and at boot) unbans what a reboot's lost timers did not.
set -eu

# fixed here, never read from the environment: this runs as root for the agent
JAIL=wolf
STATE=/var/lib/wolf-block/fail2ban.state
CLIENT=fail2ban-client

sweep() {
	[ -f "$STATE" ] || return 0
	now=$(date +%s)
	kept="$STATE.tmp"
	: >"$kept"
	while read -r addr until; do
		[ -n "$addr" ] || continue
		if [ "$until" -le "$now" ]; then
			"$CLIENT" set "$JAIL" unbanip "$addr" >/dev/null 2>&1 || true
			echo "unbanned $addr"
		else
			echo "$addr $until" >>"$kept"
		fi
	done <"$STATE"
	mv "$kept" "$STATE"
}

if [ "${1:-}" = "--sweep" ] && [ "$#" -eq 1 ]; then
	sweep
	exit 0
fi
if [ "$#" -ne 2 ]; then
	echo "usage: wolf-block-fail2ban IP SECONDS | wolf-block-fail2ban --sweep" >&2
	exit 2
fi
ip="$1"
seconds="$2"

case "$seconds" in
'' | *[!0-9]*)
	echo "seconds must be a whole number" >&2
	exit 2
	;;
esac
if [ "$seconds" -lt 60 ] || [ "$seconds" -gt 604800 ]; then
	echo "seconds must be between 60 and 604800" >&2
	exit 2
fi
case "$ip" in
'' | *[!0-9a-fA-F:.]*)
	echo "invalid address" >&2
	exit 2
	;;
esac

if ! "$CLIENT" status "$JAIL" >/dev/null 2>&1; then
	echo "fail2ban has no jail '$JAIL': install agent/fail2ban/wolf.local and reload fail2ban" >&2
	exit 1
fi

sweep >/dev/null
"$CLIENT" set "$JAIL" banip "$ip" >/dev/null
until=$(($(date +%s) + seconds))
mkdir -p "$(dirname "$STATE")"
grep -v "^$ip " "$STATE" 2>/dev/null >"$STATE.tmp" || true
echo "$ip $until" >>"$STATE.tmp"
mv "$STATE.tmp" "$STATE"

unban="no timer: --sweep unbans it"
if command -v systemd-run >/dev/null 2>&1; then
	unit="wolf-unban-$(echo "$ip" | tr ':.' '--')-$until"
	if systemd-run --quiet --unit "$unit" --on-active="${seconds}s" --timer-property=AccuracySec=10s \
		"$(command -v "$CLIENT")" set "$JAIL" unbanip "$ip" >/dev/null 2>&1; then
		unban="unban scheduled ($unit)"
	fi
fi
echo "banned $ip for ${seconds}s in fail2ban jail $JAIL; $unban"
