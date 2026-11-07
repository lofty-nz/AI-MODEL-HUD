#!/usr/bin/env python3
"""Always-on-top panel listing the local AI containers and models, with start and stop buttons.

Each Docker container is a row. Ollama's installed models sit under the ollama container,
and ComfyUI gets an UNLOAD row while it holds a model in VRAM. The dot is green when a
row is running or loaded, amber while it's starting or stopping, and grey when it's off.
The numbers on the right are VRAM, then system RAM.

The AUTO pills are placeholders for automatic mode, which isn't built yet: start a
model when something asks for it, stop it when it has been idle.

Drag to move, scroll to resize. Click the title for settings, right-click for the menu.
"""
import json
import math
import os
import subprocess
import threading
import time

import cairo
import gi
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from aimon_core import (GIB, read_gpu, read_ram, read_rows, run,  # noqa: E402
                        serves_web_page, start_row, stop_row)

CONFIG = os.path.expanduser("~/.config/aimon-gui/settings.json")
DEFAULTS = {"refresh_s": 2.0, "scale": 1.0, "on_top": True, "x": None, "y": None,
            "startup_load": None,   # row key loaded when the panel starts, such as "ollama:qwen3:14b"
            "web": {},              # container name -> whether its port serves a web page, learned as they run
            "hub_url": "http://192.168.50.235:8090/",   # the AI Hub web page (WEB button)
            "mini": False, "hud_scale": 1.0, "hud_x": None, "hud_y": None}   # mini HUD strip and where it sits
MIN_SCALE, MAX_SCALE = 0.7, 2.0
AUTOSTART = os.path.expanduser("~/.config/autostart/aimon-gui.desktop")
FAILED_SHOW_S = 30       # how long a failed start or stop stays on its row
OPEN_WAIT_S = 180        # how long a just-started container gets to put its web page up
STARTUP_WAIT_S = 300     # how long the startup load waits for Docker and Ollama after login

# Panel layout at scale 1, in pixels.
WIDTH = 400
PAD = 14
HEADER_H = 64
ROW_H = 46
SUB_H = 38
BOTTOM = 10
BTN_W, AUTO_W, PILL_H = 62, 48, 24
HUD_H = 30               # mini HUD strip height at hud_scale 1
HUD_ALPHA = 0.6          # its background is see-through
HUD_MAX_NAMES = 5        # running rows named on the strip; the rest are counted
MIN_HUD, MAX_HUD = 0.7, 2.5

BG = (0.08, 0.09, 0.11)
TRACK = (0.17, 0.19, 0.22)
TEXT = (0.90, 0.93, 0.95)
MUTED = (0.55, 0.58, 0.62)
GREEN = (0.25, 0.73, 0.31)
AMBER = (0.86, 0.62, 0.15)
RED = (0.97, 0.32, 0.29)
START_BTN = (0.13, 0.34, 0.55)
STOP_BTN = (0.55, 0.19, 0.18)
BUSY_BTN = (0.22, 0.24, 0.28)
LINK = (0.45, 0.70, 0.98)


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    s["scale"] = min(MAX_SCALE, max(MIN_SCALE, s["scale"]))
    return s


def save_settings(s):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(s, f, indent=2)




