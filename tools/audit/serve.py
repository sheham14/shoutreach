"""Audit harness: a throwaway ShoutReach with realistic fake data.

Never touches ./outreach.db. The scheduler is stubbed so nothing sends, and
the audit checks are stubbed so nothing reaches the internet.

    python serve.py seed     # build the scratch database
    python serve.py          # serve it on 127.0.0.1:5055
"""
import datetime as dt
import json
import os
import random
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
# Everything this writes goes to the system temp folder, never the repo.
WORK = os.path.join(tempfile.gettempdir(), "shoutreach-audit")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "audit.db")

os.environ["DB_PATH"] = DB
os.environ["SECRET_KEY"] = "audit-harness-secret"
os.environ["SHOUTREACH_INSECURE_COOKIES"] = "1"
os.environ.pop("ADMIN_PASS", None)
os.chdir(WORK)                      # anything written relative lands here
sys.path.insert(0, REPO)

import scheduler  # noqa: E402
scheduler.start = lambda *a, **k: None

import audit  # noqa: E402

CANNED_AUDIT = {
    "site": {"ok": True, "status": 200, "final_url": "https://example-clinic.ae/", "https": True,
             "load_ms": 2840, "page_kb": 612, "title": "Al Noor Dental & Orthodontic Centre | Dubai Marina",
             "description": "Family dentistry, braces and cosmetic dentistry in Dubai Marina.",
             "mobile_viewport": True, "open_graph": False, "noindex": False,
             "schema_types": ["Dentist", "LocalBusiness"], "built_with": ["WordPress", "Elementor"],
             "tracking": ["Google Analytics"], "widgets": [], "socials": {"Instagram": "alnoordental"},
             "footer_year": 2021},
    "pagespeed": {"ok": True, "performance": 34, "seo": 82, "accessibility": 71, "best_practices": 58,
                  "first_paint": "3.9 s", "largest_paint": "9.2 s", "blocking_time": "1,240 ms",
                  "layout_shift": "0.31", "screenshot": ""},
    "ssl": {"ok": True, "valid": True, "expires": "2027-01-14", "days_left": 54, "issuer": "Let's Encrypt"},
    "email": {"ok": True, "provider": "Google Workspace", "hosts": ["aspmx.l.google.com"]},
    "archive": {"ok": True, "first": "2016-03-02", "captures": 148},
    "domain": {"ok": False, "error": "Couldn't look it up (Timeout)"},
    "checked_at": "2026-11-21 09:12:00",
}
audit.run_checks = lambda website, domain, key="": dict(CANNED_AUDIT)


def step(label, fn):
    try:
        out = fn()
        print(f"  ok   {label}")
        return out
    except Exception:
        print(f"  FAIL {label}")
        traceback.print_exc()
        return None


FIRST = ["Al Noor", "Emirates", "Gulf", "Marina", "Palm", "Oasis", "Pearl", "Royal", "Smile", "Bright",
         "Crescent", "Desert Rose", "Falcon", "Golden", "Harmony", "Jumeirah", "Khalifa", "Liwa", "Majestic",
         "Noble", "Premier", "Zenith", "Skyline", "Sapphire", "Tulip", "Unity", "Vista", "Wellness",
         "Dr. Fatima Al Mansoori", "Dr. Rashid & Partners", "Blue Lagoon", "City Centre", "Elite", "First"]
KIND = {
    "Dentist": ["Dental Clinic", "Dental & Orthodontic Centre", "Smile Studio", "Family Dentistry",
                "Dental & Implant Specialists"],
    "Beauty salon": ["Ladies Salon", "Beauty Lounge", "Hair & Nail Studio", "Spa & Salon"],
    "Physiotherapist": ["Physiotherapy Centre", "Rehab & Sports Medicine", "Physio Clinic"],
    "Veterinarian": ["Veterinary Clinic", "Pet Hospital", "Animal Care Centre"],
    "Real estate agency": ["Properties", "Real Estate Brokers", "Realty"],
    "Gym": ["Fitness Club", "Strength & Conditioning", "CrossFit"],
}
CITY = {"AE": ["Dubai", "Abu Dhabi", "Sharjah", "Ajman"], "SA": ["Riyadh", "Jeddah"], "QA": ["Doha"]}
AREA = ["Marina", "JLT", "Business Bay", "Deira", "Al Barsha", "Jumeirah 1", "Al Olaya", "West Bay",
        "Khalidiyah", "Al Nahda", "Downtown", "Mirdif"]


