"""Copy the hand-back screenshots from the last walk-throughs into the repo.

    python tools/audit/keep_shots.py <stage> [dest-dir]

Plan 3's hand-back asks, per package, for Dashboard, WhatsApp To do, a lead
panel, Calling To do, Settings and the sign-in page at computer and phone
width. Run shoot.py for plan-desktop-1, plan-desktop-2 and plan-phone-1
first; this picks those screens out by step name and saves them as WebP
(a tenth of the PNG size) under <dest-dir>/<stage>/. The full PNG sets stay
in <system temp>/shoutreach-audit/shots/.
"""
import glob
import os
import sys
import tempfile

from PIL import Image

SHOTS = os.path.join(tempfile.gettempdir(), "shoutreach-audit", "shots")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DEFAULT_DEST = os.path.join(REPO, "docs", "plans", "plan-3-shots")

# (shot prefix, step name) -> saved name
PICKS = [
    ("d", "login", "desktop-1-sign-in"),
    ("d", "dashboard", "desktop-2-dashboard"),
    ("d", "wa-todo", "desktop-3-whatsapp-todo"),
    ("e", "wa-leads-panel", "desktop-4-lead-panel"),
    ("d", "calling-todo", "desktop-5-calling-todo"),
    ("d", "settings", "desktop-6-settings"),
    ("p", "login", "phone-1-sign-in"),
    ("p", "dashboard", "phone-2-dashboard"),
    ("p", "wa-todo", "phone-3-whatsapp-todo"),
    ("p", "wa-todo-lead", "phone-4-lead-panel"),
    ("p", "calling-todo", "phone-5-calling-todo"),
    ("p", "settings", "phone-6-settings"),
]


def main():
    stage = sys.argv[1]
    dest = os.path.join(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DEST, stage)
    os.makedirs(dest, exist_ok=True)
    for prefix, step, name in PICKS:
        found = glob.glob(os.path.join(SHOTS, f"{prefix}-[0-9][0-9]-{step}.png"))
        if not found:
            print("missing:", prefix, step)
            continue
        # Several runs of different plan versions can leave the same step
        # under different numbers: take the newest.
        img = Image.open(max(found, key=os.path.getmtime)).convert("RGB")
        img.save(os.path.join(dest, name + ".webp"), "WEBP", quality=82, method=6)
        print("kept", name)


if __name__ == "__main__":
    main()
