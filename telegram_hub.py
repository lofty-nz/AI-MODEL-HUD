"""AI Hub's Telegram messenger: one bot that sends notifications to your phone for the other apps
(the TTR app's new leads, for a start) and shows everything it sent on the hub's Telegram tab.

- Set up on the Telegram tab: make a bot with @BotFather, paste its token, then send the bot the
  6-digit code the tab shows. That links your Telegram chat. Nobody else can use the bot.
- Apps on this PC send with POST /api/telegram/send {text, key, buttons, silent, source} and can
  change a message later with POST /api/telegram/edit {key, text, buttons} (same key). Buttons are
  link buttons: [[{"text": "Open in TTR", "url": "https://..."}]].
- Long polling (getUpdates), so nothing here is opened to the internet. Only one program may read a
  bot's updates at a time, so only one hub has the bot (config "telegram": true).
- Answers: an app can send with "reply_url" (an address on this PC). What you send the bot in answer to
  that message (a reply to it, or the next message you send within a day; with "telegram_default_url" set in
  the hub's config, a message that isn't a reply goes to that address instead) is POSTed there as
  {text, voice, key}; the app answers {say}, which the bot sends back. 🎤 Voice messages are written out
  first with the hub's Whisper (the same speech-to-text as the 🎤 buttons). TTR uses this for leads:
  "send" emails the draft, anything else rewrites it.

Token and linked chats: ~/.config/aihub/telegram.json (owner-only). Message log:
~/.local/state/aihub/telegram-log.json. Only standard-library Python.
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request

CONFIG = os.path.expanduser("~/.config/aihub/telegram.json")
STATE_DIR = os.path.expanduser("~/.local/state/aihub")
LOG = os.path.join(STATE_DIR, "telegram-log.json")
OFFSET = os.path.join(STATE_DIR, "telegram.offset")
API = os.environ.get("AIHUB_TELEGRAM_API") or "https://api.telegram.org"   # tests point this at a stand-in
TOKEN_RE = re.compile(r"^\d+:[\w-]{30,}$")
KEEP = 300                  # messages kept in the log
PAIR_S = 30 * 60            # how long a pairing code works
MAX_TEXT = 4000             # Telegram's limit is 4096
HELP = ("I'm AI Hub's messenger. I'll message you here when something needs you, such as a new lead "
        "in the TTR app; the button under each message opens it. To answer a lead, reply to its message "
        "(or just send your next message), typed or as a 🎤 voice message: say “send” to email the draft, "
        "or tell me what to change and the AI rewrites it. Everything shows on the hub's Telegram tab.")
REPLY_URL = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/[^\s]*$")   # answers only go to apps on this PC
HUB_CONFIG = os.path.expanduser("~/.config/aihub/config.json")
# "telegram_default_url" in the hub's config: where a message that isn't a reply goes (after Whisper), e.g. TTR's
# "http://127.0.0.1:8766/telegram/any", which works out which lead or job you mean. Blank: the latest message that
# takes answers (up to a day old), as before.
ANSWER_WINDOW = 24 * 3600   # a message that isn't a reply goes to the latest one that takes answers, up to a day old
MAX_VOICE = 20 * 1024 * 1024


class TelegramError(ValueError):
    pass


def _clean_buttons(buttons):
    """Link buttons only, as rows: [[{text, url}]]. A flat list is one row."""
    if not buttons:
        return []
    if isinstance(buttons, dict):
        buttons = [[buttons]]
    rows = [r if isinstance(r, list) else [r] for r in buttons]
    out = []
    for row in rows[:4]:
        keep = []
        for b in row[:3]:
            text, url = str((b or {}).get("text") or "").strip()[:60], str((b or {}).get("url") or "").strip()
            if text and re.match(r"^https?://[^\s]+$", url) and len(url) < 1000:
                keep.append({"text": text, "url": url})
        if keep:
            out.append(keep)
    return out


def _with_links(text, rows):
    """For when Telegram turns down a link button (it refuses some addresses): the links go in the text."""
    links = [f"{b['text']}: {b['url']}" for row in rows for b in row]
    return (text.rstrip() + "\n\n" + "\n".join(links)) if links else text


class Telegram:
    def __init__(self, enabled, transcribe=None):
        self.enabled = bool(enabled)
        self.transcribe = transcribe     # audio bytes -> text (aihub.transcribe), for voice messages
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.poll_error = ""
        self.refused = None          # the last stranger who messaged the bot: {name, id, ts}
        self.cfg = self._read_config()
        self.log = self._read_log()
        if self.enabled:
            threading.Thread(target=self._run, daemon=True).start()

    # ---------------------------------------------------------------- files
    def _read_config(self):
        try:
            with open(CONFIG) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        cfg["token"] = str(cfg.get("token") or "").strip()
        cfg["chats"] = [c for c in cfg.get("chats") or [] if isinstance(c, dict) and isinstance(c.get("id"), int)]
        return cfg

    def _save_config(self):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        tmp = CONFIG + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: self.cfg[k] for k in ("token", "username", "chats", "pair") if k in self.cfg}, f, indent=1)
        os.replace(tmp, CONFIG)

    def _read_log(self):
        try:
            with open(LOG) as f:
                return json.load(f)[-KEEP:]
        except (OSError, ValueError):
            return []

    def _save_log(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.log = self.log[-KEEP:]
        tmp = LOG + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.log, f)
        os.replace(tmp, LOG)

    # ---------------------------------------------------------------- Bot API
    def _call(self, method, token=None, http_timeout=20, **params):
        token = token or self.cfg["token"]
        if not token:
            raise TelegramError("No bot is set up yet")
        req = urllib.request.Request(f"{API}/bot{token}/{method}", json.dumps(params).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                res = json.load(e)
            except ValueError:
                raise TelegramError(f"Telegram answered {e.code}")
        except (OSError, ValueError) as e:
            raise TelegramError(f"Can't reach Telegram ({e})")
        if not res.get("ok"):
            raise TelegramError(res.get("description") or "Telegram said no")
        return res["result"]

    def ready(self):
        return bool(TOKEN_RE.match(self.cfg["token"])) and bool(self.cfg["chats"])

    # ---------------------------------------------------------------- sending
    def _send_one(self, chat, text, rows, silent):
        params = {"chat_id": chat, "text": text[:MAX_TEXT], "disable_notification": bool(silent),
                  "link_preview_options": {"is_disabled": True}}
        if rows:
            params["reply_markup"] = {"inline_keyboard": rows}
        try:
            return self._call("sendMessage", **params)["message_id"]
        except TelegramError as e:
            if rows and re.search(r"(?i)button|url", str(e)):
                params.pop("reply_markup")
                params["text"] = _with_links(text, rows)[:MAX_TEXT]
                return self._call("sendMessage", **params)["message_id"]
            raise

    def send(self, text, key=None, buttons=None, silent=False, source="", reply_url=None):
        """Message every linked chat. Returns the log entry. A new message with the key of an earlier one
        is a new message (use edit() to change the old one). reply_url: where answers to it go (see above)."""
        text = str(text or "").strip()
        reply_url = str(reply_url or "").strip()
        if reply_url and not REPLY_URL.match(reply_url):
            raise ValueError("reply_url must be an http address on this PC (127.0.0.1)")
        if not text:
            raise ValueError("Nothing to send")
        if not self.enabled:
            raise ValueError("Telegram isn't on this hub; use the server's AI Hub")
        if not self.ready():
            raise ValueError("Telegram isn't set up yet: AI Hub → Telegram tab")
        rows = _clean_buttons(buttons)
        sent, errors = [], []
        for c in self.cfg["chats"][:5]:
            try:
                sent.append([c["id"], self._send_one(c["id"], text, rows, silent)])
            except TelegramError as e:
                errors.append(f"{c.get('name') or c['id']}: {e}")
        entry = {"id": "%x" % int(time.time() * 1000) + "%03x" % random.randrange(4096), "ts": time.time(), "dir": "out",
                 "text": text[:MAX_TEXT], "buttons": rows, "source": str(source or "")[:40], "key": str(key or "")[:120],
                 "refs": sent, "silent": bool(silent)}
        if reply_url:
            entry["reply_url"] = reply_url
        if errors:
            entry["error"] = "; ".join(errors)
        with self.lock:
            self.log.append(entry)
            self._save_log()
        if not sent:
            raise ValueError("Telegram didn't take it: " + "; ".join(errors))
        return entry

    def edit(self, key, text, buttons=None):
        """Change the latest message sent with this key (no new ping on the phone)."""
        key, text = str(key or ""), str(text or "").strip()
        if not key or not text:
            raise ValueError("Need a key and the new text")
        with self.lock:
            entry = next((e for e in reversed(self.log) if e.get("dir") == "out" and e.get("key") == key), None)
        if not entry:
            raise ValueError("No message with that key")
        rows = _clean_buttons(buttons) if buttons is not None else entry.get("buttons") or []
        errors = []
        for chat, mid in entry.get("refs") or []:
            params = {"chat_id": chat, "message_id": mid, "text": text[:MAX_TEXT], "link_preview_options": {"is_disabled": True}}
            if rows:
                params["reply_markup"] = {"inline_keyboard": rows}
            try:
                self._call("editMessageText", **params)
            except TelegramError as e:
                if "not modified" in str(e):
                    continue
                if rows and re.search(r"(?i)button|url", str(e)):
                    params.pop("reply_markup")
                    params["text"] = _with_links(text, rows)[:MAX_TEXT]
                    try:
                        self._call("editMessageText", **params)
                        continue
                    except TelegramError as e2:
                        e = e2
                errors.append(str(e))
        with self.lock:
            entry.update(text=text[:MAX_TEXT], buttons=rows, edited=time.time())
            if errors:
                entry["error"] = "; ".join(errors)
            self._save_log()
        return entry

    # ---------------------------------------------------------------- setup (Telegram tab)
    def status(self):
        with self.lock:
            has = bool(TOKEN_RE.match(self.cfg["token"]))
            pair = self.cfg.get("pair") or {}
            live = has and (pair.get("until") or 0) > time.time()
            return {"enabled": self.enabled, "has_token": has, "bot": self.cfg.get("username") or "",
                    "chats": [{"id": c["id"], "name": c.get("name") or str(c["id"])} for c in self.cfg["chats"]],
                    "pair_code": pair.get("code") if live else "", "pair_until": pair.get("until") if live else 0,
                    "error": self.poll_error, "refused": self.refused, "messages": self.log[-120:]}

    def setup(self, req):
        """{token} saves a bot (checked with Telegram) and makes a pairing code; {pair} a new code;
        {disconnect: chat id or "all"} unlinks chats; {forget} removes the bot; {test} sends a test."""
        if not self.enabled:
            raise ValueError("Telegram isn't on this hub; use the server's AI Hub")
        with self.lock:
            if req.get("token"):
                tok = str(req["token"]).strip()
                if not TOKEN_RE.match(tok):
                    raise ValueError("That doesn't look like a bot token. It's like 123456789:AAF… and comes from @BotFather.")
                try:
                    me = self._call("getMe", token=tok)
                    self._call("deleteWebhook", token=tok)     # getUpdates won't work while a webhook is set
                except TelegramError as e:
                    raise ValueError(f"Telegram didn't accept that token: {e}")
                if tok != self.cfg["token"]:
                    self.cfg["chats"] = []                     # a different bot: link your chat again
                    try:
                        os.remove(OFFSET)
                    except OSError:
                        pass
                self.cfg.update(token=tok, username=me.get("username", ""))
                req["pair"] = True
                self.poll_error = ""
            if req.get("pair"):
                if not self.cfg["token"]:
                    raise ValueError("Save the bot token first")
                self.cfg["pair"] = {"code": "%06d" % random.randint(0, 999999), "until": time.time() + PAIR_S}
            if req.get("unpair"):
                self.cfg["pair"] = {}
            if req.get("disconnect"):
                d = req["disconnect"]
                self.cfg["chats"] = [] if d == "all" else [c for c in self.cfg["chats"] if c["id"] != d]
            if req.get("forget"):
                self.cfg = {"token": "", "chats": []}
                self.poll_error = ""
            self._save_config()
        self.wake.set()                                        # the poller picks up the new token now
        if req.get("test"):
            self.send("✅ Test message from AI Hub. Notifications will arrive here.", source="AI Hub",
                      buttons=[[{"text": "Open AI Hub", "url": str(req.get("hub_url") or "")}]] if req.get("hub_url") else None)
        return self.status()

    # ---------------------------------------------------------------- reading what comes in
    def _pair(self, chat, msg):
        pair = self.cfg.get("pair") or {}
        # the code as typed (a space in the middle is fine), or "/start <code>" from the tab's t.me link
        text = re.sub(r"^/start\s+", "", str(msg.get("text") or "").strip()).replace(" ", "")
        if not pair.get("code") or (pair.get("until") or 0) < time.time() or text != pair["code"]:
            return False
        c = msg.get("chat") or {}
        name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("title") or c.get("username") or str(chat)
        with self.lock:
            if chat not in [x["id"] for x in self.cfg["chats"]]:
                self.cfg["chats"].append({"id": chat, "name": name[:60]})
            self.cfg["pair"] = {}
            self._save_config()
        return True

    def _say(self, chat, text):
        try:
            self._call("sendMessage", chat_id=chat, text=text[:MAX_TEXT])
        except TelegramError as e:
            print(f"Telegram: reply failed: {e}", flush=True)

    def _incoming(self, msg):
        chat = (msg.get("chat") or {}).get("id")
        if not isinstance(chat, int):
            return
        if chat not in [c["id"] for c in self.cfg["chats"]]:
            if self._pair(chat, msg):
                self._say(chat, "✅ Connected to AI Hub. " + HELP)
                print(f"Telegram: linked chat {chat}", flush=True)
            else:
                c = msg.get("chat") or {}
                self.refused = {"name": c.get("first_name") or c.get("title") or "", "id": chat, "ts": time.time()}
                self._say(chat, "This bot is private. To link it, send the code shown in AI Hub → Telegram.")
            return
        text = str(msg.get("text") or msg.get("caption") or "").strip()
        if re.match(r"^/(start|help)\b", text):
            self._say(chat, HELP)
            return
        voice = msg.get("voice") or msg.get("audio")
        what = ("📷 a photo" if msg.get("photo") else "📎 " + ((msg.get("document") or {}).get("file_name") or "a file")
                if msg.get("document") else "🎤 a voice message" if voice else "")
        c = msg.get("chat") or {}
        entry = {"id": "in%d" % msg.get("message_id", 0) + "-%d" % chat, "ts": msg.get("date") or time.time(), "dir": "in",
                 "text": (text + ("\n" if text and what else "") + what)[:MAX_TEXT] or "(something I can't show)",
                 "from": c.get("first_name") or ""}
        target = self._answer_target(chat, msg)
        if target:
            entry["to"] = target.get("key") or target["id"]
        with self.lock:
            self.log.append(entry)
            self._save_log()
        if voice or target:      # writing out a voice message and asking the app take a while: not on the polling thread
            threading.Thread(target=self._answer, args=(chat, entry, text, voice, target), daemon=True).start()

    # ---------------------------------------------------------------- answers (to messages sent with reply_url)
    def _default_url(self):
        """telegram_default_url from the hub's config (read each time, so a change needs no restart)."""
        try:
            with open(HUB_CONFIG) as f:
                url = str(json.load(f).get("telegram_default_url") or "").strip()
        except (OSError, ValueError, AttributeError):
            return ""
        return url if REPLY_URL.match(url) else ""

    def _answer_target(self, chat, msg):
        """The message this answers: the one it replies to (if that takes answers), else, when it isn't a
        reply, telegram_default_url (when set), else the latest message that takes answers (up to a day old).
        None when it answers nothing."""
        rid = (msg.get("reply_to_message") or {}).get("message_id")
        default = "" if rid else self._default_url()
        if default:
            return {"id": "default", "key": "", "reply_url": default, "source": "TTR"}
        with self.lock:
            outs = [e for e in reversed(self.log) if e.get("dir") == "out" and e.get("reply_url")]
            if rid:
                return next((e for e in outs if [chat, rid] in (e.get("refs") or [])), None)
            return next((e for e in outs if time.time() - (e.get("ts") or 0) < ANSWER_WINDOW
                         and any(r[0] == chat for r in e.get("refs") or [])), None)

    def _hear(self, voice):
        """A voice message's words, via Whisper."""
        if not self.transcribe:
            raise TelegramError("speech-to-text isn't set up on this hub (config \"dictate\")")
        if (voice.get("file_size") or 0) > MAX_VOICE:
            raise TelegramError("it's too long")
        f = self._call("getFile", file_id=voice["file_id"])
        try:
            with urllib.request.urlopen(f"{API}/file/bot{self.cfg['token']}/{f['file_path']}", timeout=60) as r:
                audio = r.read(MAX_VOICE + 1)
        except OSError as e:
            raise TelegramError(f"couldn't download it ({e})")
        return self.transcribe(audio)

    def _tell(self, chat, text, target=None):
        """Answer in this chat. Answers about a lead take answers too (they go to the same place)."""
        try:
            mid = self._call("sendMessage", chat_id=chat, text=text[:MAX_TEXT],
                             link_preview_options={"is_disabled": True})["message_id"]
        except TelegramError as e:
            print(f"Telegram: reply failed: {e}", flush=True)
            mid = None
        entry = {"id": "%x" % int(time.time() * 1000) + "%03x" % random.randrange(4096), "ts": time.time(), "dir": "out",
                 "text": text[:MAX_TEXT], "buttons": [], "source": (target or {}).get("source") or "AI Hub",
                 "key": "", "refs": [[chat, mid]] if mid else [], "silent": False}
        if target and target.get("reply_url"):
            entry["reply_url"] = target["reply_url"]
        with self.lock:
            self.log.append(entry)
            self._save_log()

    def _answer(self, chat, entry, text, voice, target):
        heard = ""
        if voice:
            try:
                heard = str(self._hear(voice) or "").strip()
            except Exception as e:
                self._tell(chat, f"🎤 I couldn't write out that voice message ({e}). Try again, or type it.", target)
                return
            with self.lock:
                entry["text"] = (f"🎤 “{heard}”" if heard else "🎤 (no words I could make out)")[:MAX_TEXT]
                self._save_log()
            text = (text + " " + heard).strip()
        if not target:
            if voice:
                self._tell(chat, f"🎤 I heard: “{heard}”" if heard else "🎤 I couldn't make out any words in that.")
            return
        if not text:
            self._tell(chat, "🎤 I couldn't make out any words in that. Try again?", target)
            return
        try:
            req = urllib.request.Request(target["reply_url"], json.dumps({"text": text, "voice": bool(voice),
                                                                         "key": target.get("key") or ""}).encode(),
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.load(r)
            say = str(res.get("say") or "") if res.get("ok") else "⚠ " + str(res.get("error") or "it said no")
        except urllib.error.HTTPError as e:
            try:
                say = "⚠ " + str(json.load(e).get("error") or e)
            except ValueError:
                say = f"⚠ {target.get('source') or 'The app'} answered {e.code}"
        except (OSError, ValueError) as e:
            say = f"⚠ I couldn't pass that on to {target.get('source') or 'the app'} ({getattr(e, 'reason', e)}). Is it running?"
        if say:
            self._tell(chat, (f"🎤 “{heard}”\n\n" if voice else "") + say, target)

    def _run(self):
        try:
            offset = int(open(OFFSET).read())
        except (OSError, ValueError):
            offset = 0
        token_was = None
        while True:
            token = self.cfg["token"]
            if not TOKEN_RE.match(token):
                self.wake.wait(30)
                self.wake.clear()
                continue
            if token != token_was:
                token_was = token
                try:
                    offset = int(open(OFFSET).read())
                except (OSError, ValueError):
                    offset = 0
            try:
                # short polls while a pairing code is waiting, so linking feels instant
                wait = 5 if (self.cfg.get("pair") or {}).get("until", 0) > time.time() else 50
                updates = self._call("getUpdates", token=token, http_timeout=wait + 15, timeout=wait,
                                     offset=offset, allowed_updates=["message"])
                self.poll_error = ""
            except TelegramError as e:
                msg = str(e)
                if "Conflict" in msg:
                    msg = "Another program is reading this bot's messages (only one may). Stop it, or give AI Hub its own bot."
                elif "Unauthorized" in msg or "Not Found" in msg:
                    msg = "Telegram doesn't know this bot's token any more (revoked in @BotFather?). Paste the new token."
                self.poll_error = msg
                self.wake.wait(20)
                self.wake.clear()
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    os.makedirs(STATE_DIR, exist_ok=True)
                    with open(OFFSET, "w") as f:       # never read a message twice after a restart
                        f.write(str(offset))
                except OSError:
                    pass
                if u.get("message"):
                    try:
                        self._incoming(u["message"])
                    except Exception as e:             # one odd message mustn't stop the bot
                        print(f"Telegram: message failed: {e!r}", flush=True)
