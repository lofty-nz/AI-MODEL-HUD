#!/bin/bash
# Sets the Open WebUI password for an account, straight in its database. Asks for the password without showing it.
EMAIL=${1:?Usage: set-webui-password.sh <Open WebUI account email>}

read -rsp "New Open WebUI password for $EMAIL: " PW; echo
read -rsp "Type it again: " PW2; echo
[ "$PW" = "$PW2" ] || { echo "They don't match. Nothing changed."; exit 1; }
[ ${#PW} -ge 8 ] || { echo "Use at least 8 characters. Nothing changed."; exit 1; }

# The password goes in on stdin, so it never shows in the process list.
printf %s "$PW" | docker exec -i -e EMAIL="$EMAIL" open-webui python3 -c '
import os, sys, sqlite3, bcrypt
pw = sys.stdin.read()
db = sqlite3.connect("/app/backend/data/webui.db")
h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
n = db.execute("update auth set password=? where email=?", (h, os.environ["EMAIL"])).rowcount
db.commit()
print("Password changed. Sign in to Open WebUI with it now." if n else "No account with that email. Nothing changed.")
'
