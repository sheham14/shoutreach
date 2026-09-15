"""reset_password.py -- the out-of-app way back in for a locked-out admin.

Run:  python tests/test_reset_password.py     (exits 0 on pass, 1 on failure)

The interactive prompts aren't under test; the parts that can go wrong are. A
reset that silently half-works leaves someone locked out believing they aren't,
and one pointed at the wrong folder "succeeds" against a brand-new empty
database while the real one stays untouched.
"""
import importlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(label)


def main():
    work = tempfile.mkdtemp(prefix="shoutreach_reset_")
    try:
        os.environ["DB_PATH"] = os.path.join(work, "reset.db")
        import db
        importlib.reload(db)
        db.init_db()
        db.create_user("boss", "original-password-1", is_admin=True)

        import reset_password
        importlib.reload(reset_password)

        print("\n1. RESETTING A PASSWORD")
        reset_password.reset("boss", "brand-new-password-2")
        check("the new password logs in", bool(db.authenticate("boss", "brand-new-password-2")))
        check("the old one no longer does", not db.authenticate("boss", "original-password-1"))
        reset_password.reset("  BOSS ", "another-password-33")
        check("the username matches the way login matches it",
              bool(db.authenticate("boss", "another-password-33")))

        print("\n2. REFUSING RATHER THAN HALF-SUCCEEDING")
        for label, user, pw in (
            ("a password under 12 characters is refused", "boss", "short"),
            ("an unknown user is refused", "nobody", "long-enough-password"),
        ):
            try:
                reset_password.reset(user, pw)
                check(label, False, "it was accepted")
            except ValueError:
                check(label, True)
        check("and neither touched the real password",
              bool(db.authenticate("boss", "another-password-33")))

        print("\n3. POINTED AT THE WRONG FOLDER")
        missing = os.path.join(work, "does-not-exist.db")
        os.environ["DB_PATH"] = missing
        try:
            reset_password._require_database()
            check("it refuses instead of creating an empty database", False, "it carried on")
        except SystemExit:
            check("it refuses instead of creating an empty database", True)
        check("and leaves no empty database behind", not os.path.exists(missing))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
