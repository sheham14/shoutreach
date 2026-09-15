"""
Reset a ShoutReach password from the server's shell.

For when nobody who can reset it from inside the app can log in -- in practice,
the only admin forgetting their own password. Run it on the VM:

    cd ~/shoutreach && source venv/bin/activate
    python reset_password.py            # pick a user, type a new password
    python reset_password.py --list     # just show who exists

Deliberately a script and not a "forgot password" page: that would need an
email on every account, expiring reset links, rate limiting, and a public page
anyone on the internet can hit without logging in. Shell access to the server
is already a stronger proof of identity than any of that.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

# Must be settled before `import db`, which reads DB_PATH at import time.
# Defaulting to the file beside this script rather than the working directory:
# sqlite3 creates a database that doesn't exist, so running this from the wrong
# folder would otherwise "succeed" against a brand-new empty one.
os.environ.setdefault("DB_PATH", str(Path(__file__).resolve().parent / "outreach.db"))

MIN_LENGTH = 12


def _require_database():
    path = Path(os.environ["DB_PATH"])
    if not path.is_file():
        sys.exit(f"No database at {path}. Run this from the ShoutReach folder, "
                 f"or set DB_PATH to the real one.")


def reset(username: str, password: str) -> None:
    """Set a user's password. Raises ValueError rather than half-succeeding."""
    import db
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Password must be at least {MIN_LENGTH} characters")
    user = db.get_user_by_username(username)
    if not user:
        raise ValueError(f"No user called {username!r}")
    db.change_password(user["id"], password)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--list", action="store_true", help="list users and exit")
    args = parser.parse_args()

    _require_database()
    import db

    users = db.list_users()
    if not users:
        sys.exit("This database has no users. Log in through the app to create the first admin.")

    print(f"Users in {os.environ['DB_PATH']}:")
    for u in users:
        print(f"  {u['username']}{'  (admin)' if u['is_admin'] else ''}")
    if args.list:
        return

    username = input("\nReset the password for: ").strip()
    if not db.get_user_by_username(username):
        sys.exit(f"No user called {username!r}. Nothing changed.")

    password = getpass.getpass(f"New password (min {MIN_LENGTH} characters): ")
    if getpass.getpass("Type it again: ") != password:
        sys.exit("The passwords didn't match. Nothing changed.")

    try:
        reset(username, password)
    except ValueError as exc:
        sys.exit(f"{exc}. Nothing changed.")
    print(f"Password changed for {username}. Existing sessions stay logged in.")


if __name__ == "__main__":
    main()