def make_rows(n, country, seed, category=None, with_email=0.5, with_site=0.7, landline=0.15):
    rnd = random.Random(seed)
    rows, seen = [], set()
    while len(rows) < n:
        cat = category or rnd.choice(list(KIND))
        name = f"{rnd.choice(FIRST)} {rnd.choice(KIND[cat])}"
        if rnd.random() < 0.12:
            name += f" - {rnd.choice(AREA)} Branch"
        if rnd.random() < 0.05:
            name += " (Formerly known as Modern Medical Centre LLC)"
        if name in seen:
            name += f" {len(rows)}"
        seen.add(name)
        city = rnd.choice(CITY[country])
        slug = "".join(c for c in name.lower() if c.isalnum())[:22]
        tld = {"AE": "ae", "SA": "sa", "QA": "qa"}[country]
        if country == "AE":
            phone = (f"04 {rnd.randint(200, 499)} {rnd.randint(1000, 9999)}" if rnd.random() < landline
                     else f"05{rnd.choice('0245568')} {rnd.randint(100, 999)} {rnd.randint(1000, 9999)}")
        elif country == "SA":
            phone = f"05{rnd.choice('03456')} {rnd.randint(100, 999)} {rnd.randint(1000, 9999)}"
        else:
            phone = f"{rnd.choice('3567')}{rnd.randint(100, 999)} {rnd.randint(1000, 9999)}"
        row = {"company": name, "phone": phone, "city": city, "category": cat,
               "address": f"Shop {rnd.randint(1, 40)}, {rnd.choice(AREA)}, {city}",
               "rating": round(rnd.uniform(3.4, 5.0), 1), "review_count": rnd.randint(3, 900),
               "mx_valid": 1, "country": country}
        if rnd.random() < with_site:
            row["website"] = f"https://{slug}.{tld}"
        if rnd.random() < with_email:
            row["email"] = f"{rnd.choice(['info', 'hello', 'reception', 'bookings'])}@{slug}.{tld}"
        rows.append(row)
    return rows