class Monitor:
    """Polls Docker, Ollama and the GPU on a background thread, and runs start and stop requests."""

    def __init__(self, settings, on_update):
        self.settings = settings
        self.on_update = on_update
        self.lock = threading.Lock()
        self.rows = []
        self.error = None
        self.memory = {}    # "vram" / "ram" -> (used, total) bytes
        self.pending = {}   # row key -> "starting", "stopping", "opening", or ("failed", message, time)
        self.wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if settings["startup_load"]:
            threading.Thread(target=self.load_at_startup, args=(settings["startup_load"],), daemon=True).start()

    def act(self, row, starting):
        self.background(row["key"], "starting" if starting else "stopping",
                        lambda: (start_row if starting else stop_row)(row))

    def open(self, row):
        """Opens a row's web page, first starting the container that serves it if it's stopped."""
        def work():
            host = self.find(f"docker:{row['web_host']}")
            if host and host["state"] == "off":
                run(["docker", "start", row["web_host"]], timeout=120)
            deadline = time.monotonic() + OPEN_WAIT_S
            while (page := serves_web_page(row["web_port"])) is None and time.monotonic() < deadline:
                time.sleep(1)
            if page is None:
                raise RuntimeError(f"no answer on :{row['web_port']} after {OPEN_WAIT_S // 60} min")
            if not page:
                self.settings["web"][row["web_host"]] = False
                GLib.idle_add(save_settings, self.settings)
                raise RuntimeError(f":{row['web_port']} isn't a web page")
            GLib.idle_add(open_uri, row["url"])

        self.background(row["key"], "opening", work)

    def background(self, key, label, work):
        """Runs work on its own thread, showing label on the row until it finishes, or why it failed."""
        if isinstance(self.pending.get(key), str):
            return

        def run_work():
            try:
                work()
                self.pending.pop(key, None)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
                self.pending[key] = ("failed", str(e) or type(e).__name__, time.monotonic())
            self.wake.set()

        self.pending[key] = label
        GLib.idle_add(self.on_update)
        threading.Thread(target=run_work, daemon=True).start()

    def find(self, key):
        return next((r for r in self.snapshot()[0] if r["key"] == key), None)

    def load_at_startup(self, key):
        """Starts the chosen row once it shows up. An Ollama model only shows up while its container runs,
        so that container is started first. At login Docker may still be starting, hence the wait."""
        deadline = time.monotonic() + STARTUP_WAIT_S
        while time.monotonic() < deadline:
            rows = self.snapshot()[0]
            row = next((r for r in rows if r["key"] == key), None)
            if row:
                if row["state"] == "off":
                    self.act(row, starting=True)
                return
            if key.startswith("ollama@"):
                host = self.find(f"docker:{key[7:].split(':', 1)[0]}")
            elif key.startswith("ollama:"):
                host = next((r for r in rows if r["kind"] == "docker" and "ollama" in r["image"]
                             and r["name"] == "ollama"), None) or \
                       next((r for r in rows if r["kind"] == "docker" and "ollama" in r["image"]), None)
            else:
                host = None
            if host and host["state"] == "off":
                self.act(host, starting=True)
            time.sleep(2)

    def _run(self):
        web = self.settings["web"]
        while True:
            known = dict(web)
            try:
                rows, error = read_rows(web), None
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
                rows, error = [], f"Can't reach Docker: {e}"
            if web != known:
                GLib.idle_add(save_settings, self.settings)
            vram, _ = read_gpu()
            memory = {"ram": read_ram()}
            if vram:
                memory["vram"] = vram
            with self.lock:
                self.rows, self.error, self.memory = rows, error, memory
            GLib.idle_add(self.on_update)
            self.wake.wait(self.settings["refresh_s"])
            self.wake.clear()

    def snapshot(self):
        """Rows with any start, stop or failure in progress laid over them."""
        with self.lock:
            rows, error, memory = [dict(r) for r in self.rows], self.error, dict(self.memory)
        now = time.monotonic()
        for r in rows:
            p = self.pending.get(r["key"])
            if isinstance(p, str):
                r["state"], r["sub"], r["busy"] = "busy", f"{p.capitalize()}…", True
            elif p and now - p[2] < FAILED_SHOW_S:
                r["sub"], r["failed"] = f"Failed: {p[1]}", True
        return rows, error, memory


