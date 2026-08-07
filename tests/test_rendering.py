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

    print("\n5. THE SCRAPED QUALIFYING FIELDS ARE USABLE AS VARIABLES")
    # These were reachable as {{phone}} while the scraper packed them into the
    # `extra` blob. Promoting them to real columns emptied that blob, so unless
    # they are selected and named explicitly they stop resolving -- a working
    # variable retired by a change that looked unrelated.
    scraped = {"first_name": "", "company": "Paradise Dental", "email": "i@pd.ca",
               "phone": "709-555-0111", "category": "Dentist",
               "rating": 4.8, "review_count": 127,
               "website": "https://pd.ca", "address": "12 Main St", "extra": "{}"}
    check("phone resolves", sender._render("{{phone}}", scraped) == "709-555-0111")
    check("category resolves", sender._render("{{category}}", scraped) == "Dentist")
    check("rating resolves", sender._render("{{rating}}", scraped) == "4.8")
    check("review_count resolves", sender._render("{{review_count}}", scraped) == "127")
    check("website resolves", sender._render("{{website}}", scraped) == "https://pd.ca")
    check("a missing one still takes its fallback",
          sender._render("{{phone|our website}}", {"first_name": "", "extra": "{}"})
          == "our website")

    print("\n6. THE SEND QUERY ACTUALLY SUPPLIES THEM")
    # Rendering can only use what get_due_enrollments selects, so assert the
    # column list rather than trusting the two to stay in step.
    import inspect
    import db as db_mod
    src = inspect.getsource(db_mod.get_due_enrollments)
    for col in ("c.first_name", "c.last_name", "c.company", "c.extra",
                "c.phone", "c.category", "c.rating", "c.review_count"):
        check(f"the due-send query selects {col}", col in src)

    print("\n7. THE UI ACTUALLY SHOWS THE VARIABLE NAMES")
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

    print("\n8. VARIABLE COVERAGE IS COUNTED OVER THE RIGHT POPULATION")
    import importlib
    import shutil
    import tempfile as _tempfile

    work = _tempfile.mkdtemp(prefix="coverage-")
    os.environ["DB_PATH"] = os.path.join(work, "cov.db")
    import db as db2
    importlib.reload(db2)
    db2.init_db()
    try:
        # A scraped list: company and phone for everyone, never a first name.
        db2.upsert_contacts([
            {"email": f"s{i}@scraped{i}.ca", "company": f"Scraped {i}",
             "website": f"https://scraped{i}.ca", "phone": f"555-000{i}"}
            for i in range(8)
        ])
        # Hand-added contacts that do have names -- these are what make the
        # global number look healthier than any single campaign really is.
        db2.upsert_contacts([
            {"email": "a@named.ca", "first_name": "Ada", "last_name": "L",
             "company": "Named Co", "website": "https://named.ca"},
            {"email": "b@named2.ca", "first_name": "Bo", "last_name": "K",
             "company": "Named Two", "website": "https://named2.ca"},
        ])

        every = db2.get_variable_coverage(None)
        by_key = {v["key"]: v for v in every["variables"]}
        check("global scope counts every contact", every["total"] == 10,
              str(every["total"]))
        check("first_name coverage is the 2 hand-added ones",
              by_key["first_name"]["filled"] == 2, str(by_key["first_name"]))
        check("company is complete", by_key["company"]["filled"] == 10)
        check("phone is only the scraped ones", by_key["phone"]["filled"] == 8,
              str(by_key["phone"]))
        check("full_name counts as present when either half is",
              by_key["full_name"]["filled"] == 2)

        # A campaign built purely from the scrape is 0% first name, even though
        # the database as a whole is 20%. That difference is the whole point.
        cid = db2.create_campaign("Scraped only")
        db2.upsert_step(cid, 1, "s", "b", 0)
        with db2.get_db() as conn:
            scraped_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM contacts WHERE email LIKE 's%@scraped%'").fetchall()]
        db2.enroll_contacts_bulk(cid, scraped_ids)

        camp = db2.get_variable_coverage(cid)
        ck = {v["key"]: v for v in camp["variables"]}
        check("campaign scope is reported", camp["scope"] == "campaign", camp["scope"])
        check("it counts only enrolled contacts", camp["total"] == 8, str(camp["total"]))
        check("first_name is 0 for this campaign, not 2",
              ck["first_name"]["filled"] == 0, str(ck["first_name"]))
        check("company is still complete", ck["company"]["filled"] == 8)

        empty_cid = db2.create_campaign("Nobody enrolled")
        fallback = db2.get_variable_coverage(empty_cid)
        check("a campaign with nobody enrolled falls back to all contacts",
              fallback["scope"] == "all" and fallback["total"] == 10,
              f"{fallback['scope']}/{fallback['total']}")

        # Advertising a variable the send query never fetches would put a name
        # in the panel that always renders as nothing.
        import re as _re2
        send_src  = inspect.getsource(db2.get_due_enrollments)
        selected  = set(_re2.findall(r"\bc\.([a-z_]+)", send_src)) | {"full_name"}
        advertised = {k for k, _ in db2.TEMPLATE_VARIABLES}
        missing   = advertised - selected
        check("every advertised variable is one the send query supplies",
              not missing, f"not fetched: {sorted(missing)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n9. THE UI'S PLACEHOLDER PARSER MATCHES THE RENDERER")
    # The gap warning reads the copy with its own regex in JavaScript. Pull the
    # literal out of the shipped file rather than restating it, so the two
    # cannot quietly diverge and start disagreeing about what has a fallback.
    import re as _re
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "static", "js", "campaigns.js"), encoding="utf-8").read()
    m = _re.search(r"const re = /(.+?)/g;", js)
    check("the parser regex is found in campaigns.js", m is not None)
    if m:
        pattern = _re.compile(m.group(1))
        cases = [
            ("Hi {{first_name}},",            "first_name", False),
            ("Hi {{first_name|there}},",      "first_name", True),
            ("Hi {{ first_name | there }},",  "first_name", True),
            ("{{company}}",                   "company",    False),
            ("{{first_name|}}",               "first_name", False),  # empty = no real fallback
        ]
        for text, key, expect_fallback in cases:
            hit = pattern.search(text)
            got_key = hit.group(1) if hit else None
            got_fb  = bool(hit and hit.group(2) is not None and (hit.group(3) or "").strip())
            check(f"{text!r} -> {key}, fallback={expect_fallback}",
                  got_key == key and got_fb == expect_fallback,
                  f"got {got_key}, fallback={got_fb}")
        check("both sides agree a bare variable has no fallback",
              sender._render("Hi {{first_name}},", {"first_name": "", "extra": "{}"})
              == "Hi ,")

    check("saving a step runs the gap check before posting",
          "_variableGaps()" in js.split("async function saveStep")[1].split("await api")[0])

    print("\n" + ("ALL PASS" if not _failures else f"FAILURES: {_failures}"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
