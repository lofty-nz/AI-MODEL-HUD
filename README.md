# aimon

An always-on-top desktop panel for Linux that shows which local AI models are running, with START and STOP buttons for each one. It's a companion to the sysmon badge and has the same look.

- **Containers:** every Docker container is a row. START and STOP run `docker start` and `docker stop`.
- **Ollama models:** each installed model is listed under the ollama container. START loads it and keeps it loaded, and STOP unloads it.
- **ComfyUI:** while image models are in VRAM, an UNLOAD row frees them and leaves the container running.
- **Memory:** each row shows VRAM · RAM in GB. Bars at the top show the whole machine's VRAM and RAM.
- **Web pages:** a name in blue with ↗ opens its web page (Open WebUI, ComfyUI, Strata). An Ollama model's link opens Open WebUI with that model picked. A stopped container is started first.
- **Startup:** OPTIONS has "Start AI Model Monitor when I log in" and "Load at startup", which loads one container or Ollama model each time the panel opens.
- **AUTO:** placeholder for automatic mode (start when needed, stop when idle). Not built yet.
- **WEB ↗:** opens the AI Hub web page (`hub_url` in the settings, the server's http://192.168.50.235:8090/ by default).
- **Window:** drag to move, scroll to resize. Click OPTIONS (or the title) for settings and help. Right-click to open a container's web page, unload all models, or quit.

## Run

```
python3 aimon_gui.py
```

Needs Docker (with your user in the `docker` group), `nvidia-smi` and Python 3 with PyGObject (GTK 3) and pycairo.

Settings are saved in `~/.config/aimon-gui/settings.json`. "Start AI Model Monitor when I log in" adds `~/.config/autostart/aimon-gui.desktop`.

## AI Hub (web page)

`aihub.py` serves a web page on port 8090 with the same rows as the panel, for every PC at once, plus CPU and GPU temperatures, quick links, Wake-on-LAN for PCs that are off, and a library of docs. Each PC runs its own copy as a systemd user service (`aihub`). The page asks the hub it was opened from, and that hub adds the other PCs' readings from their hubs.

- `aimon_core.py`: Docker, Ollama and memory reading shared by the panel and the hub (no GTK).
- `web/index.html`: the page.
- `aihub.service`: the user service. Install with `cp aihub.service ~/.config/systemd/user/ && systemctl --user enable --now aihub`.
- `telegram_hub.py`: the **Telegram** tab. One hub (config `"telegram": true`) runs a Telegram bot that other apps on that PC use to message your phone: `POST /api/telegram/send {text, key, buttons, silent, source}`, and `POST /api/telegram/edit {key, text, buttons}` to change the message sent with that key without a new ping. Buttons are link buttons. Paste the bot's token from @BotFather on the tab and link your chat with the 6-digit code (or the tab's t.me link); only linked chats get anything. It uses long polling, so nothing is opened to the internet. Token and chats: `~/.config/aihub/telegram.json` (owner-only); log: `~/.local/state/aihub/telegram-log.json`. Other hubs show that hub's tab (`telegram_url`, default `docs_url`).
- Settings: `~/.config/aihub/config.json` with `name`, `lan_ip`, `peers` (`name`, `url`, `mac`), `links`, `docs_dir` or `docs_url`, and `telegram` (true on the one hub that runs the bot).

It needs `sysmon.py` from `~/Documents/sysmon` for the CPU and GPU readings.