def open_uri(url):
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
    except GLib.Error:
        subprocess.Popen(["xdg-open", url], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return False


def startup_choices(rows, current):
    """(key, label) for everything the panel can load at startup: containers and Ollama models."""
    choices = [(r["key"], f"{r['name']}  ({'container' if r['kind'] == 'docker' else 'Ollama model'})")
               for r in rows if r["kind"] in ("docker", "ollama")]
    if current and current not in {k for k, _ in choices}:   # e.g. an Ollama model while ollama is stopped
        choices.append((current, current.split(":", 1)[1]))
    return choices


def level_colour(frac):
    return RED if frac >= 0.9 else AMBER if frac >= 0.7 else GREEN


def draw_text(cr, s, x, y, size, colour, bold=False, centred=True, max_w=None, right=False):
    """Draws text vertically centred on y; horizontally centred on x, starting at x, or ending at x.
    Returns its width."""
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string("Sans Bold" if bold else "Sans")
    font.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(font)
    if max_w:
        layout.set_width(int(max_w * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_text(s, -1)
    w, h = layout.get_pixel_size()
    cr.set_source_rgb(*colour)
    cr.move_to(x - w if right else x - w / 2 if centred else x, y - h / 2)
    PangoCairo.show_layout(cr, layout)
    cr.new_path()
    return w


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def panel_size(rows, scale):
    body = sum(SUB_H if r["level"] else ROW_H for r in rows) if rows else ROW_H
    return int(WIDTH * scale), int((HEADER_H + body + BOTTOM) * scale)


def fmt_mem(vram, ram):
    parts = [f"{vram / GIB:.1f}" if vram else "–", f"{ram / GIB:.1f}" if ram else "–"]
    return "" if not vram and not ram else f"{parts[0]} · {parts[1]} GB"


def draw_bar(cr, x, y, w, h, label, used_total):
    used, total = used_total
    draw_text(cr, label, x, y - h * 1.9, h * 2.2, MUTED, bold=True, centred=False)
    draw_text(cr, f"{used / GIB:.1f} / {total / GIB:.0f} GB", x + w, y - h * 1.9, h * 2.2, TEXT, right=True)
    cr.set_source_rgb(*TRACK)
    rounded_rect(cr, x, y, w, h, h / 2)
    cr.fill()
    frac = min(1, used / total)
    if frac > 0.01:
        cr.set_source_rgb(*level_colour(frac))
        rounded_rect(cr, x, y, max(h, w * frac), h, h / 2)
        cr.fill()


def draw_expand_button(cr, x, y, s, pressed):
    """Square button with a window outline: back to the full panel."""
    rounded_rect(cr, x, y, s, s, s * 0.25)
    cr.set_source_rgb(*(tuple(v * 0.8 for v in START_BTN) if pressed else START_BTN))
    cr.fill()
    m = s * 0.27
    cr.set_source_rgb(*TEXT)
    cr.set_line_width(max(1, s * 0.09))
    cr.rectangle(x + m, y + m, s - 2 * m, s - 2 * m)
    cr.stroke()


SCRATCH = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))   # for measuring without a window


def draw_mini(cr, k, rows, memory, pressed):
    """The mini HUD: what's running or loaded, then VRAM and RAM. Returns its width and click targets.
    Drawing on SCRATCH just measures it."""
    h = HUD_H * k
    cy = h / 2
    bs = h - 10 * k
    x = 5 * k
    draw_expand_button(cr, x, cy - bs / 2, bs, pressed == "mini")
    targets = [(x, cy - bs / 2, bs, bs, "mini")]
    x += bs + 10 * k
    running = [r for r in rows if r["state"] in ("on", "busy")]
    for r in running[:HUD_MAX_NAMES]:
        cr.set_source_rgb(*(GREEN if r["state"] == "on" else AMBER))
        cr.arc(x + 3.5 * k, cy, 3.5 * k, 0, 2 * math.pi)
        cr.fill()
        x += 11 * k
        x += draw_text(cr, r["name"], x, cy, (12 if r["level"] == 0 else 11) * k, TEXT if r["level"] == 0 else MUTED,
                       bold=r["level"] == 0, centred=False) + 11 * k
    if len(running) > HUD_MAX_NAMES:
        x += draw_text(cr, f"+{len(running) - HUD_MAX_NAMES}", x, cy, 11 * k, MUTED, bold=True, centred=False) + 11 * k
    if not running:
        x += draw_text(cr, "nothing running", x, cy, 11 * k, MUTED, centred=False) + 11 * k
    for key in ("vram", "ram"):
        if key in memory:
            used, total = memory[key]
            x += draw_text(cr, key.upper(), x, cy, 10 * k, MUTED, bold=True, centred=False) + 4 * k
            x += draw_text(cr, f"{used / GIB:.1f}/{total / GIB:.0f}", x, cy, 12 * k, level_colour(used / total),
                           bold=True, centred=False) + 11 * k
    return int(x), targets


def draw_pill(cr, x, y, w, h, label, colour, text_colour=TEXT, outline=False):
    rounded_rect(cr, x, y, w, h, h / 2)
    cr.set_source_rgb(*colour)
    if outline:
        cr.set_line_width(max(1, h * 0.06))
        cr.stroke()
    else:
        cr.fill()
    draw_text(cr, label, x + w / 2, y + h / 2, h * 0.46, text_colour, bold=True)


def button_label(row):
    if row.get("busy"):
        return "…"
    if row["kind"] == "comfy":
        return "UNLOAD"
    return "START" if row["state"] == "off" else "STOP"


def draw_panel(cr, w, h, scale, rows, error, memory, pressed):
    """Draws the panel and returns its click targets as (x, y, w, h, target)."""
    k = scale
    targets = []
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    rounded_rect(cr, 1, 1, w - 2, h - 2, 16 * k)
    cr.set_source_rgb(*BG)
    cr.fill_preserve()
    cr.set_line_width(2)
    cr.set_source_rgb(*TRACK)
    cr.stroke()

    draw_text(cr, "AI MODELS", PAD * k, 18 * k, 15 * k, TEXT, bold=True, centred=False)
    targets.append((PAD * k, 6 * k, 96 * k, 24 * k, "options"))
    clock_w = draw_text(cr, time.strftime("%H:%M"), w - PAD * k, 18 * k, 15 * k, MUTED, bold=True, right=True)
    ox, oy, ow, oh = w - PAD * k - clock_w - 12 * k - 74 * k, 18 * k - 11 * k, 74 * k, 22 * k
    draw_pill(cr, ox, oy, ow, oh, "OPTIONS", tuple(c * 0.8 for c in BUSY_BTN) if pressed == "options" else BUSY_BTN)
    targets.append((ox, oy, ow, oh, "options"))
    wx, ww = ox - 8 * k - 58 * k, 58 * k
    draw_pill(cr, wx, oy, ww, oh, "WEB ↗", tuple(c * 0.8 for c in START_BTN) if pressed == "hub" else START_BTN)
    targets.append((wx, oy, ww, oh, "hub"))
    hx, hw = wx - 8 * k - 44 * k, 44 * k
    draw_pill(cr, hx, oy, hw, oh, "HUD", tuple(c * 0.8 for c in TRACK) if pressed == "hud" else TRACK, MUTED)
    targets.append((hx, oy, hw, oh, "hud"))
    bar_w = (w - 3 * PAD * k) / 2
    for i, key in enumerate(("vram", "ram")):
        if key in memory:
            draw_bar(cr, PAD * k + i * (bar_w + PAD * k), 50 * k, bar_w, 5 * k, key.upper(), memory[key])

    y = HEADER_H * k
    if not rows:
        draw_text(cr, error or "No containers yet", w / 2, y + ROW_H * k / 2, 12 * k, MUTED, max_w=w - 2 * PAD * k)
    for row in rows:
        rh = (SUB_H if row["level"] else ROW_H) * k
        cy = y + rh / 2
        if row["level"] == 0 and y > HEADER_H * k:
            cr.set_source_rgb(*TRACK)
            cr.rectangle(PAD * k, y, w - 2 * PAD * k, max(1, k))
            cr.fill()
        indent = (PAD + 18 * row["level"]) * k
        dot = {"on": GREEN, "busy": AMBER}.get(row["state"], RED if row.get("failed") else MUTED)
        cr.set_source_rgb(*dot)
        cr.arc(indent + 5 * k, cy, (4 if row["level"] else 5) * k, 0, 2 * math.pi)
        cr.fill()

        bx = w - PAD * k - BTN_W * k
        ax = bx - 8 * k - AUTO_W * k
        py = cy - PILL_H * k / 2
        label = button_label(row)
        colour = BUSY_BTN if row.get("busy") else START_BTN if label == "START" else STOP_BTN
        if pressed == ("toggle", row["key"]):
            colour = tuple(c * 0.8 for c in colour)
        draw_pill(cr, bx, py, BTN_W * k, PILL_H * k, label, colour)
        targets.append((bx, py, BTN_W * k, PILL_H * k, ("toggle", row["key"])))
        draw_pill(cr, ax, py, AUTO_W * k, PILL_H * k, "AUTO", TRACK, MUTED, outline=True)
        targets.append((ax, py, AUTO_W * k, PILL_H * k, ("auto", row["key"])))

        mem = fmt_mem(row["vram"], row["ram"])
        mem_x = ax - 10 * k
        if mem:
            draw_text(cr, mem, mem_x, cy, 11 * k, TEXT, right=True)
        name_x = indent + 16 * k
        name_w = mem_x - name_x - (80 if mem else 0) * k
        size = 13 if row["level"] == 0 else 12
        url = row.get("url")
        tw = draw_text(cr, row["name"] + (" ↗" if url else ""), name_x, cy - 8 * k, size * k, LINK if url else TEXT,
                       bold=row["level"] == 0, centred=False, max_w=name_w)
        if url:
            targets.append((name_x, cy - 17 * k, tw, 18 * k, ("open", row["key"])))
        draw_text(cr, row["sub"], name_x, cy + 9 * k, 10 * k, RED if row.get("failed") else MUTED, centred=False,
                  max_w=mem_x - name_x)
        targets.append((0, y, w, rh, ("row", row["key"])))
        y += rh
    return targets


def autostart_enabled():
    return os.path.exists(AUTOSTART)


def set_autostart(on):
    """Adds or removes the login entry that starts the panel."""
    if not on:
        try:
            os.remove(AUTOSTART)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
    with open(AUTOSTART, "w") as f:
        f.write("[Desktop Entry]\n"
                "Type=Application\n"
                "Name=AI Model Monitor\n"
                "Comment=Which AI models are running, with start and stop buttons\n"
                f"Exec=python3 {os.path.abspath(__file__)}\n"
                "Icon=applications-science\n"
                "X-GNOME-Autostart-enabled=true\n"
                "X-GNOME-Autostart-Delay=5\n")   # let the compositor start first, for clean round corners


HELP = """<b>Using the panel</b>
• Each <b>container</b> is a row. <b>START</b> and <b>STOP</b> run <tt>docker start</tt> and <tt>docker stop</tt>.
• <b>Ollama models</b> are listed under the ollama container while it runs. START loads a model into memory and keeps it there until you press STOP. Something else using the model, such as Open WebUI, sets its own timer, after which Ollama unloads the model by itself.
• <b>ComfyUI</b> loads its image models when a workflow runs. While they're in VRAM, an <b>Image models</b> row appears. UNLOAD frees them and leaves the container running.
• Starting <b>Strata</b> loads its whole model, about 47 GB across RAM and VRAM. Stop the other models first.

<b>Web pages</b>
A name in <span foreground="#73b3fa">blue with ↗</span> is a link. A container's link opens its web page: Open WebUI, ComfyUI or Strata's chat. An Ollama model's link opens Open WebUI with that model picked. If the container behind the page is stopped, the link starts it and opens the page once it's ready, which can take a few minutes for Strata.
Ollama's own port is only an API for other apps, so it has no link.

<b>AI Hub web page</b>
<b>WEB ↗</b> opens the AI Hub in your browser. It shows the same rows as this panel for both PCs, plus GPU and CPU temperatures, quick links, a Wake button for the main PC and the docs about the whole setup. It works from a phone on the home Wi-Fi too.

<b>Startup</b>
Click <b>OPTIONS</b> (or the title) for these settings.
• <b>Start AI Model Monitor when I log in</b> opens the panel each time you log in.
• <b>Load at startup</b> loads one container or Ollama model each time the panel opens. For an Ollama model it starts the ollama container first if needed. Turn on both to have the model ready after every restart.

<b>Reading a row</b>
The dot is <b>green</b> when the row is running or loaded, <b>amber</b> while it's starting or stopping, and <b>grey</b> when it's off. It turns <b>red</b> when a start or stop fails, and the reason shows under the name for 30 seconds.
The numbers on the right are <b>VRAM · RAM</b> in GB. A container's RAM is what its programs have allocated. Cached model files are left out, since the kernel drops them when memory runs short.
The bars at the top are the whole machine's VRAM and RAM.

<b>AUTO (coming soon)</b>
Auto mode will start a model when an app asks for it and stop it once it has been idle, so you won't need the buttons.

<b>Mini HUD</b>
<b>HUD</b> (top of the panel) shrinks it to one see-through line: what's running or loaded, then VRAM and RAM. Click the square button on its left to get the full panel back. Scroll over the mini HUD to resize it. It remembers its own spot on screen.

<b>Moving and sizing</b>
Drag the panel to move it and scroll over it to resize. Right-click to open a container's web page, unload all models, or quit."""


class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent, settings, rows):
        super().__init__(title="AI Model Monitor options", transient_for=parent, modal=True)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_keep_above(True)
        tabs = Gtk.Notebook()
        self.get_content_area().add(tabs)

        grid = Gtk.Grid(row_spacing=8, column_spacing=12, margin=16)
        tabs.append_page(grid, Gtk.Label(label="Settings"))
        fields = [("refresh_s", "Update every (seconds)", 1, 30, 0.5, 1),
                  ("scale", "Panel size", MIN_SCALE, MAX_SCALE, 0.1, 1)]
        self.spins = {}
        for i, (key, label, lo, hi, step, digits) in enumerate(fields):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            spin = Gtk.SpinButton.new_with_range(lo, hi, step)
            spin.set_digits(digits)
            spin.set_value(settings[key])
            grid.attach(spin, 1, i, 1, 1)
            self.spins[key] = spin
        self.on_top = Gtk.CheckButton(label="Keep above other windows")
        self.on_top.set_active(settings["on_top"])
        grid.attach(self.on_top, 0, len(fields), 2, 1)

        row = len(fields) + 1
        heading = Gtk.Label(xalign=0, margin_top=8)
        heading.set_markup("<b>Startup</b>")
        grid.attach(heading, 0, row, 2, 1)
        self.autostart = Gtk.CheckButton(label="Start AI Model Monitor when I log in")
        self.autostart.set_active(autostart_enabled())
        grid.attach(self.autostart, 0, row + 1, 2, 1)
        grid.attach(Gtk.Label(label="Load at startup", xalign=0), 0, row + 2, 1, 1)
        self.startup = Gtk.ComboBoxText()
        self.startup.append("", "Nothing")
        for key, label in startup_choices(rows, settings["startup_load"]):
            self.startup.append(key, label)
        self.startup.set_active_id(settings["startup_load"] or "")
        grid.attach(self.startup, 1, row + 2, 1, 1)
        note = Gtk.Label(label="Loads each time the panel opens. With the login option on as well,\n"
                               "the model is ready after every restart.", xalign=0)
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 0, row + 3, 2, 1)

        text = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True, max_width_chars=60, margin=16)
        text.set_markup(HELP)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_size_request(-1, 380)
        scroll.add(text)
        tabs.append_page(scroll, Gtk.Label(label="Help"))
        self.show_all()

    def values(self):
        v = {k: s.get_value() for k, s in self.spins.items()}
        v["on_top"] = self.on_top.get_active()
        v["startup_load"] = self.startup.get_active_id() or None
        return v


