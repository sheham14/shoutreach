"""The icon set and the Hexiv mark (Plan 3, package 3).

Run:  python tests/test_icons.py

Icons live in one place, static/js/icons.js, as Lucide path data. The JS
helper icon() draws them in the browser; app.py parses the same object for
the template macro in templates/macros/icons.html. A name that isn't in the
set draws nothing (the JS helper only logs a warning), so a typo would ship
as a missing icon with no error anywhere. This pins:

  1. app.py reads the set, and it matches what icons.js holds.
  2. Every icon name used in a template or a script is in the set.
  3. The signed-in page and the sign-in page carry the mark and the mark as
     the browser-tab icon, and no emoji favicon or "Powered by Hexiv".
  4. The sidebar draws its items with the icon set, not emoji or symbols.
  5. The Lucide licence ships next to the icons.
"""
import glob
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def main():
    work = tempfile.mkdtemp(prefix="icons-")
    os.environ["DB_PATH"] = os.path.join(work, "t.db")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    os.environ["SHOUTREACH_NO_SCHEDULER"] = "1"
    os.environ["SHOUTREACH_INSECURE_COOKIES"] = "1"
    os.environ.pop("ADMIN_PASS", None)

    import db
    import app as app_mod

    print("\n1. THE SET LOADS, FROM ONE PLACE")
    src = read("static/js/icons.js")
    m = re.search(r"/\* ICONS-JSON-START \*/\s*const ICONS = (\{.*?\});\s*/\* ICONS-JSON-END \*/", src, re.S)
    check("icons.js has the JSON block between its markers", bool(m))
    in_js = json.loads(m.group(1)) if m else {}
    loaded = app_mod.app.jinja_env.globals.get("ICONS") or {}
    check(f"app.py loaded {len(loaded)} icons", len(loaded) >= 30)
    check("and they are exactly the ones in icons.js", loaded == in_js)
    check("every entry is SVG elements only", all(
        v.startswith("<") and "<svg" not in v and "<script" not in v.lower() for v in loaded.values()))

    print("\n2. EVERY NAME USED IS IN THE SET")
    used = {}
    for path in glob.glob(os.path.join(ROOT, "templates", "**", "*.html"), recursive=True) + \
            glob.glob(os.path.join(ROOT, "static", "js", "*.js")):
        text = open(path, encoding="utf-8").read()
        # The first argument, and both arms of any ?: inside it (sort arrows
        # nest one ternary in another). Quoted names after === are values
        # being compared, not icons.
        for m in re.finditer(r"\bicon\(", text):
            arg = text[m.end():m.end() + 160]
            arg = re.split(r",\s*\{|,\s*\d|\)\s*[}`;+]", arg)[0]
            for q in re.finditer(r"""(^|\?|:)\s*\(?\s*['"]([a-z0-9-]+)['"]""", arg):
                used.setdefault(q.group(2), set()).add(os.path.relpath(path, ROOT))
    missing = {n: sorted(w) for n, w in used.items() if n not in loaded}
    check(f"{len(used)} names used, none missing", len(used) >= 20 and not missing, str(missing))
    unused = sorted(set(loaded) - set(used))
    check("nothing in the set goes unused", not unused, str(unused))

    print("\n3. THE MARK: SIDEBAR, SIGN-IN PAGE, BROWSER TAB")
    uid = db.create_user("iconuser", "test-password-123", is_admin=True)
    c = app_mod.app.test_client()
    r = c.get("/login")
    login = r.get_data(as_text=True)
    with c.session_transaction() as sess:
        sess["user_id"] = uid
        sess["csrf_token"] = "t"
    page = c.get("/").get_data(as_text=True)
    for label, html in (("sign-in page", login), ("app page", page)):
        check(f"{label}: tab icon is the mark",
              re.search(r'<link rel="icon"[^>]*hexiv-mark\.svg', html) is not None)
        check(f"{label}: the mark beside the name", 'class="logo-hex"' in html)
        check(f"{label}: no emoji tab icon, no 'Powered by Hexiv'",
              "font-size='90'" not in html and "Powered by Hexiv" not in html)
        check(f"{label}: no icon call printed as text (a raw block swallows it)",
              "{{ icon(" not in html and "icon(\\'" not in html)
    r = app_mod.app.test_client().get("/static/img/hexiv-mark.svg")
    check("the mark loads signed out", r.status_code == 200, f"got {r.status_code}")
    r.close()

    print("\n4. THE SIDEBAR USES THE SET")
    nav = page[page.find('<aside class="sidebar">'):page.find("</aside>")]
    svgs = nav.count('<svg class="icon"')
    check(f"sidebar draws {svgs} icons (9 items, run-now, sign out)", svgs >= 11)
    leftover = re.findall(r"[←-⇿⊕-⋿⌀-⏿■-⟿\U0001F300-\U0001FAFF]", nav)
    check("no symbol or emoji left in the sidebar", not leftover, repr(leftover))
    check("the JS helper is loaded after utils.js (it uses esc)",
          page.find("js/utils.js") < page.find("js/icons.js") < page.find("js/app.js"))

    print("\n5. LICENCE")
    lic = read("static/js/Lucide-LICENSE.txt")
    check("Lucide's ISC licence is shipped", "ISC License" in lic and "Lucide Contributors" in lic)

    print()
    if _failures:
        print(f"FAILED: {len(_failures)}")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("All icon checks passed.")


if __name__ == "__main__":
    main()
