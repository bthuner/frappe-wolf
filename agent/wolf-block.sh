#!/bin/sh
# wolf-block IP SECONDS: drop traffic from IP for SECONDS (60 to 604800) with nftables.
#
# The only privileged step of Wolf's response actions. Install it as root and allow the agent
# to run it, and nothing else, through sudo (agent/README.md, "Response actions"):
#   install -m 0755 -o root -g root wolf-block.sh /usr/local/sbin/wolf-block
#   echo 'wolf-agent ALL=(root) NOPASSWD: /usr/local/sbin/wolf-block' > /etc/sudoers.d/wolf-block
# Entries expire by themselves (nftables set timeout). `nft list table inet wolf` shows them.
set -eu

if [ "$#" -ne 2 ]; then
	echo "usage: wolf-block IP SECONDS" >&2
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
*:*) set_name=blocked6 ;;
*) set_name=blocked4 ;;
esac

if ! nft list table inet wolf >/dev/null 2>&1; then
	nft -f - <<'NFT'
table inet wolf {
	set blocked4 { type ipv4_addr; flags timeout; }
	set blocked6 { type ipv6_addr; flags timeout; }
	chain input {
		type filter hook input priority -10; policy accept;
		ip saddr @blocked4 drop
		ip6 saddr @blocked6 drop
	}
}
NFT
fi
nft add element inet wolf "$set_name" "{ $ip timeout ${seconds}s }"
echo "blocked $ip for ${seconds}s (inet wolf $set_name)"
