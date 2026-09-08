import os
from dataclasses import dataclass
from dotenv import load_dotenv

# override=True matters here specifically because of how the dashboard launches the scheduler:
# dashboard_server.py runs `subprocess.Popen([PY, "scheduler.py"], ...)` with no `env=` argument,
# so the new scheduler.py process INHERITS the dashboard's own already-populated os.environ
# (including whatever .env said the last time the dashboard itself started up). python-dotenv's
# load_dotenv() defaults to NOT overriding a variable that's already set in os.environ -- so
# without override=True, scheduler.py's own load_dotenv() call here would see OPERATING_END (or
# any other .env key) already present from the parent, silently keep that stale inherited value,
# and never notice the .env file was edited at all. This is exactly what "I edited .env and
# restarted via the dashboard's Stop/Start Scheduler buttons but it's still using the old value"
# was: Stop+Start only restarts the scheduler subprocess, not the dashboard process holding the
# stale environment it inherits from. override=True makes every fresh process launch (subprocess
# or a full restart) always read the current .env file, regardless of what it inherited.
load_dotenv(override=True)

@dataclass(frozen=True)
class Settings:
    workbook_path: str = os.getenv("WORKBOOK_PATH", "cc_automation_ready.xlsx")
    messages_file: str = os.getenv("MESSAGES_FILE", "messages.txt")
    messages_file_he: str = os.getenv("MESSAGES_FILE_HE", "messages_he.txt")
    state_file: str = os.getenv("STATE_FILE", "state.txt")
    action_lock_file: str = os.getenv("ACTION_LOCK_FILE", "action.lock")
    app_lock_file: str = os.getenv("APP_LOCK_FILE", "app.lock")
    instagram_username: str = os.getenv("INSTAGRAM_USERNAME", "")
    instagram_password: str = os.getenv("INSTAGRAM_PASSWORD", "")
    instagram_session_file: str = os.getenv("INSTAGRAM_SESSION_FILE", "instagram_session.json")
    # Optional one-time bootstrap: the `sessionid` cookie value from an already-logged-in browser
    # session. Used only the first time, when no instagram_session.json exists yet, to avoid the
    # private-API password-login endpoint (which some accounts get flagged on). Once a session file
    # exists this is never read again -- safe to clear from .env afterward.
    instagram_sessionid: str = os.getenv("INSTAGRAM_SESSIONID", "")
    timezone: str = os.getenv("TIMEZONE", "Asia/Jerusalem")
    operating_start: str = os.getenv("OPERATING_START", "06:00")
    operating_end: str = os.getenv("OPERATING_END", "23:00")
    daily_message_limit: int = int(os.getenv("DAILY_MESSAGE_LIMIT", "7"))
    daily_follow_limit: int = int(os.getenv("DAILY_FOLLOW_LIMIT", "5"))
    message_ready_buffer: int = int(os.getenv("MESSAGE_READY_BUFFER", "9"))
    profile_batch_limit: int = int(os.getenv("PROFILE_BATCH_LIMIT", "5"))
    profile_check_min_delay_seconds: float = float(os.getenv("PROFILE_CHECK_MIN_DELAY_SECONDS", "20"))
    profile_check_max_delay_seconds: float = float(os.getenv("PROFILE_CHECK_MAX_DELAY_SECONDS", "60"))
    status_check_hours: int = int(os.getenv("STATUS_CHECK_HOURS", "12"))
    private_follow_expiry_days: int = int(os.getenv("PRIVATE_FOLLOW_EXPIRY_DAYS", "7"))
    unseen_expiry_days: int = int(os.getenv("UNSEEN_EXPIRY_DAYS", "7"))
    seen_no_reply_hours: int = int(os.getenv("SEEN_NO_REPLY_HOURS", "48"))
    message_min_delay_hours: float = float(os.getenv("MESSAGE_MIN_DELAY_HOURS", "3"))
    message_max_delay_hours: float = float(os.getenv("MESSAGE_MAX_DELAY_HOURS", "4"))
    follow_min_delay_hours: float = float(os.getenv("FOLLOW_MIN_DELAY_HOURS", "2.5"))
    follow_max_delay_hours: float = float(os.getenv("FOLLOW_MAX_DELAY_HOURS", "3.5"))
    cycle_seconds: int = int(os.getenv("CYCLE_SECONDS", "60"))

    # --- Follow-Back Timing V2 ---------------------------------------------------------------
    # After a Follow succeeds (public auto-accept or private approval), the DM is no longer sent
    # immediately -- the lead waits up to follow_back_wait_hours (Y) for the target to follow us
    # back first. An early follow-back triggers the DM after a short stabilization delay; no
    # follow-back by Y still sends exactly one proactive DM (follow-back monitoring continues
    # afterward, purely for stats and the eventual cleanup below). If no follow-back exists by
    # follow_back_timeout_hours from the *original* Follow Requested At, the lead is unfollowed.
    # Global/shared across SP1-3, same as every other account-level setting.
    #
    # follow_back_timeout_hours was originally specced/shipped at 168h (7 days, "the hard
    # deadline"). Cut to 72h (3 days) on 2026-09-08 at Amit's explicit request -- the 7-day
    # backlog from a freshly-shipped feature (most follows not yet old enough to be eligible for
    # cleanup at all) read as "unfollow isn't working" from the outside. Both cleanup workers
    # already process every eligible row every single cycle with no batch limit -- there was no
    # actual throughput problem to fix, only the eligibility window itself.
    follow_back_wait_hours: float = float(os.getenv("FOLLOW_BACK_WAIT_HOURS", "24"))
    follow_back_dm_delay_minutes: float = float(os.getenv("FOLLOW_BACK_DM_DELAY_MINUTES", "10"))
    follow_back_timeout_hours: float = float(os.getenv("FOLLOW_BACK_TIMEOUT_HOURS", "72"))
    follow_back_check_interval_minutes: float = float(os.getenv("FOLLOW_BACK_CHECK_INTERVAL_MINUTES", "10"))

    # --- Multi-salesperson mode -------------------------------------------------------------
    # Off by default: with MULTI_SALESPERSON unset/false, get_salespeople() below returns a
    # single source pointing at the same workbook_path/state_file as always, so every existing
    # code path behaves exactly as it did in single-workbook mode. Turning this on switches to
    # three independent workbooks (one per salesperson) sharing one Instagram account, one set
    # of daily limits, and one pause/resume state.
    multi_salesperson: bool = os.getenv("MULTI_SALESPERSON", "false").strip().lower() in ("1", "true", "yes", "on")
    global_state_file: str = os.getenv("GLOBAL_STATE_FILE", "global_state.txt")
    sp1_name: str = os.getenv("SP1_NAME", "Salesperson 1")
    sp1_workbook: str = os.getenv("SP1_WORKBOOK", "sales_1.xlsx")
    sp1_state: str = os.getenv("SP1_STATE", "state_sales_1.txt")
    sp2_name: str = os.getenv("SP2_NAME", "Salesperson 2")
    sp2_workbook: str = os.getenv("SP2_WORKBOOK", "sales_2.xlsx")
    sp2_state: str = os.getenv("SP2_STATE", "state_sales_2.txt")
    sp3_name: str = os.getenv("SP3_NAME", "Salesperson 3")
    sp3_workbook: str = os.getenv("SP3_WORKBOOK", "sales_3.xlsx")
    sp3_state: str = os.getenv("SP3_STATE", "state_sales_3.txt")

