# Audit tools

Scripts from the 2026-11-21 audit, kept so a build can be checked the same way
it was audited. They never touch `./outreach.db` or the live site: everything
they write goes to `<system temp>/shoutreach-audit/`.

Needs the worker's requirements (`pip install -r requirements-worker.txt`,
`playwright install chromium`) plus `requests` and `Pillow`.

## A local copy with fake data

```
python tools/audit/serve.py seed     # build the scratch database (452 businesses, 3 users)
python tools/audit/serve.py          # serve it on http://127.0.0.1:5055, sending switched off
```

Users: `sheham` (admin), `cofounder` (non-admin). Password for both:
`audit-password-123`. The email sender and the lead audit's outside calls are
replaced, so nothing leaves the machine.

## Screenshots and measurements (server must be running)

```
python tools/audit/shoot.py tools/audit/plan-desktop-1.json
python tools/audit/shoot.py tools/audit/plan-phone-1.json
python tools/audit/measure.py phone      # tap targets, first-row position, type sizes
python tools/audit/measure.py desktop    # requests per screen, polling, keyboard, page weight
```

A plan file is a list of steps (`js`, `click`, `tap`, `select`, `wait`), each
ending in a screenshot. Copy one to make a new walk-through. Any JavaScript
error or failed API call during a step is printed beside it.

## Checks for specific findings

Each prints what the app does today. Run them before and after a fix; the
output should change from the fault to the fixed behaviour. The first five
build their own scratch database; the last two need the server above.

| Script | Finding |
|---|---|
| `checks/suppression_db.py` | B-1, at the database layer |
| `checks/suppression_routes.py` | B-1, through the import, enrol and unsubscribe routes |
| `checks/security_findings.py` | B-3, B-1 (restore, edit), B-6 (stored value), B-8 (backslash name), B-4 |
| `checks/scheduler_shared_loop.py` | B-6: one bad campaign blocks the next |
| `checks/scheduler_thread_death.py` | B-23: the sender thread dies on a held lock |
| `checks/nonadmin_access.py` | B-2, B-10, F-25, as the non-admin user |
| `checks/calling_reopen.py` | B-22 |

These are demonstrations, not tests: they don't exit non-zero. Each fix should
also add a real check to `tests/`.
