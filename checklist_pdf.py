#!/usr/bin/env python3
"""A checklist run as a PDF: every step with its result, the reply, the screenshots and Claude's answer.

Reads the checklist from a hub (the server's has the checklists) and prints it with Playwright's Chromium,
so run it with the Playwright venv on the main PC:

  ~/pwvenv/bin/python checklist_pdf.py <hub url> <checklist id> <out.pdf>
  e.g. ~/pwvenv/bin/python checklist_pdf.py http://192.168.50.235:8090/ ttr-test-run-2026-11-11-run1 run1.pdf

Then copy the PDF into the docs folder (it shows on the Docs tab) and link it from the run with
`python3 checklists.py run-pdf <id> <saved run id> "<file name>"` on the server.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hub_auth  # noqa: E402  the machine key (hub.key) gets past the hub's login

RES = {"pass": ("WORKS", "pass"), "fail": ("PROBLEM", "fail"), "skip": ("SKIPPED", "skip")}


def md(s):
    """The little Markdown the checklists use: **bold**, *italic*, `code`, [text](url)."""
    s = html.escape(str(s or ""))
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)\*(?!\w)", r"<i>\1</i>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1", s)
    return s


def when(t):
    return time.strftime("%-d %b %Y, %-I:%M %p", time.localtime(t)).replace("AM", "am").replace("PM", "pm") if t else ""


def build(base, cid):
    req = urllib.request.Request(base + "api/checklists/" + urllib.parse.quote(cid), headers=hub_auth.key_header())
    with urllib.request.urlopen(req) as r:
        g = json.load(r)
    d, st, c = g["list"], g["state"]["items"], g["counts"]
    arch = d.get("archived") or {}
    shots_n = sum(len(v.get("shots") or []) for v in st.values())
    items = [(si, s, it) for si, s in enumerate(d["sections"]) for it in s.get("items", [])]
    problems = [(it, st.get(it["id"], {})) for _, _, it in items if st.get(it["id"], {}).get("result") == "fail"]
    out = ["""<!doctype html><html><head><meta charset="utf-8"><title>%s</title><style>