settings = Settings()

if not (0 < settings.follow_back_wait_hours < settings.follow_back_timeout_hours):
    raise SystemExit(
        f"Invalid .env: FOLLOW_BACK_WAIT_HOURS ({settings.follow_back_wait_hours}) must be greater "
        f"than 0 and less than FOLLOW_BACK_TIMEOUT_HOURS ({settings.follow_back_timeout_hours})."
    )


def get_salespeople():
    """The list of lead sources to process. In single-workbook mode (the default) this is one
    source that resolves to exactly settings.workbook_path / settings.state_file -- the same
    files every existing single-source deployment already uses -- so nothing about single-source
    behavior changes. In multi-salesperson mode it's the three configured sources, each with its
    own workbook and pointer/progress state file, all sharing one Instagram account."""
    if not settings.multi_salesperson:
        return [{"id": "SP1", "name": "default", "workbook": settings.workbook_path, "state": settings.state_file}]
    return [
        {"id": "SP1", "name": settings.sp1_name, "workbook": settings.sp1_workbook, "state": settings.sp1_state},
        {"id": "SP2", "name": settings.sp2_name, "workbook": settings.sp2_workbook, "state": settings.sp2_state},
        {"id": "SP3", "name": settings.sp3_name, "workbook": settings.sp3_workbook, "state": settings.sp3_state},
    ]
