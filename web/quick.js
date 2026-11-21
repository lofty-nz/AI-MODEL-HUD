/* AI Hub quick button: a round button (bottom right) that pops up the 10 programs you use most.
 *
 * Any page can have it with one line (served by the hub, no login needed for this file):
 *     <script src="https://<your hub>/quick.js" defer></script>
 * Options, as data- attributes on that tag:
 *     data-bottom="92"   px from the bottom edge, to sit above the page's own corner buttons (default 16)
 *     data-right="16"    px from the side edge (default 16)
 *     data-side="left"   bottom left instead of bottom right
 *     data-visit="off"   don't count opening this page as a use
 *
 * Kept apart from the page on purpose, so neither can break the other:
 * - the button is in a closed shadow root (the page's styles can't reach it, its styles can't reach the page)
 *   and nothing is added to the page's window;
 * - the list is the hub's own page (/quick) in a frame, with the hub's login, so the page never sees it;
 * - the frame talks to this script over its own MessageChannel, so the page's message handlers never see it;
 * - if the hub can't be reached, the page carries on as normal (no button, or a short note in the pop-up).
 * Drag the button to move it; that's remembered for this site (localStorage "aihub-quick-pos").
 * The hub fills in CONF when it serves this file: which hub's pop-up to use for each page address.
 */
