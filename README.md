# Instagram Excel Automation V4.3 - Excel Only

V4.3 keeps the Excel-only architecture and adds reliability hardening.

Key changes:

- Existing Excel User ID remains the immutable lead identity.
- Added Automation Username tracking to detect username changes after an outward action.
- Added LAST_KNOWN_EXCEL_ROW high-water mark so newly appended rows are not lost when H/G sorting changes the priority list.
- PAUSED_SAFE is now set only by the scheduler between workers.
- Added application-level lock and account-level outward-action lock using filelock.
- Added PREVIOUS_CONVERSATION filtering before first DM.
- Follow and Unfollow now check return values and independently verify friendship state.
- DM send now requires a returned DirectMessage plus inbox verification; uncertain DMs are never blindly resent.
- Replies are evaluated after Message Sent At.
- First reliable Message Seen At is locked and is not moved forward.
- SEEN without a usable timestamp falls back to the general 7-day no-reply rule.
- Failed cleanup/unfollow operations are retained as UNFOLLOW_FAILED and retried later.
- Pending follow expiration is not finalized until cancellation is verified.
- Follow requests are spaced independently with NEXT_FOLLOW_NOT_BEFORE.
- Message preparation buffer is a real total buffer, default 9, rather than 9 new messages every cycle.
- Profile processing is limited and stops when current-day capacity/buffer is satisfied.
- Excel, HikerAPI, and Instagram session failures pause automation instead of crashing the scheduler.
- run-once refuses to operate unless MODE=RUN and no scheduler already owns the app lock.

## Safe Excel editing

1. `python main.py pause`
2. Wait for `MODE=PAUSED_SAFE` in `python main.py status`
3. Open, edit, save, and close Excel
4. Append new users at the end of the workbook
5. `python main.py resume`

## Column E

`Open to new connection? = NO` is a hard local filter. Blank or any value other than NO is eligible.

## Priority

H is primary, G is secondary. **1 is the highest priority (processed first), 3 is the lowest**
of the valid values, blank ranks below any explicit 1/2/3 (processed after them, but still
before invalid entries). Explicit 0 in either field filters locally. Blank G/H is allowed for
new rows.

## Follow logic (corrected rule)

Every lead that isn't already followed or already pending gets a follow request first, whether
their profile is public or private:

- **Public profile**: Instagram auto-accepts the follow instantly, so the lead goes straight to
  `READY_TO_MESSAGE` right after the follow succeeds.
- **Private profile**: the lead goes to `WAITING_FOLLOW_APPROVAL` and is rechecked periodically
  (`STATUS_CHECK_HOURS`) until accepted, expired, or the profile turns public.

We never send a DM to someone we haven't first followed. Note this does **not** guarantee Instagram
will deliver the DM -- delivery of a first message also depends on the *recipient's* own message-request
settings ("who can send me message requests"), which is outside the automation's control and not
detectable in advance. Leads whose message never gets seen are still caught and cleaned up by the
normal 7-day no-reply rule.

Previous-conversation history is checked immediately after the Instagram profile/relationship check,
before any follow decision -- so we never spend a follow request on someone we already have a DM
thread with.

## Messages

Two approved, editable message libraries, plain text, one message per line, `GEN ` or `PER ` prefix:

- `messages.txt` -- English, 10 generic + 10 personalized.
- `messages_he.txt` -- Hebrew, 5 generic + 5 personalized, used only for `Language Override = HEB`
  leads. These are sent as-is (real pre-approved text), never machine-translated.

The automation picks **randomly** among the templates that fit each lead (not always the first
line), so wording varies across leads. Edit either file freely -- add, remove, or reword lines,
keep the `GEN`/`PER` prefix and, for `PER` lines, keep the `_____` (first name) and
`{{city}}`/`{{location}}` placeholders.

### Real name vs. brand/business account name

Not every "Full Name" in the sheet or on Instagram is an actual person's name -- some are page
names like "Money Hustle" or "Daily Motivation". Before using a `PER` (personalized) template,
`name_classifier.py` runs a rule-based check (rejects names with emoji, digits, symbols, obvious
brand/business keywords, more than 3 words, etc.) and only personalizes when the name plausibly
belongs to a person. Otherwise it silently falls back to a random `GEN` message -- never
"Hey Money Hustle, how are you?". This is a deterministic heuristic, not a live AI/LLM call, so it's
free and instant; it can be swapped for an LLM-based classifier later if the heuristic misses too
many cases, at the cost of a per-lead API call and a new credential to manage.

## Language Override