class App(Gtk.Window):
    """Undecorated rounded window: drag to move, scroll to resize, right-click for the menu."""

    def __init__(self):
        super().__init__(title="AI Model Monitor")
        self.pressed = None
        self.press_target = None
        self.save_pending = False
        self.targets = []
        self.size = None
        self.cursor = None
        self.monitor = Monitor(load_settings(), self.on_update)
        s = self.monitor.settings

        self.set_icon_name("applications-science")
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_keep_above(s["on_top"])
        self.set_skip_taskbar_hint(False)
        screen = self.get_screen()
        if screen.is_composited() and screen.get_rgba_visual():
            self.set_visual(screen.get_rgba_visual())
        x, y = (s["hud_x"], s["hud_y"]) if s["mini"] else (s["x"], s["y"])
        if x is not None:
            self.move(x, y)

        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                             | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.set_has_tooltip(True)
        self.area.connect("query-tooltip", self.on_tooltip)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release)
        self.area.connect("scroll-event", self.on_scroll)
        self.add(self.area)
        self.connect("configure-event", self.on_configure)
        self.connect("destroy", self.on_quit)
        self.on_update()
        GLib.timeout_add_seconds(1, lambda: self.queue_draw() or True)   # the clock and failure notes

    def on_update(self):
        """Fits the window to the rows (or the mini HUD to its line), then redraws."""
        rows, _, memory = self.monitor.snapshot()
        s = self.monitor.settings
        if s["mini"]:
            k = s["hud_scale"]
            size = (draw_mini(SCRATCH, k, rows, memory, None)[0], int(HUD_H * k))
        else:
            size = panel_size(rows, s["scale"])
        if size != self.size:
            self.size = size
            self.set_size_request(*size)
            self.resize(*size)
            self.update_shape()
        self.queue_draw()
        return False

    def update_shape(self):
        """Clips the window, and the area that takes clicks, to the rounded panel."""
        w, h = self.size
        mask = cairo.ImageSurface(cairo.FORMAT_A1, w, h)
        cr = cairo.Context(mask)
        s = self.monitor.settings
        rounded_rect(cr, 0, 0, w, h, h / 2 if s["mini"] else 16 * s["scale"])
        cr.fill()
        region = Gdk.cairo_region_create_from_surface(mask)
        self.input_shape_combine_region(region)
        if not self.get_screen().is_composited():
            self.shape_combine_region(region)

    def on_draw(self, widget, cr):
        rows, error, memory = self.monitor.snapshot()
        if self.monitor.settings["mini"]:
            w, h = self.size
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            rounded_rect(cr, 1, 1, w - 2, h - 2, h / 2 - 1)
            cr.set_source_rgba(*BG, HUD_ALPHA)
            cr.fill_preserve()
            cr.set_line_width(1.5)
            cr.set_source_rgba(*TRACK, 0.8)
            cr.stroke()
            self.targets = draw_mini(cr, self.monitor.settings["hud_scale"], rows, memory, self.pressed)[1]
            return False
        self.targets = draw_panel(cr, *self.size, self.monitor.settings["scale"], rows, error, memory, self.pressed)
        return False

    def hit(self, x, y):
        """The first click target under the point, buttons before the rows they sit on."""
        for tx, ty, tw, th, target in self.targets:
            if tx <= x <= tx + tw and ty <= y <= ty + th:
                return target
        return None

    def row(self, key):
        return next((r for r in self.monitor.snapshot()[0] if r["key"] == key), None)

    def on_motion(self, _, event):
        target = self.hit(event.x, event.y)
        name = "pointer" if target in ("options", "hub", "hud", "mini") or (target and target[0] in ("toggle", "open")) else "default"
        if name != self.cursor:
            self.cursor = name
            self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))

    def on_tooltip(self, _, x, y, keyboard, tooltip):
        target = self.hit(x, y)
        if target == "options":
            tooltip.set_text("Options: startup, refresh and help")
            return True
        if target == "hud":
            tooltip.set_text("Shrink to a mini HUD")
            return True
        if target == "mini":
            tooltip.set_text("Back to the full panel")
            return True
        if target == "hub":
            tooltip.set_text(f"Open the AI Hub web page\n{self.monitor.settings['hub_url']}\n"
                             "Both PCs' models, GPU temperatures and the docs")
            return True
        if not target:
            return False
        if target[0] == "auto":
            tooltip.set_text("Auto mode is coming soon: start this when an app asks for it, stop it when idle")
            return True
        row = self.row(target[1])
        if not row:
            return False
        if target[0] == "open":
            host = self.row(f"docker:{row['web_host']}")
            tooltip.set_text(f"Open {row['url']}" + (f"\nStarts {row['web_host']} first" if host and
                                                    host["state"] == "off" else ""))
            return True
        if not row.get("tip"):
            return False
        tooltip.set_text(row["tip"] + ("\n\nVRAM · RAM in GB" if row["vram"] or row["ram"] else ""))
        return True

    def on_press(self, _, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:   # ignore the extra double-click events
            return
        if event.button == 3:
            self.show_menu(event)
            return
        if event.button != 1:
            return
        self.press_target = self.hit(event.x, event.y)
        if self.press_target in ("options", "hub", "hud", "mini") or (self.press_target and self.press_target[0] in ("toggle", "open")):
            self.pressed = self.press_target
            self.queue_draw()
        else:
            self.press_target = None
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)

    def on_release(self, _, event):
        target, self.press_target = self.press_target, None
        self.pressed = None
        self.queue_draw()
        if event.button != 1 or target is None or self.hit(event.x, event.y) != target:
            return
        if target == "options":
            GLib.idle_add(self.open_settings)
            return
        if target == "hub":
            open_uri(self.monitor.settings["hub_url"])
            return
        if target in ("hud", "mini"):
            self.toggle_mini()
            return
        row = self.row(target[1])
        if not row or row.get("busy"):
            return
        if target[0] == "open":
            self.monitor.open(row)
        else:
            self.monitor.act(row, starting=row["state"] == "off" and row["kind"] != "comfy")

    def on_scroll(self, _, event):
        step = {Gdk.ScrollDirection.UP: 1.1, Gdk.ScrollDirection.DOWN: 1 / 1.1}.get(event.direction)
        if step:
            s = self.monitor.settings
            if s["mini"]:
                s["hud_scale"] = round(min(MAX_HUD, max(MIN_HUD, s["hud_scale"] * step)), 2)
            else:
                s["scale"] = round(min(MAX_SCALE, max(MIN_SCALE, s["scale"] * step)), 2)
            self.on_update()
            self.queue_save()

    def show_menu(self, event):
        menu = Gtk.Menu()
        rows = self.monitor.snapshot()[0]
        item = Gtk.MenuItem(label=f"Open AI Hub web page  ·  {self.monitor.settings['hub_url']}")
        item.connect("activate", lambda _: open_uri(self.monitor.settings["hub_url"]))
        menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        for r in rows:
            if r.get("url") and r["kind"] == "docker":
                item = Gtk.MenuItem(label=f"Open {r['name']}  ·  {r['url']}")
                item.connect("activate", lambda _, row=r: self.monitor.open(row))
                menu.append(item)
        if menu.get_children():
            menu.append(Gtk.SeparatorMenuItem())
        loaded = [r for r in rows if r["kind"] != "docker" and r["state"] == "on"]
        item = Gtk.MenuItem(label="Unload all models" if loaded else "No models loaded")
        item.set_sensitive(bool(loaded))
        item.connect("activate", lambda _: [self.monitor.act(r, starting=False) for r in loaded])
        menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        for label, action in (("Full panel" if self.monitor.settings["mini"] else "Mini HUD", self.toggle_mini),
                              ("Options…", self.open_settings), ("Quit", self.destroy)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _, a=action: a())
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)

    def open_settings(self):
        s = self.monitor.settings
        dialog = SettingsDialog(self, s, self.monitor.snapshot()[0])
        if dialog.run() == Gtk.ResponseType.OK:
            s.update(dialog.values())
            self.set_keep_above(s["on_top"])
            save_settings(s)
            if dialog.autostart.get_active() != autostart_enabled():
                set_autostart(dialog.autostart.get_active())
            self.on_update()
        dialog.destroy()
        return False

    def toggle_mini(self):
        """Switches between the full panel and the mini HUD, each at its own place on screen."""
        s = self.monitor.settings
        self.save_position()
        s["mini"] = not s["mini"]
        self.pressed = None
        x, y = (s["hud_x"], s["hud_y"]) if s["mini"] else (s["x"], s["y"])
        self.on_update()
        if x is not None:
            self.move(x, y)
        save_settings(s)

    def on_configure(self, *_):
        self.queue_save()
        return False

    def queue_save(self):
        """Saves size and position shortly after the panel stops moving."""
        if not self.save_pending:
            self.save_pending = True
            GLib.timeout_add(800, self.save_position)

    def save_position(self):
        self.save_pending = False
        s = self.monitor.settings
        if s["mini"]:
            s["hud_x"], s["hud_y"] = self.get_position()
        else:
            s["x"], s["y"] = self.get_position()
        save_settings(s)
        return False

    def on_quit(self, *_):
        save_settings(self.monitor.settings)
        Gtk.main_quit()


if __name__ == "__main__":
    App().show_all()
    Gtk.main()
