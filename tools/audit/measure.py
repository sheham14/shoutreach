"""Measured checks on the harness copy: tap targets, overflow, type and colour
spread, page weight, keyboard focus, modal behaviour, first-row position."""
import json
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5055"
HERE = os.path.dirname(os.path.abspath(__file__))

TARGETS_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'
      && r.bottom > 0 && r.top < innerHeight * 3; };
  const top = el => { // only what's actually on top (not under a sheet/modal)
    const r = el.getBoundingClientRect();
    const x = Math.min(innerWidth - 1, Math.max(0, r.left + r.width / 2));
    const y = r.top + r.height / 2;
    if (y < 0 || y > innerHeight) return true;   // off-screen: can't hit-test, keep
    const hit = document.elementFromPoint(x, y);
    return hit && (el === hit || el.contains(hit) || hit.contains(el));
  };
  const els = [...document.querySelectorAll('button, a, input, select, textarea, summary, [onclick]')]
    .filter(vis).filter(top);
  const rows = els.map(el => { const r = el.getBoundingClientRect();
    return { tag: el.tagName.toLowerCase(), cls: (el.className || '').toString().slice(0, 40),
             text: (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 28),
             w: Math.round(r.width), h: Math.round(r.height) }; });
  const small44 = rows.filter(r => r.h < 44 || r.w < 44);
  const small24 = rows.filter(r => r.h < 24 || r.w < 24);
  const sizes = {}; const fams = {}; const colors = {};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = walker.nextNode())) {
    if (!n.nodeValue.trim()) continue; const p = n.parentElement; if (!p || !vis(p) || !top(p)) continue;
    const r = p.getBoundingClientRect(); if (r.top > innerHeight || r.bottom < 0) continue;
    const cs = getComputedStyle(p);
    sizes[cs.fontSize] = (sizes[cs.fontSize] || 0) + 1;
    fams[cs.fontFamily.split(',')[0]] = (fams[cs.fontFamily.split(',')[0]] || 0) + 1;
    colors[cs.color] = (colors[cs.color] || 0) + 1;
  }
  return { total: rows.length, under44: small44.length, under24: small24.length,
           worst: small44.sort((a, b) => a.h - b.h).slice(0, 14),
           overflowX: document.documentElement.scrollWidth - innerWidth,
           fontSizesOnScreen: sizes, families: fams, textColoursOnScreen: Object.keys(colors).length,
           pageHeight: document.documentElement.scrollHeight, vh: innerHeight };
}
"""


def login(page, user="sheham"):
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', "audit-password-123")
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def settle(page, ms=500):
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def phone(p):
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                        has_touch=True)
    ctx.add_init_script("window.open = () => ({focus(){}}); window.confirm = () => true;")
    page = ctx.new_page()
    login(page)
    page.evaluate("() => { window.openWhatsAppChat = () => {}; }")
    out = {}

    def take(name, js=None, tap=None, first_row=None):
        if js:
            page.evaluate(f"async () => {{ {js} }}")
        settle(page)
        if tap:
            try:
                page.locator(tap).first.tap(timeout=4000)
            except Exception as exc:
                out[name] = {"error": str(exc).splitlines()[0][:120]}
                return
            settle(page)
        m = page.evaluate(TARGETS_JS)
        if first_row:
            m["firstRowTop"] = page.evaluate(
                "(sel) => { const e = document.querySelector(sel); return e ? Math.round(e.getBoundingClientRect().top) : null; }",
                first_row)
            m["rowsVisible"] = page.evaluate(
                "(sel) => { const rows = [...document.querySelectorAll(sel.replace(/:nth-child\\(1\\)|:first-child/, ''))];"
                " return rows.filter(r => { const b = r.getBoundingClientRect(); return b.top >= 0 && b.bottom <= innerHeight; }).length; }",
                first_row)
        out[name] = m

    take("dashboard", "showSection('dashboard')")
    take("wa-todo", "showSection('whatsapp'); setTab('whatsapp','todo')", first_row="#wa-queue tr:nth-child(1)")
    take("wa-todo-lead", tap="#wa-queue tr:nth-child(2) td.m-card")
    take("wa-todo-did-it-send", tap="#wa-actions .btn-primary")
    take("wa-leads", "closeAllSheets(); setTab('whatsapp','leads')", first_row="#wl-body tbody tr:nth-child(1)")
    take("wa-leads-panel", tap="#wl-body tbody tr:nth-child(2) td.m-card")
    take("wa-booked", "closeAllSheets(); setTab('whatsapp','booked')")
    take("wa-messages", "setTab('whatsapp','templates')")
    take("wa-campaigns", "setTab('whatsapp','campaigns')")
    take("calling-todo", "showSection('calling'); setTab('calling','todo')", first_row="#cq-table tr:nth-child(1)")
    take("calling-lead", tap="#cq-table tr:nth-child(1) td.m-card")
    take("contacts", "closeAllSheets(); showSection('contacts')", first_row="#section-contacts tbody tr:nth-child(1)")
    take("email-todo", "showSection('email'); setTab('email','todo')", first_row="#er-body tbody tr:nth-child(1)")
    take("email-campaigns", "setTab('email','campaigns')")
    take("getleads-import", "showSection('scraper'); setTab('scraper','import')")
    take("settings", "showSection('settings')")
    # is the tab strip scrollable with something hidden, and is there any hint?
    out["wa-tabs"] = page.evaluate("""() => { showSection('whatsapp'); const t = document.querySelector('[data-tab-group=whatsapp]');
        return { scrollWidth: t.scrollWidth, clientWidth: t.clientWidth,
                 hidden: [...t.children].filter(c => c.getBoundingClientRect().right > innerWidth).map(c => c.innerText.trim()) }; }""")
    b.close()
    return out


def desktop(p):
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    reqs = []
    page.on("response", lambda r: reqs.append((r.url.replace(BASE, ""), r.status,
                                               int(r.headers.get("content-length", 0) or 0),
                                               r.headers.get("cache-control", ""),
                                               r.headers.get("content-encoding", ""))))
    login(page)
    settle(page, 1200)
    out = {}
    first = list(reqs)
    out["load"] = {
        "requests": len(first),
        "api": [u for u, *_ in first if u.startswith("/api/")],
        "static": [(u, n, cc, enc) for u, s, n, cc, enc in first if u.startswith("/static/")],
        "external": [u[:70] for u, *_ in first if u.startswith("http")],
        "html_bytes": next((n for u, s, n, cc, enc in first if u == "/"), None),
    }
    out["dom"] = page.evaluate("""() => ({ nodes: document.querySelectorAll('*').length,
        inlineStyle: document.querySelectorAll('[style]').length,
        onclick: document.querySelectorAll('[onclick]').length,
        aria: document.querySelectorAll('[aria-label],[aria-labelledby],[role]').length,
        inputs: document.querySelectorAll('input,select,textarea').length,
        labelled: [...document.querySelectorAll('input,select,textarea')].filter(e => e.labels && e.labels.length || e.getAttribute('aria-label')).length,
        buttonsNoText: [...document.querySelectorAll('button')].filter(b => !(b.innerText||'').trim().match(/[A-Za-z]{2,}/) && !b.getAttribute('aria-label') && !b.title).length,
        clickableNonButtons: document.querySelectorAll('a[onclick]:not([href]), span[onclick], div[onclick], tr[onclick], td[onclick]').length,
        headings: ['h1','h2','h3','h4'].map(h => [h, document.querySelectorAll(h).length]),
        lang: document.documentElement.lang })""")

    # polling: which API calls repeat while sitting on each screen for 20s
    def idle(name, js):
        page.evaluate(f"async () => {{ {js} }}")
        settle(page, 800)
        mark = len(reqs)
        page.wait_for_timeout(20000)
        calls = [u.split("?")[0] for u, *_ in reqs[mark:] if u.startswith("/api/")]
        agg = {}
        for c in calls:
            agg[c] = agg.get(c, 0) + 1
        out.setdefault("polling20s", {})[name] = agg

    def on_open(name, js):
        mark = len(reqs)
        page.evaluate(f"async () => {{ {js} }}")
        settle(page, 900)
        calls = [u for u, *_ in reqs[mark:] if u.startswith("/api/")]
        out.setdefault("callsOnOpen", {})[name] = calls

    on_open("dashboard", "showSection('dashboard')")
    on_open("contacts", "showSection('contacts')")
    on_open("email", "showSection('email')")
    on_open("calling", "showSection('calling')")
    on_open("whatsapp", "showSection('whatsapp'); setTab('whatsapp','todo')")
    on_open("wa-leads", "setTab('whatsapp','leads')")
    on_open("getleads", "showSection('scraper')")
    on_open("settings", "showSection('settings')")
    idle("dashboard", "showSection('dashboard')")
    idle("getleads", "showSection('scraper')")
    idle("whatsapp", "showSection('whatsapp')")

    # keyboard: tab through WhatsApp To do, record where focus goes and whether it is visible
    page.evaluate("async () => { showSection('whatsapp'); setTab('whatsapp','todo') }")
    settle(page)
    page.mouse.click(5, 5)
    trail = []
    for _ in range(22):
        page.keyboard.press("Tab")
        trail.append(page.evaluate("""() => { const e = document.activeElement; if (!e) return null;
            const cs = getComputedStyle(e); const r = e.getBoundingClientRect();
            return { el: e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + '.' + (e.className||'').toString().split(' ')[0],
                     text: (e.innerText || e.placeholder || '').trim().slice(0, 22),
                     outline: cs.outlineStyle + ' ' + cs.outlineWidth, shadow: cs.boxShadow !== 'none',
                     border: cs.borderColor, onScreen: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 }; }"""))
    out["tabTrail"] = trail
    # can a table row / lead be opened by keyboard?
    out["rowsFocusable"] = page.evaluate("() => [...document.querySelectorAll('#wa-queue tr')].filter(r => r.tabIndex >= 0).length")
    # modal: Esc, focus moves in, focus trapped?
    page.evaluate("() => openWaImportModal()")
    settle(page)
    in_modal = page.evaluate("() => !!document.activeElement.closest('.modal')")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    still = page.evaluate("() => document.querySelectorAll('.modal-overlay.open').length")
    out["modal"] = {"focusMovedIntoModal": in_modal, "openAfterEsc": still,
                    "role": page.evaluate("() => document.querySelector('.modal-overlay .modal').getAttribute('role')"),
                    "clickOutsideCloses": None}
    page.evaluate("() => document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'))")
    b.close()
    return out


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    with sync_playwright() as p:
        res = {}
        if which in ("phone", "both"):
            res["phone"] = phone(p)
        if which in ("desktop", "both"):
            res["desktop"] = desktop(p)
    out = os.path.join(os.path.join(tempfile.gettempdir(), "shoutreach-audit"), f"measure-{which}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(res, open(out, "w", encoding="utf-8"), indent=1)
    print("written", out)
