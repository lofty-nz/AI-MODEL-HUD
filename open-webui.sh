#!/bin/bash
# Opens Open WebUI in the browser, first starting it and Ollama if they're stopped.
URL=http://127.0.0.1:3000/

stopped=$(for c in ollama open-webui; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ] || echo "$c"
done)
if [ -n "$stopped" ]; then
  notify-send -i applications-science "Open WebUI" "Starting $(echo $stopped)…" 2>/dev/null
  docker start $stopped >/dev/null || { notify-send -u critical "Open WebUI" "Couldn't start $(echo $stopped)"; exit 1; }
fi

# Wait up to 3 minutes for the page to come up.
for _ in $(seq 180); do
  curl -sf -o /dev/null "$URL" && exec xdg-open "$URL"
  sleep 1
done
notify-send -u critical "Open WebUI" "No answer on $URL after 3 minutes" 2>/dev/null
exit 1