def seed():
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(DB + ext):
            os.remove(DB + ext)
    import db
    db.init_db()
    rnd = random.Random(7)
    now = dt.datetime.utcnow()
    today = dt.date.today()

    def ago(days=0, hours=0):
        return (now - dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    me = db.create_user("sheham", "audit-password-123", is_admin=True)
    him = db.create_user("cofounder", "audit-password-123", is_admin=False)
    print("users", me, him)

    def sql(q, args=()):
        with db.get_db() as conn:
            return conn.execute(q, args).fetchall()

    # ── Email infrastructure ────────────────────────────────────────────────
    for i, (nm, em) in enumerate([("Sheham - main", "sheham@hexiv-mail.example"),
                                  ("Outreach 2", "hello@hexiv-reach.example"),
                                  ("Warm-up inbox", "team@hexiv-labs.example")]):
        step(f"smtp account {nm}", lambda nm=nm, em=em: db.create_smtp_account({
            "name": nm, "email": em, "from_name": "Sheham from Hexiv", "smtp_host": "smtp.invalid",
            "smtp_port": 587, "smtp_user": em, "smtp_pass": "x", "imap_host": "imap.invalid",
            "imap_user": em, "imap_pass": "x"}))

    # ── Scrapes and businesses (mine) ───────────────────────────────────────
    jobs = {}
    for niche, city, dest, n, country in [("dentists", "Dubai", "email", 120, "AE"),
                                          ("beauty salons", "Riyadh", "whatsapp", 60, "SA"),
                                          ("physiotherapists", "Abu Dhabi", "calling", 70, "AE"),
                                          ("vets", "Doha", "email", 40, "QA")]:
        jid = step(f"scrape job {niche}", lambda: db.create_scrape_job(
            niche, city, max_results=n, owner_id=me, destination="email", country=country))
        jobs[(niche, city)] = jid
        if jid:
            step("finish job", lambda: db.update_scrape_job(jid, status="done", progress=n, total=n))

    all_rows = {}
    cat_for = {"dentists": "Dentist", "beauty salons": "Beauty salon",
               "physiotherapists": "Physiotherapist", "vets": "Veterinarian"}
    for (niche, city), jid in jobs.items():
        country = {"Dubai": "AE", "Riyadh": "SA", "Abu Dhabi": "AE", "Doha": "QA"}[city]
        n = {"dentists": 120, "beauty salons": 60, "physiotherapists": 70, "vets": 40}[niche]
        rows = make_rows(n, country, seed=hash(niche) % 1000, category=cat_for[niche],
                         with_email=0.0 if niche == "beauty salons" else 0.6)
        for r in rows:
            r["city"] = city
            r["source_job_id"] = jid
        all_rows[niche] = rows

    def ids_by_names(names):
        marks = ",".join("?" * len(names))
        got = sql(f"SELECT id, name FROM businesses WHERE owner_id=? AND name IN ({marks})", (me, *names))
        by = {r["name"]: r["id"] for r in got}
        return [by[n] for n in names if n in by]

    step("import dentists (email)", lambda: db.upsert_businesses(all_rows["dentists"], owner_id=me))
    step("import physios", lambda: db.upsert_businesses(all_rows["physiotherapists"], owner_id=me))
    step("import vets", lambda: db.upsert_businesses(all_rows["vets"], owner_id=me))
    step("import by hand", lambda: db.upsert_businesses(make_rows(25, "AE", 99), owner_id=me))

    # ── Email campaigns ─────────────────────────────────────────────────────
    def email_campaign(name, status):
        cid = db.create_campaign(name, daily_limit=40, owner_id=me)
        db.upsert_step(cid, 1, "Quick question about {{business_name}}'s website",
                       "<p>Hi {{first_name|there}},</p><p>I had a look at {{website}} on my phone "
                       "and noticed a couple of things that are probably costing you bookings.</p>"
                       "<p>Worth a 10 minute call this week?</p><p>Sheham</p>", 0)
        db.upsert_step(cid, 2, "Re: Quick question about {{business_name}}'s website",
                       "<p>Just floating this back up, {{first_name|hi}}.</p>", 3)
        db.upsert_step(cid, 3, "Should I close the file?",
                       "<p>No worries if the timing is off. I'll leave it here.</p>", 5)
        with db.get_db() as conn:
            conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, cid))
        return cid

    c1 = step("email campaign 1", lambda: email_campaign("Dubai dentists - website refresh", "active"))
    c2 = step("email campaign 2", lambda: email_campaign("Doha vets - online booking", "paused"))
    c3 = step("email campaign 3", lambda: email_campaign("Abu Dhabi physios - Google reviews (draft, not started yet)", "draft"))

    dent_ids = ids_by_names([r["company"] for r in all_rows["dentists"]])
    vet_ids = ids_by_names([r["company"] for r in all_rows["vets"]])
    physio_ids = ids_by_names([r["company"] for r in all_rows["physiotherapists"]])
    if c1:
        step("enrol dentists", lambda: db.enroll_businesses(c1, dent_ids[:90], owner_id=me))
    if c2:
        step("enrol vets", lambda: db.enroll_businesses(c2, vet_ids, owner_id=me))

    def email_history():
        enr = sql("SELECT e.id, e.campaign_id, e.email_lead_id, l.email FROM enrollments e "
                  "JOIN email_leads l ON l.id=e.email_lead_id WHERE e.campaign_id=?", (c1,))
        for i, e in enumerate(enr):
            if i % 5 == 4:
                continue
            db.log_send(e["campaign_id"], e["email_lead_id"], 1,
                        "Quick question about your website", f"<m{i}@x>", account_id=1 + i % 2)
            if i % 3 == 0:
                db.log_send(e["campaign_id"], e["email_lead_id"], 2, "Re: Quick question", f"<n{i}@x>", account_id=1)
        for i, e in enumerate(enr[:7]):
            db.mark_enrollment_replied(e["campaign_id"], e["email_lead_id"],
                                       subject=["Re: Quick question about your website",
                                                "Re: Quick question - who is this?",
                                                "RE: Quick question about Al Noor Dental & Orthodontic Centre's website - please call the manager"][i % 3],
                                       from_email=e["email"])
        for e in enr[8:11]:
            db.unsubscribe_contact(e["email"])
        for e in enr[12:15]:
            db.mark_bounced(e["email"])
    if c1:
        step("email sends, replies, unsub, bounce", email_history)

    # ── Calling ─────────────────────────────────────────────────────────────
    cc1 = step("call campaign", lambda: db.create_call_campaign("Physios - no booking system", owner_id=me))
    cc2 = step("call campaign 2", lambda: db.create_call_campaign("Dentists who never opened", owner_id=me))
    step("add physios to calling", lambda: db.add_to_calling(physio_ids, owner_id=me, call_campaign_id=cc1))
    step("add dentists to calling", lambda: db.add_to_calling(dent_ids[90:118], owner_id=me, call_campaign_id=cc2))

    def call_history():
        leads = sql("SELECT cl.id FROM call_leads cl JOIN businesses b ON b.id=cl.business_id "
                    "WHERE b.owner_id=? ORDER BY cl.id", (me,))
        outcomes = [o for o in db.get_call_outcomes(owner_id=me)] if hasattr(db, "get_call_outcomes") else []
        keys = [o["key"] if isinstance(o, dict) else o for o in outcomes] or \
               ["no_answer", "callback", "not_interested", "booked", "voicemail"]
        print("    outcome keys:", keys)
        soon = lambda d, h=10: (today + dt.timedelta(days=d)).strftime(f"%Y-%m-%dT{h:02d}:30")
        plan = [("no_answer", "Rang out twice.", None), ("callback", "Receptionist said the owner is in after 3pm. Ask for Dr. Khan.", soon(0, 15)),
                ("callback", "Wants pricing first - send the one-pager then call.", soon(-1)), ("voicemail", "Left a 20s voicemail.", None),
                ("not_interested", "Already has an agency on retainer.", None), ("callback", "Call Thursday morning.", soon(2)),
                ("booked", "Intro call booked with the practice manager.", soon(3, 11)), ("no_answer", "", None),
                ("callback", "Busy with patients, try again tomorrow.", soon(1)), ("wrong_number", "Number belongs to a pharmacy.", None)]
        for i, l in enumerate(leads[:46]):
            oc, note, when = plan[i % len(plan)]
            if oc not in keys:
                oc = keys[i % len(keys)]
            try:
                db.log_call(l["id"], oc, notes=note, next_call_at=when)
                if i % 4 == 0:
                    db.log_call(l["id"], "no_answer" if "no_answer" in keys else keys[0], notes="Second try, no pick-up.")
            except Exception as exc:
                print("    call skip:", oc, exc)
    step("call history", call_history)

    # ── WhatsApp ────────────────────────────────────────────────────────────
    wc1 = step("wa campaign 1", lambda: db.create_wa_campaign("Dubai dentists - booking system", owner_id=me, country="AE"))
    wc2 = step("wa campaign 2", lambda: db.create_wa_campaign("Riyadh salons - Instagram to bookings", owner_id=me, country="SA"))
    wc3 = step("wa campaign 3", lambda: db.create_wa_campaign("Doha gyms (new)", owner_id=me, country="QA"))

    wa_dent = make_rows(140, "AE", 321, category="Dentist", with_email=0.2)
    for i, r in enumerate(wa_dent):
        r["city"] = "Dubai"
        if i % 6 == 0:
            r["message"] = (f"Hi, is this {r['company']}? I was looking for a dentist near {r['address'].split(',')[1].strip()} "
                            f"and noticed you can't book online - patients have to call. I build booking pages for clinics "
                            f"in Dubai. Want me to send a 1 minute video of how it'd look for you?")
            r["followup_1"] = "Hi again, did you get a chance to see my message about online booking?"
            r["followup_2"] = f"Last one from me. Happy to mock it up for {r['company']} for free if useful."
    wa_salon = all_rows["beauty salons"]
    step("wa import dentists", lambda: db.upsert_wa_leads(wa_dent, default_country="AE", owner_id=me, wa_campaign_id=wc1))
    step("wa import salons", lambda: db.upsert_wa_leads(wa_salon, default_country="SA", owner_id=me, wa_campaign_id=wc2))
    step("wa import gyms", lambda: db.upsert_wa_leads(make_rows(12, "QA", 55, category="Gym", with_email=0),
                                                      default_country="QA", owner_id=me, wa_campaign_id=wc3))

    def wa_history():
        leads = sql("SELECT w.id, w.business_id, b.name FROM wa_leads w JOIN businesses b ON b.id=w.business_id "
                    "WHERE b.owner_id=? AND w.wa_campaign_id=? ORDER BY w.id", (me, wc1))
        msg = "Hi, is this the clinic? Quick question about your online booking."
        with db.get_db() as conn:
            pass
        sent = leads[:78]
        for i, l in enumerate(sent):
            db.mark_wa_sent(l["id"], msg, kind="opener", template_variant="AB"[i % 2])
            days = [0, 0, 1, 2, 4, 5, 7, 9, 12][i % 9]
            with db.get_db() as conn:
                conn.execute("UPDATE wa_leads SET sent_date=? WHERE id=?", (ago(days, i % 7), l["id"]))
                conn.execute("UPDATE wa_log SET sent_at=? WHERE wa_lead_id=?", (ago(days, i % 7), l["id"]))
        # a few second follow-ups
        for l in sent[60:66]:
            db.mark_wa_sent(l["id"], "Hi again, did you get a chance to see my message?", kind="followup")
            with db.get_db() as conn:
                conn.execute("UPDATE wa_leads SET sent_date=? WHERE id=?", (ago(6), l["id"]))
        # sent today (so "N sent today" has a number)
        for l in leads[78:87]:
            db.mark_wa_sent(l["id"], msg, kind="opener", template_variant="A")
        # opened, not answered
        for l in leads[87:89]:
            db.mark_wa_opened(l["id"], True)
        db.set_wa_no_whatsapp([l["id"] for l in leads[89:95]], True, owner_id=me)
        for l in leads[95:97]:
            db.set_wa_paused(l["id"], True)
        for l in sent[20:26]:
            db.mark_wa_replied(l["id"], True)
        d = lambda n, h, m=0: (today + dt.timedelta(days=n)).strftime(f"%Y-%m-%d {h:02d}:{m:02d}:00")
        booked = [(sent[30], d(-1, 16)), (sent[31], d(0, 15, 30)), (sent[32], d(1, 11)),
                  (sent[33], d(4, 10)), (sent[34], d(9, 14))]
        for l, when in booked:
            db.set_business_pipeline(l["business_id"], "booked", channel="whatsapp",
                                     next_action_at=when, owner_id=me)
        db.save_meeting(booked[1][0]["business_id"], starts_at=booked[1][1], timezone="Asia/Dubai",
                        title="Hexiv x clinic: intro call", link_kind="zmeeting",
                        join_url="https://meeting.zoho.example/join?key=123", calendar_name="Hexiv meetings",
                        calendar_uid="cal1", event_uid="ev1", synced_at=ago(0, 3), invited_at=ago(0, 3),
                        invite_email="manager@clinic.example")
        db.save_meeting(booked[2][0]["business_id"], starts_at=booked[2][1], timezone="Asia/Dubai",
                        title="Hexiv x clinic: intro call", link_kind="own",
                        own_link="https://meet.google.com/abc-defg-hij",
                        sync_error="Zoho said: INVALID_OAUTHTOKEN - the access token has expired")
        db.save_meeting(booked[3][0]["business_id"], starts_at=booked[3][1], timezone="Asia/Dubai",
                        title="Hexiv x clinic: intro call", link_kind="none")
        db.set_business_pipeline(sent[35]["business_id"], "proposal_sent", channel="whatsapp",
                                 next_action_at=d(2, 0)[:10], owner_id=me)
        db.set_business_pipeline(sent[36]["business_id"], "proposal_due", channel="whatsapp",
                                 next_action_at=d(-2, 0)[:10], owner_id=me)
        db.set_business_pipeline(sent[37]["business_id"], "won", channel="whatsapp", owner_id=me)
        db.set_business_pipeline(sent[38]["business_id"], "not_interested", channel="whatsapp", owner_id=me)
        key = db.create_pipeline_stage("Sent demo video", wants_date=True, owner_id=me)
        db.set_business_pipeline(sent[39]["business_id"], key, channel="whatsapp",
                                 next_action_at=d(1, 0)[:10], owner_id=me)
        with db.get_db() as conn:
            conn.execute("UPDATE businesses SET notes=? WHERE id=?",
                         ("Spoke to Maryam at reception on WhatsApp. Owner (Dr. Khan) is travelling until the 28th. "
                          "They use a paper diary and Instagram DMs for bookings. Send the demo video and follow up after the 28th.",
                          sent[31]["business_id"]))
        db.save_business_audit(sent[31]["business_id"], json.dumps(CANNED_AUDIT))
        db.save_business_audit(leads[100]["business_id"], json.dumps(CANNED_AUDIT))
        # second campaign: some sent, some due
        salons = sql("SELECT w.id FROM wa_leads w JOIN businesses b ON b.id=w.business_id "
                     "WHERE b.owner_id=? AND w.wa_campaign_id=? ORDER BY w.id", (me, wc2))
        for i, l in enumerate(salons[:25]):
            db.mark_wa_sent(l["id"], "Salam, quick question about your Instagram bookings.", kind="opener")
            with db.get_db() as conn:
                conn.execute("UPDATE wa_leads SET sent_date=? WHERE id=?", (ago(3 + i % 6), l["id"]))
    step("wa history", wa_history)

    # ── Named imports, a CSV-only scrape, trash, logs ───────────────────────
    def batches():
        before = sql("SELECT COALESCE(MAX(id),0) m FROM businesses")[0]["m"]
        _, ids = db.upsert_businesses(make_rows(30, "AE", 777, category="Real estate agency"), owner_id=me)
        db.record_import(ids, before, owner_id=me, source="brokers-dubai-chatgpt.json", channel="email")
        before = sql("SELECT COALESCE(MAX(id),0) m FROM businesses")[0]["m"]
        _, ids2 = db.upsert_businesses(make_rows(18, "AE", 778, category="Gym"), owner_id=me)
        db.record_import(ids2, before, owner_id=me, source="Pasted", channel="calling")
        return ids, ids2
    got = step("named imports", batches)
    if got:
        step("delete 9 leads (Recently deleted)", lambda: db.delete_businesses(got[1][:9], owner_id=me))
        step("delete 3 more", lambda: db.delete_businesses(got[0][:3], owner_id=me))

    step("csv-only scrape", lambda: db.create_scrape_job("orthodontists", "Sharjah", max_results=50,
                                                        owner_id=me, destination="csv"))
    for i, (m, lvl) in enumerate([("Scheduler cycle: 12 sent, 0 failed", "INFO"),
                                  ("IMAP check hello@hexiv-reach.example: 2 new replies", "INFO"),
                                  ("SMTP auth failed for team@hexiv-labs.example (535 5.7.8 Username and Password not accepted)", "ERROR"),
                                  ("Daily cap reached for 'Dubai dentists - website refresh' (40/40)", "WARN"),
                                  ("Bounce detected: reception@goldendentalclinic.ae (550 mailbox unavailable)", "WARN")] * 6):
        db.add_log(m, lvl)

    # ── The cofounder's own, smaller world ──────────────────────────────────
    def his():
        hc = db.create_wa_campaign("Abu Dhabi clinics - Google Ads", owner_id=him, country="AE")
        rows = make_rows(45, "AE", 4242, category="Dentist", with_email=0.1)
        db.upsert_wa_leads(rows, default_country="AE", owner_id=him, wa_campaign_id=hc)
        leads = sql("SELECT w.id, w.business_id FROM wa_leads w JOIN businesses b ON b.id=w.business_id "
                    "WHERE b.owner_id=? ORDER BY w.id", (him,))
        for i, l in enumerate(leads[:20]):
            db.mark_wa_sent(l["id"], "Hi, quick question about your Google Ads.", kind="opener")
            with db.get_db() as conn:
                conn.execute("UPDATE wa_leads SET sent_date=? WHERE id=?", (ago(i % 8), l["id"]))
        db.set_business_pipeline(leads[3]["business_id"], "booked", channel="whatsapp",
                                 next_action_at=(today + dt.timedelta(days=1)).strftime("%Y-%m-%d 12:00:00"),
                                 owner_id=him)
    step("cofounder's leads", his)

    for t in ("businesses", "email_leads", "call_leads", "wa_leads", "wa_log", "call_log", "enrollments",
              "sends", "lead_batches", "deleted_leads", "meetings", "scrape_jobs"):
        print(f"  {t:16} {sql(f'SELECT COUNT(*) c FROM {t}')[0]['c']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        seed()
    else:
        import app as app_mod
        app_mod.app.run(host="127.0.0.1", port=5055, debug=False, use_reloader=False, threaded=True)
