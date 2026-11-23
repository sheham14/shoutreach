"""Rules in main.css that other code relies on.

Run:  python tests/test_css.py

1. The hidden attribute always wins (Plan 3 review, R1). Several elements
   are shown and hidden with el.hidden (the run-now button, WhatsApp's crumb,
   its "N marked not on WhatsApp" link, the Leads filter note). A later
   `display:` rule on one of them beats the browser's own [hidden] style, and
   a phone rule did exactly that: "0 marked not on WhatsApp" was drawn while
   hidden. A top-level `[hidden] { display: none !important; }` stops any
   rule from doing it again.
2. No element that the scripts show with el.hidden is also shown with
   style.display, which the rule above would defeat.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def top_level_rules(css):
    """(selector, body) for rules not inside any @-block."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, depth, i, start = [], 0, 0, 0
    sel_start = 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            if depth == 0:
                selector = css[sel_start:i].strip()
                start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append((selector, css[start:i]))
                sel_start = i + 1
        i += 1
    return out


def main():
    css = open(os.path.join(ROOT, "static", "css", "main.css"), encoding="utf-8").read()

    print("\n1. [hidden] ALWAYS WINS")
    rules = [(s, b) for s, b in top_level_rules(css) if not s.startswith("@")]
    hidden = [b for s, b in rules if s == "[hidden]"]
    check("a top-level [hidden] rule exists (not inside @media)", bool(hidden))
    check("it is display: none !important",
          any(re.search(r"display\s*:\s*none\s*!important", b) for b in hidden),
          hidden[0].strip() if hidden else "")

    print("\n2. NOTHING SHOWN WITH el.hidden IS ALSO SHOWN WITH style.display")
    js = {p: open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(ROOT, "static", "js", "*.js"))}
    toggled = set()
    for src in js.values():
        # `const note = document.getElementById('x'); ... note.hidden = ...`
        for var, el_id in re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*document\.getElementById\(['\"]([\w-]+)['\"]\)", src):
            if re.search(rf"\b{var}\.hidden\s*=", src):
                toggled.add((var, el_id))
    clash = []
    for var, el_id in toggled:
        for path, src in js.items():
            if re.search(rf"getElementById\(['\"]{el_id}['\"]\)\.style\.display", src):
                clash.append(f"{el_id} in {os.path.basename(path)}")
    check(f"{len(toggled)} elements toggled with .hidden; none also set with style.display",
          len(toggled) >= 3 and not clash, "; ".join(clash))

    print()
    if _failures:
        print(f"FAILED: {len(_failures)}")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("All CSS checks passed.")


if __name__ == "__main__":
    main()