(function () {
  "use strict";
  var CONF = /*HUB-CONFIG*/null;
  var me = document.currentScript;
  if (!me || window.top !== window) return;                      // not inside frames
  if (document.querySelector("aihub-quick")) return;             // already on this page
  var root0 = document.createElement("aihub-quick");
  if (!root0.attachShadow) return;                               // a very old browser: no button at all

  /* ---- which hub: one on the same site as this page, so its login comes along ---- */
  var src = new URL(me.src, location.href);
  var host = location.hostname.toLowerCase(), hub = null, rules = (CONF && CONF.hubs) || [];
  for (var i = 0; i < rules.length && !hub; i++) {
    var k = rules[i][0];
    if (host === k || host.slice(-(k.length + 1)) === "." + k) hub = rules[i][1];
  }
  if (!hub) hub = src.origin + "/";                              // somewhere else: the hub this file came from
  var hubOrigin = new URL(hub).origin;

  var opt = me.dataset || {};
  var num = function (v, d) { v = parseInt(v, 10); return isFinite(v) && v >= 0 && v < 2000 ? v : d; };
  var DEF = { side: opt.side === "left" ? "left" : "right", e: num(opt.right != null ? opt.right : opt.left, 16), b: num(opt.bottom, 16) };
  var POS_KEY = "aihub-quick-pos", SIZE = 48, GAP = 10, M = 8;
  var pos = DEF, moved = false;
  try {
    var saved = JSON.parse(localStorage.getItem(POS_KEY) || "null");
    if (saved && (saved.side === "left" || saved.side === "right") && isFinite(saved.e) && isFinite(saved.b)) { pos = saved; moved = true; }
  } catch (e) { /* storage blocked: the default corner */ }

  /* ---- the button and the pop-up, in a closed shadow root ---- */
  root0.setAttribute("style", "all:initial!important;display:block!important;position:fixed!important;top:0!important;" +
    "left:0!important;width:0!important;height:0!important;z-index:2147483000!important;");
  var sh = root0.attachShadow({ mode: "closed" });
  sh.innerHTML =
    "<style>" +
    ":host{all:initial}" +
    ".b{position:fixed;width:" + SIZE + "px;height:" + SIZE + "px;margin:0;padding:0;border:0;border-radius:50%;cursor:pointer;" +
    "background:#1b2130;color:#fff;box-shadow:0 6px 18px -6px rgba(0,0,0,.6),0 0 0 1.5px rgba(255,255,255,.16);display:grid;" +
    "place-items:center;touch-action:none;-webkit-tap-highlight-color:transparent;-webkit-user-select:none;user-select:none;" +
    "opacity:.9;transition:opacity .15s,transform .15s}" +
    ".b:hover,.b[aria-expanded=true]{opacity:1}" +
    ".b:focus-visible{outline:3px solid #73b3fa;outline-offset:2px}" +
    ".b.drag{transition:none;opacity:1;transform:scale(1.08);cursor:grabbing}" +
    ".b svg{width:22px;height:22px;display:block;pointer-events:none}" +
    ".p{position:fixed;display:none;overflow:hidden;border-radius:14px;background:#151820;" +
    "box-shadow:0 18px 50px -12px rgba(0,0,0,.7),0 0 0 1px rgba(255,255,255,.1)}" +
    ".p.open{display:block}" +
    ".p iframe{display:block;width:100%;height:100%;border:0;background:transparent}" +
    ".f{position:absolute;inset:0;padding:18px;font:14px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#e6edf2;background:#151820}" +
    ".f a{color:#73b3fa}" +
    ".f[hidden]{display:none}" +
    "@media (prefers-color-scheme:light){.p,.f{background:#fff;color:#1a1f26}.f a{color:#2468b0}}" +
    "@media print{.b,.p{display:none!important}}" +
    "@media (prefers-reduced-motion:reduce){.b{transition:none}}" +
    "</style>" +
    "<button class='b' type='button' aria-haspopup='dialog' aria-expanded='false'" +
    " aria-label='Most-used programs (AI Hub)' title='Your most-used programs (AI Hub). Drag to move.'>" +
    "<svg viewBox='0 0 24 24' aria-hidden='true'>" +
    "<circle cx='5' cy='5' r='2.3' fill='#fff'/><circle cx='12' cy='5' r='2.3' fill='#fff'/><circle cx='19' cy='5' r='2.3' fill='#fff'/>" +
    "<circle cx='5' cy='12' r='2.3' fill='#fff'/><circle cx='12' cy='12' r='2.6' fill='#40ba4f'/><circle cx='19' cy='12' r='2.3' fill='#fff'/>" +
    "<circle cx='5' cy='19' r='2.3' fill='#fff'/><circle cx='12' cy='19' r='2.3' fill='#fff'/><circle cx='19' cy='19' r='2.3' fill='#fff'/></svg>" +
    "</button>" +
    "<div class='p' role='dialog' aria-label='Most-used programs'>" +
    "<div class='f' hidden>The list didn't load here. AI Hub may be off, or it doesn't know this page's address" +
    " (add it to quick_hubs in the hub's config).<br><br><a target='_blank' rel='noopener'>Open AI Hub</a></div></div>";
  var btn = sh.querySelector(".b"), panel = sh.querySelector(".p"), fb = sh.querySelector(".f");
  fb.querySelector("a").href = hub;

  var vw = function () { return document.documentElement.clientWidth || innerWidth; };
  var vh = function () { return innerHeight; };
  function applyPos() {
    var maxE = Math.max(M, vw() - SIZE - M), maxB = Math.max(M, vh() - SIZE - M);
    var e = Math.min(Math.max(pos.e, 4), maxE), b = Math.min(Math.max(pos.b, 4), maxB);
    btn.style.left = pos.side === "left" ? e + "px" : "auto";
    btn.style.right = pos.side === "right" ? e + "px" : "auto";
    // the default corner keeps clear of a phone's home bar; a dragged spot stays where it was put
    btn.style.bottom = moved ? b + "px" : "calc(" + b + "px + env(safe-area-inset-bottom, 0px))";
  }
  applyPos();

  /* ---- the pop-up: the hub's /quick page in a frame, made on the first open ---- */
  var frame = null, port = null, ready = false, readyTimer = 0, wantH = 520, isOpen = false;
  function place() {
    if (!isOpen) return;
    var r = btn.getBoundingClientRect(), W = vw(), H = vh();
    var w = Math.min(340, W - 2 * M);
    var left = r.left + r.width / 2 > W / 2 ? Math.min(r.right, W - M) - w : Math.max(r.left, M);
    left = Math.min(Math.max(left, M), W - w - M);
    var above = r.top + r.height / 2 > H / 2;
    var room = above ? r.top - GAP - M : H - r.bottom - GAP - M;
    var h, top;
    if (room >= 200) {
      h = Math.min(wantH, room);
      top = above ? r.top - GAP - h : r.bottom + GAP;
    } else {   // a short screen (a phone on its side): use the height, over the button
      h = Math.min(wantH, H - 2 * M);
      top = M;
    }
    panel.style.left = left + "px"; panel.style.top = top + "px";
    panel.style.width = w + "px"; panel.style.height = h + "px";
  }
  function send(msg) { if (port) try { port.postMessage(msg); } catch (e) { /* frame gone */ } }
  function connect() {
    // A new private channel each time the frame loads (it reloads after a login). Handed only to the hub's origin.
    var ch = new MessageChannel();
    port = ch.port1;
    port.onmessage = function (ev) {
      var d = ev.data || {};
      if (d.aihubQuick === "ready") { ready = true; clearTimeout(readyTimer); fb.hidden = true; }
      else if (d.aihubQuick === "size" && isFinite(d.h)) { wantH = Math.min(Math.max(d.h, 120), 2000); place(); }
      else if (d.aihubQuick === "close") close(true);
      else if (d.aihubQuick === "reset") { moved = false; pos = DEF; try { localStorage.removeItem(POS_KEY); } catch (e) {} applyPos(); place(); send({ aihubQuick: "moved", moved: false }); }
    };
    try { frame.contentWindow.postMessage({ aihubQuick: "port", moved: moved }, hubOrigin, [ch.port2]); } catch (e) { /* not the hub */ }
  }
  function failed() { if (!ready) fb.hidden = false; }
  function open() {
    isOpen = true;
    panel.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
    if (!frame) {
      frame = document.createElement("iframe");
      frame.title = "Most-used programs";
      frame.referrerPolicy = "origin";
      frame.addEventListener("load", connect);
      frame.src = hub + "quick#from=" + encodeURIComponent(location.origin + location.pathname);
      panel.insertBefore(frame, fb);
      readyTimer = setTimeout(failed, 7000);
    } else {
      send({ aihubQuick: "refresh", moved: moved });
    }
    place();
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", onKey, true);
    addEventListener("resize", place);
    setTimeout(function () { try { frame.focus(); } catch (e) {} }, 30);
  }
  function close(focusBtn) {
    if (!isOpen) return;
    isOpen = false;
    panel.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", onKey, true);
    removeEventListener("resize", place);
    if (focusBtn) btn.focus();
  }
  function outside(e) { if ((e.composedPath ? e.composedPath() : [e.target]).indexOf(root0) < 0) close(false); }
  function onKey(e) { if (e.key === "Escape") { e.stopPropagation(); close(true); } }

  /* ---- click to open or close; drag to move ---- */
  var drag = null, draggedAt = 0, tapAt = 0;
  btn.addEventListener("pointerdown", function (e) {
    if (e.button !== 0) return;
    var r = btn.getBoundingClientRect();
    drag = { x: e.clientX, y: e.clientY, left: r.left, top: r.top, moving: false };
    try { btn.setPointerCapture(e.pointerId); } catch (err) {}
  });
  btn.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moving && dx * dx + dy * dy < 64) return;
    drag.moving = true;
    btn.classList.add("drag");
    var W = vw(), H = vh();
    var left = Math.min(Math.max(drag.left + dx, 4), W - SIZE - 4), top = Math.min(Math.max(drag.top + dy, 4), H - SIZE - 4);
    var right = left + SIZE / 2 > W / 2;
    pos = { side: right ? "right" : "left", e: Math.round(right ? W - left - SIZE : left), b: Math.round(H - top - SIZE) };
    moved = true;
    applyPos();
    place();
  });
  function toggle() { if (isOpen) close(false); else open(); }
  function endDrag(e) {
    if (!drag) return;
    var was = drag.moving;
    drag = null;
    btn.classList.remove("drag");
    if (!was && e.type === "pointerup" && e.pointerType && e.pointerType !== "mouse") {
      // a finger or pen: act on the tap itself. Chrome may drop the click of a tap that follows a quick flick.
      tapAt = Date.now();
      toggle();
    }
    if (was) {
      draggedAt = Date.now();   // a mouse drag ends with a click that isn't one (a finger drag doesn't)
      try { localStorage.setItem(POS_KEY, JSON.stringify(pos)); } catch (e) {}
      send({ aihubQuick: "moved", moved: true });
    }
  }
  btn.addEventListener("pointerup", endDrag);
  btn.addEventListener("pointercancel", endDrag);
  btn.addEventListener("click", function () {   // a mouse, or Enter / Space
    if (Date.now() - draggedAt < 400 || Date.now() - tapAt < 800) return;
    toggle();
  });
  addEventListener("resize", applyPos);

  (document.documentElement).appendChild(root0);   // outside <body>, so a page that rebuilds its body keeps it

  /* ---- opening this page counts as a use of its program (once per page load; the hub ignores repeats) ---- */
  if (opt.visit !== "off" && location.origin !== hubOrigin && navigator.sendBeacon && /^https?:$/.test(location.protocol)) {
    setTimeout(function () {
      try {
        navigator.sendBeacon(hub + "api/quick/use", new Blob([JSON.stringify({ url: location.origin + location.pathname, how: "visit" })],
          { type: "text/plain" }));
      } catch (e) { /* the page carries on */ }
    }, 1500);
  }
})();
