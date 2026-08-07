"""Template variable rendering and the help text that documents it.

Run:  python tests/test_rendering.py

Two bugs are pinned here.

The renderer replaced only the keys it knew about, so a placeholder with no
matching value was delivered to the prospect with its braces intact -- a typo
like {{firstname}} shipped as literal text. There was also no way to say what
should happen when a field is blank, which matters because scraped lists
mostly have a company and no first name, and "Hi ," reads worse than no
personalisation at all.

Separately, the UI hints listing the available variables are written in Jinja
templates. Jinja evaluated {{first_name}} as an undefined variable and rendered
it as nothing, so the label read "Subject — use , ," and the Help page's
variable reference was blank -- the variable names were the one thing the user
could not see.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def main():
    os.environ.setdefault("DB_PATH", os.path.join(
        tempfile.mkdtemp(prefix="rendering-"), "t.db"))
    os.environ.setdefault("SECRET_KEY", "test-secret")

    import sender

    named = {"first_name": "Sarah", "last_name": "Jones",
             "company": "Wellness Clinic", "email": "s@wc.ca", "extra": "{}"}
    anon  = {"first_name": "", "last_name": "",
             "company": "Paradise Dental", "email": "info@pd.ca", "extra": "{}"}

    print("\n1. PLAIN SUBSTITUTION STILL WORKS")
    check("a known field is replaced",
          sender._render("Hi {{first_name}},", named) == "Hi Sarah,")
    check("several in one line",
          sender._render("{{first_name}} at {{company}}", named)
          == "Sarah at Wellness Clinic")
    check("full_name is derived",
          sender._render("{{full_name}}", named) == "Sarah Jones")
    check("text with no placeholders is untouched",
          sender._render("No placeholders here", named) == "No placeholders here")

    print("\n2. FALLBACKS COVER THE MISSING-FIELD CASE")
    check("a present value wins over its fallback",
          sender._render("Hi {{first_name|there}},", named) == "Hi Sarah,")
    check("a blank value uses the fallback",
          sender._render("Hi {{first_name|there}},", anon) == "Hi there,")
    check("without a fallback a blank value leaves the bare sentence",
          sender._render("Hi {{first_name}},", anon) == "Hi ,")
    check("the spaced-out style does not leak padding",
          sender._render("Hi {{ first_name | there }},", anon) == "Hi there,",
          repr(sender._render("Hi {{ first_name | there }},", anon)))
    check("an empty fallback renders as nothing",
          sender._render("[{{first_name|}}]", anon) == "[]")
    check("a multi-word fallback survives intact",
          sender._render("{{full_name|your team}}", anon) == "your team")

    print("\n3. AN UNKNOWN PLACEHOLDER NEVER REACHES THE PROSPECT")
    out = sender._render("Typo: {{firstname}} here", named)
    check("the braces are gone", "{{" not in out and "}}" not in out, repr(out))
    check("it collapses to nothing", out == "Typo:  here", repr(out))
    check("an unknown name can still take a fallback",
          sender._render("{{firstname|friend}}", named) == "friend")

    print("\n4. THE THREE SOURCES AND THEIR PRIORITY")
    check("campaign variables resolve",
          sender._render("Book {{link}}", anon, {"link": "cal.com/hexiv"})
          == "Book cal.com/hexiv")
    check("a campaign variable can have a fallback too",
          sender._render("Book {{link|our site}}", anon, {}) == "Book our site")
    check("a contact extra field resolves",
          sender._render("{{rating}}", {"first_name": "", "extra": '{"rating":"4.8"}'})
          == "4.8")
    check("a standard field beats a campaign variable of the same name",
          sender._render("{{company}}", anon, {"company": "Wrong Co"})
          == "Paradise Dental")

    print("\n5. THE UI ACTUALLY SHOWS THE VARIABLE NAMES")
    import app as app_mod
    expected = {
        "modals/step_editor.html": ["{{first_name}}", "{{company}}", "{{first_name|there}}"],
        "sections/help.html":      ["{{first_name}}", "{{company}}", "{{job_title}}",
                                    "{{first_name|there}}"],
    }
    with app_mod.app.app_context():
        for tpl, needles in expected.items():
            html = app_mod.app.jinja_env.get_template(tpl).render()
            for needle in needles:
                check(f"{tpl} shows {needle}", needle in html)
            check(f"{tpl} is not left with an empty variable list",
                  "use , ," not in html)

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
