# Instagram Outreach Automation — Setup & Usage Guide

This guide gets the system running on your computer and shows you how to use it day to day.
No coding knowledge needed — one file to double-click does everything.

---

## Part 1 — Starting it up (every time, including the very first time)

Double-click **`Start_Automation.bat`** in this folder. That's it — that one file:

- Checks everything it needs is installed, and installs anything missing (only the first time —
  every time after that, it checks and skips straight past this in a couple of seconds).
- Connects your Instagram account, if it isn't already connected.
- Opens the control panel in your browser automatically.

**The very first time**, it will ask you for two things along the way — just follow what's on
screen:

1. **A Notepad window may open** asking you to fill in your Instagram username and password.
   Type them in, save, close Notepad, then double-click `Start_Automation.bat` again to continue.
2. **A browser window may open** to log into Instagram. If it just shows your feed, you don't
   need to do anything. If Instagram asks you to verify anything — a code, "was this you?", etc.
   — complete it yourself in that window, same as normal. The program waits for you.

After that first run, every future time is just: **double-click the file, wait a few seconds,
the dashboard opens.**

*(If your computer ever restarts, this is exactly what you do again — double-click the same
file. If the automation was running before, it picks back up automatically.)*

---

## Part 2 — Using the control panel

Keep the black window that opened alongside your browser running in the background — that's
what's actually doing the work. Closing it stops the automation. The browser tab is just the
window into it.

### The control panel

At the top you can pick from **three visual styles** — purely how it looks, they all do the same
thing. Your choice is remembered.

**Buttons — what each one does:**

| Button | What it does |
|---|---|
| **Resume** | Turns the automation on (needed before anything runs) |
| **Pause** | Turns it off safely — finishes what it's doing, then stops |
| **Start Scheduler** | Starts it running continuously in the background |
| **Stop Scheduler** | Stops the background process |
| **Run Once** | Does a single pass right now (useful for testing) |
| **Rewind / Skip** | Moves the "which lead to check next" pointer back or forward by one — you shouldn't normally need these |
| **Set Pointer…** | Manually jumps to a specific row — for advanced troubleshooting only |

**Normal day-to-day use**: click **Resume**, then **Start Scheduler**, and leave it running.
It checks itself roughly once a minute, but it only actually follows or messages someone every
few hours (on purpose — see "Why it's slow" below), and only within your set daily limits.

### What the panel shows you

- **Top bar**: whether it's currently running, and if anything's gone wrong (shown in red).
- **Today's numbers**: how many people were followed, messaged, replied, etc. today.
- **Pipeline**: how many leads are at each stage right now.
- **Tabs** (DM'd today, Followed today, Replied, Manual review, etc.): click any tab to see the
  actual list of people in that category.
- **Live log**: a running commentary of exactly what the program is doing and why, updated
  automatically.

### The "Manual review" tab — check this regularly
Occasionally a lead needs a human decision — something the program isn't confident enough to
handle on its own (a username that looks off, an unclear error, etc.). These show up in
**Manual review**, along with a plain explanation of why. Nothing else about that lead happens
automatically until you've looked at it.

### If someone replies
The moment a person replies to a message, the automation **stops touching that conversation
completely** — it will never send that person anything else. It's now yours to continue by hand,
same as any normal Instagram DM.

---

## Part 3 — Important things to know

### Editing the Excel file safely
Never edit the Excel file while the program is running. To edit it:
1. Click **Pause** in the control panel and wait for it to confirm.
2. Make your changes, save, and close Excel.
3. Click **Resume**.

New leads should be added at the **bottom** of the sheet.

### Adding a brand-new spreadsheet
Want to start over with a completely different/empty spreadsheet? Put just that one Excel file
in this folder (only one), delete the `state.txt` file if it exists, and double-click
`Start_Automation.bat` — it automatically finds your file and adds the columns it needs, without
touching anything already in it.

### What happens if your computer restarts or shuts down
Nothing breaks — all progress is saved continuously to the Excel file and to a small status
file, not to memory. If your PC shuts down, restarts, or sleeps, the automation simply stops
until you start it again — nothing is lost. Just double-click `Start_Automation.bat` and it
picks up exactly where it left off, including resuming automatically if it was running before.

*(If you want this running unattended all day even when your computer might be off or asleep,
ask about the cloud-hosting option separately — it's a different, always-on setup.)*

### Why it's slow on purpose
The program deliberately waits hours between follows and messages, and only acts during normal
daytime hours. This is intentional — it's what keeps your account looking human rather than
automated, and reduces the risk of Instagram restricting the account. Please don't lower these
delays without checking first.

### If something looks wrong
Check the **Live log** panel first — it explains what happened in plain terms. If the top bar
shows red / an error, it will usually tell you exactly what to do (for example, reconnecting
your Instagram account). If you're ever unsure, click **Pause** and reach out rather than
guessing.
