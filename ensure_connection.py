"""Pre-flight Instagram connection check -- run automatically by Start_Automation.bat right
before the dashboard starts.

Priority order, exactly matching how a returning user expects this to feel:
  1. Already have a saved session (instagram_session.json) or a saved INSTAGRAM_SESSIONID in
     .env? Try it silently. If it works, done -- no browser, no prompts, nothing to do.
  2. Only if that's missing or doesn't work anymore does it ask anything, and even then it
     gives a real choice instead of forcing one path:
       1) Paste a session cookie value you already have (from any browser, any account) --
          zero extra installs, fastest if you already know how to grab one.
       2) Let the automation open a browser and log in for you -- needs a one-time extra
          install (Playwright), only downloaded if you actually pick this option.
       3) Skip for now -- the dashboard still opens; the automation just stays paused until
          you reconnect (safe, nothing breaks).
"""
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "instagram_session.json"


def has_saved_connection():
    return SESSION_FILE.exists() or bool(os.getenv("INSTAGRAM_SESSIONID"))


def try_login():
    from instagram_action_client import InstagramActionClient, InstagramSessionError
    print("Checking your Instagram connection...")
    client = InstagramActionClient()
    try:
        client.login()
        print("[OK] Instagram connection verified.")
        return True
    except InstagramSessionError as exc:
        print(f"[!] Instagram connection isn't working right now: {exc}")
        return False
    except Exception as exc:
        print(f"[!] Unexpected error while checking the connection: {exc}")
        return False


def paste_sessionid_flow():
    from env_utils import write_env_var
    from config import settings
    print()
    print("Paste your Instagram session cookie value below, then press Enter.")
    print("(In Chrome: log into Instagram, press F12, go to Application > Cookies >")
    print(" instagram.com, and copy the value next to \"sessionid\".)")
    try:
        value = input("sessionid: ").strip()
    except (EOFError, KeyboardInterrupt):
        value = ""
    if not value:
        print("Nothing entered -- skipping.")
        return False
    write_env_var("INSTAGRAM_SESSIONID", value)
    # `settings` was already loaded once at process start and won't re-read .env on its own --
    # patch this same shared instance in place so the check right below (and everything else in
    # this run) actually sees the value just pasted, instead of silently falling through to the
    # password-login endpoint we're specifically trying to avoid.
    object.__setattr__(settings, "instagram_sessionid", value)
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()  # force a fresh check against the new cookie, not a stale session
    print("Saved. Checking it now...")
    return try_login()


def browser_fetch_flow():
    try:
        import playwright  # noqa: F401
    except ImportError:
        print()
        print("This option needs a one-time extra install (a few minutes, only ever happens once):")
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "playwright"], check=False)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    subprocess.run([sys.executable, str(BASE_DIR / "fetch_login_cookie.py")], check=False)
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
    return try_login()


def main():
    if has_saved_connection():
        if try_login():
            return 0
        print("Your saved connection stopped working -- let's reconnect.")
    else:
        print("No Instagram connection saved yet.")

    while True:
        print()
        print("How would you like to connect your Instagram account?")
        print("  1) Paste a session cookie I already have")
        print("  2) Open a browser and log in automatically (one-time extra install)")
        print("  3) Skip for now -- the dashboard will still open, paused until reconnected")
        try:
            choice = input("Choose 1, 2, or 3: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "3"

        if choice == "1":
            if paste_sessionid_flow():
                return 0
        elif choice == "2":
            if browser_fetch_flow():
                return 0
        elif choice == "3":
            print("Skipping for now -- run this file again anytime to reconnect.")
            return 0
        else:
            print("Please type 1, 2, or 3.")


if __name__ == "__main__":
    sys.exit(main())
