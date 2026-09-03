# Setting up on a new machine (e.g. continuing at the office)

This repo is **code only**. Everything credential- or data-related is deliberately excluded from
git (see `.gitignore`) — cloning gets you the automation logic, not a working setup. You need to
bring the excluded files over yourself, from your current machine, through a channel you trust
(a private USB copy, a personal cloud folder, etc. — never commit these, never send through a
public channel).

## 1. Clone the code

```bash
git clone https://github.com/AhmedAli1199/instagram-automation.git
cd instagram-automation
```

## 2. Bring these files over from your current machine (not in git, not optional)

| File | Why it's excluded from git | What happens if you skip it |
|---|---|---|
| `.env` | Real Instagram credentials/session cookie in plain text | Automation won't know which account to use — you'd have to reconfigure from `.env.example` and log in fresh |
| `instagram_session.json` | A live, valid authentication token — equivalent to a password | A fresh login will be required (via `ensure_connection.py`), which is fine but means re-verifying the account |
| Your real workbook(s) (`*.xlsx`) | Lead data, not source code | No leads to process until you copy them over |
| `state.txt` / `state_sales_*.txt` / `global_state.txt` | Progress pointers, daily-cap timers -- specific to where the automation left off | Losing these means the automation starts from scratch: re-checks already-processed leads, and daily follow/message counts are forgotten (harmless, just redundant work, not data loss) |

If you skip the state files specifically, that's the least risky thing to skip — it just costs the automation an hour or two re-establishing where it was, not correctness or safety. `.env` and `instagram_session.json` are the two that actually matter for anything to work at all.

## 3. Install dependencies

```bash
py -m pip install --disable-pip-version-check -r requirements.txt
```

Or just double-click `Start_Automation.bat` — it checks and installs on first run automatically.

## 4. Start it

```bash
py dashboard_server.py
```

Or again, just `Start_Automation.bat`. Opens the dashboard at `http://localhost:8765`.

## Keeping two machines in sync going forward

- **Code changes**: commit and push from whichever machine you're working on, `git pull` on the
  other before you start there.
- **`.env`/session/workbooks/state**: git never touches these — if you make real progress on one
  machine (leads processed, daily counts, etc.), that state only exists on that machine unless you
  copy it over yourself. Running the automation from *both* machines on the *same* Instagram
  account *at the same time* is not supported — the app-level lock (`app.lock`) only prevents two
  processes on the *same* machine from colliding, not two machines. Only run it from one machine
  at a time.
