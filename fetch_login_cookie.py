"""One-time setup helper: logs into Instagram in a real, visible browser window and saves the
resulting session cookie into .env automatically -- so nobody has to open DevTools and copy a
cookie value by hand.

Why this exists: the automation's normal password-login endpoint is Instagram's private mobile
API, which some accounts get flagged/rejected on (see README's "Follow logic" notes). Logging in
through a real browser instead, the same way a person would, avoids that endpoint entirely and
is far less likely to trip Instagram's automation detection.

Run this ONCE per account (or again later only if the session ever fully expires):

    python fetch_login_cookie.py

A Chrome window will open. If Instagram shows anything other than your home feed --  a security
check, a code sent to your email/phone, a "was this you?" screen -- just complete it yourself in
that window like normal. The script waits for you; it does not need you to type anything back
into the terminal. Once you're logged in, it saves the session to .env and closes the window by
itself.

One-time install this needs (not required by anything else in the project):

    pip install playwright
    playwright install chromium
"""
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import os

from env_utils import write_env_var

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
PROFILE_DIR = BASE_DIR / "browser_profile"  # persisted so this looks like the same browser every time
LOGIN_URL = "https://www.instagram.com/accounts/login/"
MAX_WAIT_SECONDS = 300  # 5 minutes -- plenty of time to solve a checkpoint by hand


def get_sessionid(context):
    for cookie in context.cookies("https://www.instagram.com"):
        if cookie.get("name") == "sessionid" and cookie.get("value"):
            return cookie["value"]
    return None


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright isn't installed yet. Run this once, then try again:")
        print("  pip install playwright")
        print("  playwright install chromium")
        sys.exit(1)

    username = os.getenv("INSTAGRAM_USERNAME", "")
    password = os.getenv("INSTAGRAM_PASSWORD", "")
    if not username or not password:
        print("INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD are not set in .env -- fill those in first.")
        sys.exit(1)

    PROFILE_DIR.mkdir(exist_ok=True)
    print(f"Opening a browser to log in as @{username} ...")
    print("If Instagram asks you to verify anything, just do it in the window that opens.")
    print("This script will keep waiting -- you don't need to touch this terminal.\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Already logged in from a previous run of this script (persisted browser profile)?
        sessionid = get_sessionid(context)
        if not sessionid:
            try:
                page.wait_for_selector('input[name="username"]', timeout=15000)
                page.fill('input[name="username"]', username)
                page.fill('input[name="password"]', password)
                page.click('button[type="submit"]')
            except Exception:
                pass  # if the form isn't there, we may already be past login -- fall through to polling

            print("Waiting for login to complete (up to 5 minutes) ...")
            waited = 0
            while waited < MAX_WAIT_SECONDS:
                sessionid = get_sessionid(context)
                if sessionid:
                    break
                time.sleep(2)
                waited += 2

        if not sessionid:
            print("\nTimed out waiting for a successful login. The browser window is still open --")
            print("finish logging in there, then run this script again.")
            context.close()
            sys.exit(1)

        write_env_var("INSTAGRAM_SESSIONID", sessionid)
        print(f"\nLogged in. Session saved to {ENV_PATH.name} as INSTAGRAM_SESSIONID.")
        print("You can close the browser window now -- the automation will use this session on its next run.")
        context.close()


if __name__ == "__main__":
    main()
