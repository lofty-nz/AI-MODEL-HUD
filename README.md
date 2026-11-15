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
- `projects.py`: the **Projects** tab. A card for each little project, from `<docs_dir>/projects.json` on the docs hub (groups, then projects with `places` and `folders`). **▶ Open** goes straight to a project's only place (or the one remembered in this browser) and otherwise asks where: a web link; a project folder shared by a PC's hub on `files_port` (default 8091, its own address so project pages can't use the hub's API; config `"serve": {key: folder}`, web file types only, no hidden files); a program started on a PC's screen (config `"launchers"`: `{"desktop": "~/Desktop/x.desktop"}` or `{"exec": [...], "cwd"}`, optional `"running"` pgrep pattern so it isn't started twice; run through `systemd-run --user --scope` so it outlives a hub restart); or a folder opened in VS Code / Files (only under config `"folder_roots"`). The page can change names, icons, groups, web links, GitHub links and hidden; commands and folders only come from each PC's own config. API: `GET /api/projects`, `POST /api/projects/save {project}|{delete}` (docs hub), `POST /api/launch {machine, run}|{machine, folder, app: code|files}` (forwarded to the PC's `/api/local/launch`). Launch output goes to `~/.local/state/aihub/launch.log`.
- `checklists.py`: the **Checklists** tab. Test runs and to-do lists kept in `<docs_dir>/checklists`: `<id>.json` is the checklist (sections of steps, each with what to do and what should happen, written by hand or by an AI) and `<id>.state.json` is what was ticked and written. Each step can be ticked, marked Works / Problem / Skipped and given a reply (typed or 🎤 spoken) with screenshots (📋 Paste screenshot, Ctrl+V, 📷 Add picture or a dropped file; kept in `<docs_dir>/checklists/shots/<id>/`, removed ones moved to `.removed/` there); an answer can be written under each step from the command line: `python3 checklists.py` lists them, `show <id> [--all]` prints the problems and replies, `answer <id> <step> "text"` writes an answer. API on the docs hub: `GET /api/checklists`, `GET /api/checklists/<id>`, `POST /api/checklists/save {list, item, done, result, reply, by}`, `POST /api/checklists/shot?list=&item=&by=` (the body is the picture; PNG, JPEG, WebP or GIF, checked by its bytes, 15 MB, 20 per step), `POST /api/checklists/shot/delete {list, item, name}`, `GET /api/checklists/<id>/shots/<name>`. `show` lists each step's screenshots by full path. `archive <id> <new id> --label "Run 1"` saves a read-only copy of a run (shown under Previous runs, linked from the checklist), and `checklist_pdf.py` (main PC, with ~/pwvenv) prints a run as a PDF with its screenshots; `run-pdf <id> <saved id> "<file>"` links that PDF. The file format is described at the top of `checklists.py`.
- `aidoor.py`: the **📂 File access** tab (security checklist c1). A small root helper (`/usr/local/sbin/aidoor`, settings `/etc/aidoor/config.json`, log `/var/log/aidoor.log`) opens a **door**: one of your folders appears inside a running container at `/doors/<folder>`, read-only or read-write, without a restart, until its time is up or you close it. Every door closes on reboot, and every open and close is logged. You pick the folder in a browser over your home folder and `/mnt`; aidoor refuses hidden folders, the whole home folder, anything outside those roots, and a `"deny"` list (globs allowed) of folders holding other programs' keys and data, or any folder containing one. A container may have doors once its compose file mounts `/srv/aidoors/<name>:/doors:ro,rslave`; without it the tab marks the container **NO /doors YET**. It also does **Restart PC** and the PCIe slot speed. Install with `sudo bash install-aidoor.sh`; the hub may run only the listed jobs without a password (`/etc/sudoers.d/60-aidoor`). The Open WebUI tool `openwebui_door_files.py` (installed by `openwebui_install_tool.py`) lists and reads door files and writes into a read-write door. Tests: `tests/test_aidoor.sh` (on the PC, with a throwaway container) and `tests/test_door_files_tool.py`.
- Settings: `~/.config/aihub/config.json` with `name`, `lan_ip`, `peers` (`name`, `url`, `mac`), `links`, `docs_dir` or `docs_url`, and `telegram` (true on the one hub that runs the bot).

It needs `sysmon.py` from `~/Documents/sysmon` for the CPU and GPU readings.
