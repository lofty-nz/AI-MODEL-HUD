#!/bin/bash
# Tests for aidoor (run on a PC where it's installed, as the hub's user, with sudo -n allowed).
# Uses a throwaway alpine container "door-test" and the approved folders door-test-files + outbox-open-webui.
A="sudo -n /usr/local/sbin/aidoor"
FAILS=0
ok()   { echo "  PASS  $1"; }
bad()  { echo "  FAIL  $1  -> $2"; FAILS=$((FAILS+1)); }
want() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "wanted '$2', got '$3'"; fi; }
inside() { docker exec door-test sh -c "$1" 2>&1; }

docker rm -f door-test >/dev/null 2>&1
docker run -d --rm --name door-test -v /srv/aidoors/door-test:/doors:ro,rslave alpine:latest sleep 3600 >/dev/null
sleep 1

echo "Open and close"
want "nothing open at the start" "" "$(inside 'ls /doors')"
$A open door-test door-test-files ro --by tester >/dev/null
want "read-only door appears inside the running container" "README.md bugs.csv" "$(inside 'ls /doors/door-test-files' | tr '\n' ' ' | sed 's/ $//')"
inside 'echo x > /doors/door-test-files/x.txt' >/dev/null
want "read-only door refuses writes" "no" "$( [ -e ~/ai-door-test/x.txt ] && echo yes || echo no)"
want "inside it shows as ro" "ro" "$(inside 'grep " /doors/door-test-files " /proc/self/mountinfo' | awk '{print $6}' | cut -d, -f1)"
want "same door again just updates its time" "Already open; its time is updated" "$($A open door-test door-test-files ro --until $(( $(date +%s) + 600 )) 2>&1)"
want "the other way while open is refused" "That door is already open the other way; close it first" "$($A open door-test door-test-files rw 2>&1)"
$A open door-test outbox-open-webui rw --by tester >/dev/null
inside 'echo from-container > /doors/outbox-open-webui/made-inside.txt'
want "read-write door: what the container writes is owned by you" "andrew" "$(stat -c %U ~/ai-outbox/open-webui/made-inside.txt 2>&1)"
want "list shows both doors" "2" "$($A list --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["doors"]))')"
want "list knows the modes" "ro rw" "$($A list --json | python3 -c 'import json,sys; print(" ".join(sorted(d["mode"] for d in json.load(sys.stdin)["doors"])))')"
$A close door-test door-test-files --by tester >/dev/null
$A close door-test outbox-open-webui --by tester >/dev/null
want "closed: gone from inside the container" "" "$(inside 'ls /doors')"
want "closed: nothing left on the host" "" "$(ls /srv/aidoors/door-test)"
want "closing a closed door is fine" "That door is already closed" "$($A close door-test door-test-files 2>&1)"
rm -f ~/ai-outbox/open-webui/made-inside.txt

echo "Time limit"
$A open door-test door-test-files ro --until $(( $(date +%s) + 2 )) >/dev/null
sleep 3
$A expire >/dev/null
want "expire closes a door whose time is up" "" "$(inside 'ls /doors')"
want "the log says why" "time up" "$(tail -1 /var/log/aidoor.log | python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason"))')"

echo "Refusals"
want "unknown container" "nope isn't a container that may have doors" "$($A open nope door-test-files ro 2>&1)"
want "unapproved folder" "secrets isn't an approved folder" "$($A open door-test secrets ro 2>&1)"
case "$($A approve ~/.ssh 2>&1)" in *Hidden*) ok "can't approve ~/.ssh";; *) bad "approve ~/.ssh" "allowed";; esac
case "$($A approve ~/.config/aihub 2>&1)" in *Hidden*) ok "can't approve inside ~/.config";; *) bad "approve ~/.config/aihub" "allowed";; esac
case "$($A approve /etc 2>&1)" in *"Only folders inside"*) ok "can't approve /etc";; *) bad "approve /etc" "allowed";; esac
case "$($A approve ~ 2>&1)" in *"Only folders inside"*) ok "can't approve the whole home folder";; *) bad "approve ~" "allowed";; esac
case "$($A approve /srv/aidoors/door-test 2>&1)" in *"Only folders inside"*|*"doors folder"*) ok "can't approve the doors folder";; *) bad "approve doors folder" "allowed";; esac
mkdir -p ~/aidoor-swap-test && ln -sfn /etc ~/aidoor-link-test
case "$($A approve ~/aidoor-link-test 2>&1)" in *link*) ok "can't approve through a link";; *) bad "approve a link" "allowed";; esac
$A approve ~/aidoor-swap-test --id swap-test >/dev/null
rmdir ~/aidoor-swap-test && ln -sfn /etc ~/aidoor-swap-test
case "$($A open door-test swap-test ro 2>&1)" in *moved*|*link*) ok "folder swapped for a link after approval: refused";; *) bad "swapped folder" "opened"; $A close door-test swap-test >/dev/null;; esac
rm -f ~/aidoor-swap-test ~/aidoor-link-test
$A unapprove swap-test >/dev/null
want "no door was left open by the refusals" "" "$(inside 'ls /doors')"
want "bad door name on close" "No such door" "$($A close door-test '../x' 2>&1)"

echo "Restart and PCIe (read only; nothing restarted)"
$A can reboot; want "can reboot" "0" "$?"
case "$($A pcie 00:1c.0 read-max 2>&1)" in [0-9a-f]*) ok "PCIe read of the listed slot";; *) bad "pcie read" "$($A pcie 00:1c.0 read-max 2>&1)";; esac
case "$($A pcie 00:01.0 read-max 2>&1)" in *"No speed setting"*) ok "other slots refused";; *) bad "other slot" "allowed";; esac
case "$($A pcie 00:1c.0 set 9 2>&1)" in *"Use read-max"*) ok "silly speed refused";; *) bad "pcie set 9" "allowed";; esac

echo "sudo rules"
sudo -n -l /usr/local/sbin/aidoor list >/dev/null 2>&1; want "the hub may run aidoor list without a password" "0" "$?"

docker rm -f door-test >/dev/null 2>&1
echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS failed"
exit $FAILS
