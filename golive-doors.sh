#!/bin/bash
# 📂 File access (security checklist c1) go-live. Run on the main PC, by Claude.
#   bash golive-doors.sh live       hubs with File access on both PCs; Open WebUI gets /doors + the 📂 Door files tool
#   bash golive-doors.sh check
#   bash golive-doors.sh rollback   hubs back to the login-only version, Open WebUI without /doors, tool removed
# aidoor itself (server /usr/local/sbin/aidoor) is already installed; it does nothing until asked.
set -euo pipefail
TAG=bak-20261113-doors
LIVE=~/Documents/aimon
FILES="aihub.py web/index.html"
NEW="aidoor.py install-aidoor.sh openwebui_door_files.py openwebui_install_tool.py"
COMPOSE=/home/andrew/ai/docker-compose.yml
OWNER=$(ssh server "python3 -c \"import sqlite3; c=sqlite3.connect('file:/home/andrew/ai/open-webui/webui.db?mode=ro', uri=True); print(c.execute(\\\"select id from user where role='admin' order by created_at limit 1\\\").fetchone()[0])\"")
say() { printf '\n== %s\n' "$*"; }

live() {
  say "backups (*.$TAG)"
  for f in $FILES; do cp -p "$LIVE/$f" "$LIVE/$f.$TAG"; done
  ssh server "cd ~/Documents/aimon && for f in $FILES; do cp -p \$f \$f.$TAG; done; cp -p $COMPOSE $COMPOSE.$TAG"

  say "main PC: code (git merge doors)"
  cd "$LIVE" && git merge -q --ff-only doors && git log --oneline -1

  say "server: code"
  for f in $FILES $NEW; do scp -q "$LIVE/$f" "server:Documents/aimon/$f"; done

  say "server: test entries out of aidoor's settings (the throwaway container)"
  ssh server 'docker rm -f door-test >/dev/null 2>&1 || true; sudo -n python3 -I - <<"PY"
import json, os
p = "/etc/aidoor/config.json"
c = json.load(open(p))
c["containers"].pop("door-test", None)
json.dump(c, open(p + ".tmp", "w"), indent=1); os.chmod(p + ".tmp", 0o644); os.replace(p + ".tmp", p)
PY
sudo -n rmdir /srv/aidoors/door-test 2>/dev/null || true'

  say "server: Open WebUI gets its door folder (/doors, read-only, rslave) - recreates the container"
  ssh server "grep -q '/srv/aidoors/open-webui:/doors' $COMPOSE || python3 -I - <<'PY'
p = '$COMPOSE'
s = open(p).read()
old = '      - ./open-webui:/app/backend/data\n'
assert s.count(old) == 1
s = s.replace(old, old + '      - /srv/aidoors/open-webui:/doors:ro,rslave   # AI Hub 📂 File access (aidoor)\n')
open(p, 'w').write(s)
PY
sudo -n mkdir -p /srv/aidoors/open-webui
cd ~/ai && docker compose up -d open-webui 2>&1 | tail -2"

  say "server: wait for Open WebUI, then install the 📂 Door files tool"
  ssh server "for i in \$(seq 1 60); do [ \"\$(docker inspect -f '{{.State.Health.Status}}' open-webui)\" = healthy ] && break; sleep 3; done; docker inspect -f '{{.State.Health.Status}}' open-webui
    docker cp ~/Documents/aimon/openwebui_door_files.py open-webui:/tmp/ && docker cp ~/Documents/aimon/openwebui_install_tool.py open-webui:/tmp/ &&
    docker exec -w /app/backend open-webui python3 /tmp/openwebui_install_tool.py /tmp/openwebui_door_files.py $OWNER"

  say "restart both hubs"
  systemctl --user restart aihub
  ssh server 'systemctl --user restart aihub'
  sleep 4
  check
}

check() {
  KEY=$(cat ~/.config/aihub/hub.key)
  say "checks"
  curl -s -H "X-Hub-Key: $KEY" http://127.0.0.1:8090/api/doors | python3 -c "
import json, sys
for m in json.load(sys.stdin)['machines']:
    print(m['name'], '- File access:', 'yes' if m.get('available') else 'no', '| containers:', {k: v.get('running') for k, v in m.get('containers', {}).items()}, '| folders:', list(m.get('folders', {})), '| open:', len(m.get('doors', [])))"
  ssh server "curl -s -H 'X-Hub-Key: $KEY' http://127.0.0.1:8090/api/local" | python3 -c "import json,sys; j=json.load(sys.stdin); print('server restart allowed:', j.get('can_reboot'), '| PCIe:', [(p['label'], p.get('target'), p.get('error')) for p in j.get('pcie_caps', [])])"
  ssh server 'docker exec open-webui sh -c "ls -ld /doors && grep \" /doors \" /proc/self/mountinfo | cut -d\" \" -f5,6"'
  for s in aihub; do printf 'main PC %s: %s\n' $s "$(systemctl --user is-active $s)"; done
  ssh server 'printf "server aihub: %s | open-webui: %s\n" "$(systemctl --user is-active aihub)" "$(docker inspect -f "{{.State.Health.Status}}" open-webui)"'
}

rollback() {
  say "rollback"
  ssh server "sudo -n aidoor list --json | python3 -c \"import json,sys; [print(d['container'], d['folder']) for d in json.load(sys.stdin)['doors']]\" | while read c f; do sudo -n aidoor close \$c \$f --by rollback; done"
  cd "$LIVE"; for f in $FILES; do [ -e "$f.$TAG" ] && cp -p "$f.$TAG" "$f"; done
  ssh server "cd ~/Documents/aimon && for f in $FILES; do [ -e \$f.$TAG ] && cp -p \$f.$TAG \$f; done
    docker cp ~/Documents/aimon/openwebui_install_tool.py open-webui:/tmp/ && docker exec -w /app/backend open-webui python3 /tmp/openwebui_install_tool.py --remove || true
    [ -e $COMPOSE.$TAG ] && cp -p $COMPOSE.$TAG $COMPOSE && cd ~/ai && docker compose up -d open-webui 2>&1 | tail -1"
  systemctl --user restart aihub; ssh server 'systemctl --user restart aihub'
  echo "Rolled back (git on the main PC still has the merge: git reset --hard pre-doors-20261113 there if wanted)."
}

case "${1:-}" in live) live ;; check) check ;; rollback) rollback ;; *) echo "usage: $0 live|check|rollback"; exit 1 ;; esac