`HEB` sends a message from the approved Hebrew library (`messages_he.txt`) as-is. Otherwise English
is the default and a confidently detected foreign language present in the lead's own bio may be
appended as an auto-translated addition underneath the English message (this translation-as-addition
behavior is separate from, and unaffected by, the HEB override library above).

## Safety

Instagrapi uses Instagram private interfaces. The project does not implement CAPTCHA bypass, anti-detection, or restriction evasion.

## Profile lookups

All profile data (privacy, verification, bio, city, full name) is fetched directly through the logged-in
Instagrapi session (`InstagramActionClient.get_profile`). HikerAPI is not used and there is no external
profile API dependency or key required.

## Logging

Every run writes to both the console and a rotating log file at `logs/automation.log` (5MB per file, 5 files
kept). Each worker logs what it checked, what it decided for each lead, and why (filtered/processed/followed/
messaged/skipped), plus every outward Instagram action (login, follow, unfollow, DM send) and every random
delay it scheduled. Tail the log file while `scheduler.py` is running to watch the automation work in real time.

## Multi-salesperson mode

Off by default (`MULTI_SALESPERSON=false` in `.env`) -- everything above this section describes single-
workbook mode exactly as it's always worked, completely unaffected by any of this. Turning it on runs
**three independent lead workbooks through one shared Instagram account**.

### Enabling it

In `.env`, set:

```
MULTI_SALESPERSON=true
SP1_NAME=Alice
SP1_WORKBOOK=sales_1.xlsx
SP1_STATE=state_sales_1.txt
SP2_NAME=Bob
SP2_WORKBOOK=sales_2.xlsx
SP2_STATE=state_sales_2.txt
SP3_NAME=Carla
SP3_WORKBOOK=sales_3.xlsx
SP3_STATE=state_sales_3.txt
```

Put each salesperson's own Excel or CSV file in the project folder under the name given as
`SPn_WORKBOOK` (a `.csv` is auto-converted to `.xlsx` the same way single-workbook mode does it).
Restart the dashboard/scheduler -- it provisions all three workbooks' automation columns automatically,
same as single-workbook mode does for one.

### How it actually behaves

- **Every source is processed every cycle** -- profile checks, follow/DM eligibility, reply/seen
  monitoring, cleanup, all of it, for SP1/SP2/SP3, every single cycle. Nobody's data waits for a "turn".
- **Daily follow/message caps and the random spacing between actions are shared across all three**,
  not per-source -- they belong to the one Instagram account, so the system never multiplies the
  configured limits by three. Whichever source has an eligible lead ready when the shared timer opens
  gets to send; a rotating fairness order (`NEXT_SALESPERSON` in `global_state.txt`) decides who gets
  first crack when more than one source is ready at once, so it's not always the same source winning.
- **`PROFILE_BATCH_LIMIT` is per-source, not shared** -- each salesperson's file gets its own full
  batch allowance of profile checks per cycle.
- **Pause/Resume is global** -- it affects all three sources at once, since they share one Instagram
  session (same as the existing safe-editing workflow, just now covering three files).
- **One workbook being locked/unavailable only affects that one source** -- the other two keep
  running normally that cycle. A broken Instagram session, on the other hand, pauses everything, since
  every source shares the same login.
- **Replies, seen status, and cleanup always write back to the workbook the lead came from**, regardless
  of which source's "turn" triggered the check that found them.

### Where things live

| File | Scope | Contents |
|---|---|---|
| `global_state.txt` | Shared | Mode (RUN/PAUSED/...), shared next-follow/next-message timers, last error, rotation fairness |
| `state_sales_1.txt` / `_2` / `_3` | Per salesperson | That source's own pointer/progress -- processing SP2 never touches SP1's |
| `sales_1.xlsx` / `_2` / `_3` | Per salesperson | Leads and full automation history for that source |
| `instagram_session.json`, `messages.txt`, `messages_he.txt`, `action.lock`, `app.lock` | Shared | Unchanged -- still exactly one of each |

The very first time `MULTI_SALESPERSON` is turned on, the existing single-workbook `state.txt` is
automatically read once to seed `global_state.txt` (mode, pause state, in-flight delay timers) --
nothing about your current run gets lost or reset by switching modes.

### CLI

`rewind` / `skip` / `set-pointer` take an optional `--source SP1` (defaults to the first configured
source). `status` prints the shared global state plus each source's own pointer. `pause` / `resume`
stay global, no `--source` needed.

### Dashboard

The theme bar gains a row of chips: **All salespeople**, plus one per configured source. Selecting one
filters the stats, pipeline, and lead tables to just that source (with account-wide totals still shown
alongside the daily caps, since those are shared); selecting "All salespeople" shows every source's
leads together with a Salesperson column. Rewind/Skip/Set Pointer act on whichever source is currently
selected.
