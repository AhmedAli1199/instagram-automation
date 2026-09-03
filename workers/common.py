from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import settings

TERMINAL_MESSAGE_STATUSES = {"SENT", "FAILED", "UNCERTAIN"}

def start_follow_back_wait(t0):
    """Fields to set the moment a lead's OWN Follow is confirmed (public auto-accept, private
    approval, or discovering we already follow them) -- starts the Follow-Back Timing V2 clock
    (see workers/follow_back_monitor.py) instead of jumping straight to READY_TO_MESSAGE.

    `t0` must be the ORIGINAL Follow Requested At moment (T0), not "whenever this transition
    happens to run" -- per spec, both the Y-hour and 168-hour deadlines are measured from T0. For
    a public auto-accept these are the same instant; for a private profile, acceptance can land
    hours or days after the original request, so the caller must pass the real stored Follow
    Requested At, not `datetime.now()`, or both deadlines would silently drift later than
    intended every time."""
    return {
        "Automation Status": "WAITING_FOLLOW_BACK",
        "Follow Back Status": "WAITING",
        "Message Fallback At": (t0 + timedelta(hours=settings.follow_back_wait_hours)).isoformat(),
        "Follow Back Deadline At": (t0 + timedelta(hours=settings.follow_back_timeout_hours)).isoformat(),
    }

def normalize_priority(value):
    if value is None or str(value).strip() == "": return None
    try: return int(float(value))
    except Exception: return "INVALID"

def local_priority(g, h, allow_blank=False):
    g_val, h_val = normalize_priority(g), normalize_priority(h)
    if g_val == 0 or h_val == 0: return False, "PRIORITY_ZERO"
    if g_val == "INVALID" or h_val == "INVALID": return False, "INVALID_PRIORITY"
    if allow_blank and (g_val is None or h_val is None): return True, None
    if g_val is None or h_val is None: return False, "INVALID_PRIORITY"
    if g_val not in (1,2,3) or h_val not in (1,2,3): return False, "INVALID_PRIORITY"
    return True, None

def inside_hours():
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    sh, sm = map(int, settings.operating_start.split(":"))
    eh, em = map(int, settings.operating_end.split(":"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        return start <= now <= end
    # Overnight window (e.g. OPERATING_START=06:00, OPERATING_END=03:00 -- meaning "operate from
    # 6am until 3am the *next* day"): `end` computed above lands on today's calendar date, before
    # `start`, so a plain start<=now<=end check is an inverted/empty range and is False for every
    # moment of every day, no matter the actual time -- that's the bug this branch fixes. When the
    # configured end time is numerically earlier than the start time, the window spans midnight:
    # "now" is inside it if it's at/after today's start, OR at/before today's end (which is really
    # tonight's start through tomorrow's end).
    return now >= start or now <= end
