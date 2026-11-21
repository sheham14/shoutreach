"""Drive the harness copy of ShoutReach and screenshot it.

    python shoot.py <plan-file.json> [--only name-substring]

A plan is {"device": "desktop"|"phone", "user": "...", "prefix": "...",
"steps": [{"name": "...", "do": [["js", "..."], ["click", "sel"], ["wait", 300],
["select", "sel", "value"], ["fill", "sel", "text"]], "full": false}]}
"""
import json
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:5055"
OUT = os.path.join(os.path.join(tempfile.gettempdir(), "shoutreach-audit"), "shots")
os.makedirs(OUT, exist_ok=True)


def main():
    plan = json.load(open(sys.argv[1], encoding="utf-8"))
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    phone = plan.get("device") == "phone"
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        if phone:
            ctx = browser.new_context(
                viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True,
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                           "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        else:
            ctx = browser.new_context(viewport={"width": plan.get("width", 1440), "height": plan.get("height", 900)})
        ctx.add_init_script("""
            window.__opened = [];
            window.open = (...a) => { window.__opened.push(a); return { focus(){}, closed:false, location:{} }; };
            window.confirm = () => true;
            window.alert = (m) => { console.log('ALERT:' + m); };
            window.prompt = (m) => { console.log('PROMPT:' + m); return null; };
        """)
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text[:200]}"))
        page.on("pageerror", lambda e: problems.append(f"pageerror: {str(e)[:200]}"))
        page.on("response", lambda r: r.status >= 400 and "/api/" in r.url and
                problems.append(f"http {r.status}: {r.request.method} {r.url.replace(BASE, '')[:120]}"))

        page.goto(BASE + "/login")
        if plan.get("login_shot"):
            page.screenshot(path=os.path.join(OUT, f"{plan['prefix']}-00-login.png"))
        page.fill('input[name="username"]', plan["user"])
        page.fill('input[name="password"]', "audit-password-123")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        if phone:
            page.evaluate("() => { window.openWhatsAppChat_real = window.openWhatsAppChat; }")

        for i, st in enumerate(plan["steps"], 1):
            if only and only not in st["name"]:
                # still run the actions marked 'always' so state carries
                if not st.get("always"):
                    continue
            mark = len(problems)
            for act in st.get("do", []):
                try:
                    kind = act[0]
                    if kind == "js":
                        page.evaluate(f"async () => {{ {act[1]} }}")
                    elif kind == "click":
                        page.locator(act[1]).first.click(timeout=4000)
                    elif kind == "tap":
                        page.locator(act[1]).first.tap(timeout=4000)
                    elif kind == "select":
                        page.select_option(act[1], act[2])
                    elif kind == "fill":
                        page.fill(act[1], act[2])
                    elif kind == "wait":
                        page.wait_for_timeout(act[1])
                    elif kind == "scroll":
                        page.evaluate(f"() => window.scrollTo(0, {act[1]})")
                    elif kind == "size":
                        page.set_viewport_size({"width": act[1], "height": act[2]})
                    elif kind == "eval":
                        print(f"   eval[{st['name']}]:", json.dumps(page.evaluate(act[1]))[:1500])
                except Exception as exc:
                    problems.append(f"step '{st['name']}' action {act[:2]}: {str(exc).splitlines()[0][:160]}")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(st.get("settle", 350))
            if not st.get("noshot"):
                path = os.path.join(OUT, f"{plan['prefix']}-{i:02d}-{st['name']}.png")
                page.screenshot(path=path, full_page=bool(st.get("full")))
            new = problems[mark:]
            print(f"{i:02d} {st['name']}" + (f"   !! {len(new)} problem(s)" if new else ""))
            for n in new:
                print("     ", n)
        browser.close()
    print("\nTOTAL PROBLEMS:", len(problems))


if __name__ == "__main__":
    main()
