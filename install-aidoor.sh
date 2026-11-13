#!/bin/bash
# Install or update aidoor, AI Hub's root helper (security checklist c1). Run as root:
#   sudo bash install-aidoor.sh [settings.json]
# The settings file is only used the first time (later the existing /etc/aidoor/config.json is kept).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run it with sudo"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
USER_NAME=${SUDO_USER:-andrew}

install -o root -g root -m 0755 "$HERE/aidoor.py" /usr/local/sbin/aidoor
install -d -o root -g root -m 0755 /etc/aidoor
if [ ! -e /etc/aidoor/config.json ]; then
  if [ -n "${1:-}" ]; then install -o root -g root -m 0644 "$1" /etc/aidoor/config.json
  else printf '{"door_root": "/srv/aidoors", "owner_uid": %s, "allow_roots": ["/home/%s", "/mnt"], "containers": {}, "folders": {}, "pcie_ports": [], "reboot": false}\n' \
         "$(id -u "$USER_NAME")" "$USER_NAME" > /etc/aidoor/config.json; chmod 0644 /etc/aidoor/config.json
  fi
fi
DOOR_ROOT=$(python3 -I -c 'import json; print(json.load(open("/etc/aidoor/config.json")).get("door_root", "/srv/aidoors"))')
install -d -o root -g root -m 0755 "$DOOR_ROOT"
touch /var/log/aidoor.log && chmod 0644 /var/log/aidoor.log

# The hub (running as you) may use these without a password; approve/unapprove/add-container still ask.
SUDOERS=/etc/sudoers.d/60-aidoor
A=/usr/local/sbin/aidoor
cat > "$SUDOERS.tmp" <<EOF
# AI Hub's root helper (security checklist c1): what the hub may run without a password.
$USER_NAME ALL=(root) NOPASSWD: $A list, $A list *, $A open *, $A close *, $A expire, $A reboot, $A reboot *, $A pcie *, $A can *
EOF
chmod 0440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null
mv "$SUDOERS.tmp" "$SUDOERS"
echo "aidoor installed: /usr/local/sbin/aidoor, settings /etc/aidoor/config.json, doors in $DOOR_ROOT, log /var/log/aidoor.log"