@page { size: A4; margin: 14mm 13mm 16mm; }
body { font: 10.5pt/1.45 "Noto Sans", "DejaVu Sans", Arial, sans-serif; color: #1d232b; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 4px; line-height: 1.15; }
.eyebrow { font-size: 8pt; letter-spacing: .08em; text-transform: uppercase; color: #6b7480; font-weight: 700; }
.sum { margin: 8px 0 14px; padding: 9px 12px; background: #f3f5f8; border: 1px solid #d9dee5; border-radius: 8px; }
.sum b { font-size: 12pt; }
.bad { color: #c62828; font-weight: 700; }
.toc { margin: 0 0 6px; padding-left: 18px; columns: 2; column-gap: 24px; font-size: 9.5pt; }
h2 { font-size: 13.5pt; margin: 18px 0 2px; padding-bottom: 3px; border-bottom: 2px solid #1d232b; break-after: avoid; }
.note { color: #5b6470; font-size: 9pt; margin: 4px 0 6px; }
.step { padding: 9px 0 10px; border-top: 1px solid #e1e5ea; }
.step h3 { font-size: 11pt; margin: 0; display: flex; gap: 8px; align-items: baseline; break-after: avoid; }
.id { font: 600 8.5pt ui-monospace, "DejaVu Sans Mono", monospace; color: #6b7480; min-width: 26px; }
.badge { font-size: 7.5pt; font-weight: 800; letter-spacing: .05em; padding: 1px 6px; border-radius: 4px; background: #e6e9ee; color: #4b5563; white-space: nowrap; }
.badge.pass { background: #e3f3e6; color: #22863a; } .badge.fail { background: #fbe4e4; color: #c62828; } .badge.skip { background: #eceff3; color: #6b7480; }
.by { margin-left: auto; font-size: 8pt; color: #8a929c; white-space: nowrap; font-weight: 400; }
.what { font-size: 8.8pt; color: #5b6470; margin: 4px 0 0 34px; }
.what ul, .what ol { margin: 2px 0; padding-left: 16px; }
.reply { margin: 6px 0 0 34px; padding: 6px 10px; border-left: 3px solid #1d232b; background: #fafbfc; white-space: pre-wrap; overflow-wrap: anywhere; }
.reply .lab, .answer .lab { display: block; font-size: 7.5pt; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; color: #6b7480; }
.answer { margin: 6px 0 0 34px; padding: 6px 10px; border-left: 3px solid #1565c0; background: #eef4fc; white-space: pre-wrap; overflow-wrap: anywhere; }
.answer .lab { color: #1565c0; }
.shots { margin: 6px 0 0 34px; display: grid; gap: 6px; }
.shots figure { margin: 0; break-inside: avoid; }
.shots img { max-width: 100%%; max-height: 120mm; border: 1px solid #d9dee5; border-radius: 4px; }
.shots figcaption { font-size: 7.5pt; color: #8a929c; }
code { font-size: 9pt; background: #f1f3f6; padding: 0 3px; border-radius: 3px; }
</style></head><body>""" % html.escape(d.get("title") or cid)]
    out.append('<div class="eyebrow">%s</div><h1>%s</h1>' % (md(d.get("eyebrow") or "AI Hub checklist"), md(d.get("title") or cid)))
    saved = ("Saved %s. " % when(arch["at"])) if arch.get("at") else ("Printed %s. " % when(time.time()))
    out.append('<div class="sum">%s<b>%d of %d</b> steps done · <span class="%s">%d problem%s</span> · %d repl%s · %d screenshot%s</div>' % (
        saved, c["done"], c["total"], "bad" if c["problems"] else "", c["problems"], "" if c["problems"] == 1 else "s",
        c["replies"], "y" if c["replies"] == 1 else "ies", shots_n, "" if shots_n == 1 else "s"))
    if problems:
        out.append('<div class="eyebrow">Problems</div><ol class="toc">%s</ol>' % "".join(
            "<li><b>%s</b> %s</li>" % (html.escape(it["id"]), md(it.get("title"))) for it, _ in problems))
    cur = None
    for si, sec, it in items:
        if sec is not cur:
            cur = sec
            out.append("<h2>Part %d: %s</h2>" % (si + 1, md(sec.get("title"))))
            if sec.get("note"):
                out.append('<div class="note">%s</div>' % md(sec["note"]))
        s = st.get(it["id"], {})
        lab, cls = RES.get(s.get("result"), ("DONE", "") if s.get("done") else ("NOT DONE", ""))
        by = " · ".join(x for x in (s.get("by") or "", when(s.get("at"))) if x)
        out.append('<div class="step"><h3><span class="id">%s</span>%s <span class="badge %s">%s</span><span class="by">%s</span></h3>' % (
            html.escape(it["id"]), md(it.get("title")), cls, lab, html.escape(by)))
        what = ""
        if it.get("do"):
            what += "<ol>%s</ol>" % "".join("<li>%s</li>" % md(x) for x in it["do"])
        if it.get("expect"):
            what += "Should happen:<ul>%s</ul>" % "".join("<li>%s</li>" % md(x) for x in it["expect"])
        if what:
            out.append('<div class="what">%s</div>' % what)
        if str(s.get("reply") or "").strip():
            out.append('<div class="reply"><span class="lab">Reply</span>%s</div>' % html.escape(s["reply"].strip()))
        shots = s.get("shots") or []
        if shots:
            out.append('<div class="shots">%s</div>' % "".join(
                '<figure><img src="%s"><figcaption>Screenshot %d%s</figcaption></figure>' % (
                    html.escape(base + "api/checklists/%s/shots/%s" % (urllib.parse.quote(cid), urllib.parse.quote(x["f"]))),
                    n + 1, (" · " + html.escape(when(x.get("at")))) if x.get("at") else "") for n, x in enumerate(shots)))
        if s.get("answer"):
            out.append('<div class="answer"><span class="lab">%s%s</span>%s</div>' % (
                html.escape(s.get("answer_by") or "Claude"), (" · " + html.escape(when(s.get("answer_at")))) if s.get("answer_at") else "",
                html.escape(s["answer"])))
        out.append("</div>")
    out.append("</body></html>")
    return "".join(out), d.get("title") or cid


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    base, cid, dest = argv
    base = base.rstrip("/") + "/"
    page_html, title = build(base, cid)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(extra_http_headers=hub_auth.key_header())   # the screenshots come from the hub too
        pg.set_content(page_html, wait_until="networkidle")
        pg.evaluate("Promise.all([...document.images].map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; })))")
        pg.pdf(path=dest, format="A4", print_background=True, display_header_footer=True,
               header_template="<span></span>",
               footer_template='<div style="font-size:7pt;color:#8a929c;width:100%%;text-align:center">%s · page <span class="pageNumber"></span> of <span class="totalPages"></span></div>' % html.escape(title))
        b.close()
    print("Wrote", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
