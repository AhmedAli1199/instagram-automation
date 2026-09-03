import logging
from datetime import datetime, timezone, timedelta
from config import settings, get_salespeople
from excel_store import ExcelStore
from instagram_action_client import InstagramActionClient, InstagramProfileNotFound, InstagramSessionError
from state_store import pause_requested, outward_action_lock

logger = logging.getLogger(__name__)

def parse_dt(v):
    try:
        dt = datetime.fromisoformat(str(v)) if v else None
        return dt if not dt or dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _attempt_unfollow(store, ig, lead, now):
    """Verified-unfollow pattern matching follow_status_worker.py's expiry cleanup exactly: never
    claim success without independently re-checking the relationship afterward. If the API result
    is uncertain, the row is left retryable rather than marked done -- per spec section 9, "If the
    Unfollow result is uncertain or verification fails, do not claim success; preserve cleanup
    state for retry/manual review."."""
    with outward_action_lock() as locked:
        if not locked:
            return False
        try:
            ig.unfollow(lead["username"])
            verified = ig.verify_unfollow(lead["username"])
        except InstagramSessionError:
            raise
        except Exception as exc:
            logger.error("Row %s (@%s): 7-day cleanup unfollow failed: %s", lead["row"], lead["username"], exc, exc_info=True)
            store.update(lead["row"], **{
                "Follow Status": "UNFOLLOW_FAILED", "Last Error": str(exc),
                "Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_CLEANUP_RETRY",
            })
            return False
    if verified:
        store.update(lead["row"], **{
            "Follow Status": "UNFOLLOWED", "Follow Back Status": "EXPIRED_NO_RETURN",
            "Follow Cleanup Reason": "NO_FOLLOW_BACK_7_DAYS", "Unfollowed At": now.isoformat(),
            "Do Not ReFollow": "YES", "Last Error": "", "Last Checked At": now.isoformat(),
            "Last Automation Action": "FOLLOW_BACK_CLEANUP_UNFOLLOWED_VERIFIED",
        })
        logger.info("Row %s (@%s): no follow-back after 7 days, unfollowed (verified)", lead["row"], lead["username"])
        return True
    store.update(lead["row"], **{
        "Follow Status": "UNFOLLOW_FAILED", "Last Checked At": now.isoformat(),
        "Last Automation Action": "FOLLOW_BACK_CLEANUP_UNFOLLOW_NOT_VERIFIED",
    })
    return False

def run(source=None):
    # `source` identifies which salesperson's workbook/state to use, same convention as every
    # other worker -- Y/168h are global settings, but each row is only ever read from and written
    # back to the workbook that owns it (multi-salesperson spec section 10).
    if source is None:
        source = get_salespeople()[0]
    tag = source["id"]
    store = ExcelStore(source["workbook"]); ig = InstagramActionClient(); now = datetime.now(timezone.utc)
    checked = 0
    interval = timedelta(minutes=settings.follow_back_check_interval_minutes)
    for lead in store.priority_rows():
        if pause_requested(): break
        data = store.get_row(lead["row"])
        if data.get("Automation Status") not in ("WAITING_FOLLOW_BACK", "READY_TO_MESSAGE"): continue
        fb_status = str(data.get("Follow Back Status") or "").upper()
        # Only rows this feature actually put in play (has a Follow Back Status) -- a
        # READY_TO_MESSAGE row from a path that predates this feature, or one that reached
        # READY_TO_MESSAGE through some other route entirely, has no Follow Back Status at all and
        # is correctly left alone rather than being pulled into 7-day cleanup on missing data.
        if not fb_status: continue
        if fb_status in ("EXPIRED_NO_RETURN",): continue  # 7-day cleanup already ran
        last = parse_dt(data.get("Last Checked At"))
        if last and now - last < interval: continue

        deadline = parse_dt(data.get("Follow Back Deadline At"))
        fallback_at = parse_dt(data.get("Message Fallback At"))

        try:
            rel = ig.relationship(lead["username"])
        except InstagramProfileNotFound:
            logger.warning("Row %s (@%s): username no longer found during follow-back check, filtering", lead["row"], lead["username"])
            store.update(lead["row"], **{"Automation Status": "FILTERED", "Filtered Reason": "USERNAME_NOT_FOUND", "Do Not ReFollow": "YES", "Last Checked At": now.isoformat(), "Last Automation Action": "PROFILE_NOT_FOUND"}); continue
        except InstagramSessionError:
            raise
        except Exception as exc:
            # One lead's unexpected failure must not stall every other pending follow-back check.
            logger.error("Row %s (@%s): unexpected error during follow-back check: %s", lead["row"], lead["username"], exc, exc_info=True)
            store.update(lead["row"], **{"Last Error": str(exc), "Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_CHECK_ERROR"}); continue
        checked += 1
        followed_back = bool(rel.get("followed_by"))
        # Has this row already been given ITS one message trigger (via early follow-back or Y
        # timeout)? Once true, a follow-back detected from here on is ALWAYS "late" -- record it
        # for stats, never re-derive/overwrite the trigger. Checking this explicitly (rather than
        # just Follow Back Status == WAITING) is what keeps a Y_TIMEOUT_PROACTIVE trigger from
        # being silently overwritten with FOLLOW_BACK when the target follows back afterward --
        # both leave Follow Back Status at WAITING right up until a follow-back actually lands, so
        # status alone can't tell "never triggered yet" apart from "already triggered, still
        # waiting on the reciprocal follow".
        already_triggered = bool(data.get("Message Trigger"))

        # 168-hour deadline takes priority over everything else -- a final live check right now,
        # regardless of what Follow Back Status currently says.
        if deadline and now >= deadline:
            if followed_back:
                if fb_status != "RECEIVED":
                    store.update(lead["row"], **{"Follow Back Status": "RECEIVED", "Follow Back At": data.get("Follow Back At") or now.isoformat(), "Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_DEADLINE_CHECK_FOLLOWING"})
                else:
                    store.update(lead["row"], **{"Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_DEADLINE_CHECK_FOLLOWING"})
                continue
            _attempt_unfollow(store, ig, lead, now); continue

        if followed_back and not already_triggered:
            # Early (or on-time) return-follow, first time detected. Don't jump straight to
            # READY_TO_MESSAGE -- give the relationship state the short stabilization delay from
            # spec before the lead becomes DM-eligible.
            eligible_at = now + timedelta(minutes=settings.follow_back_dm_delay_minutes)
            store.update(lead["row"], **{
                "Automation Status": "READY_TO_MESSAGE", "Follow Back Status": "RECEIVED",
                "Follow Back At": now.isoformat(), "Message Trigger": "FOLLOW_BACK",
                "Message Eligible At": eligible_at.isoformat(), "Last Checked At": now.isoformat(),
                "Last Automation Action": "FOLLOW_BACK_RECEIVED",
            })
            logger.info("Row %s (@%s): follow-back received, DM eligible at %s", lead["row"], lead["username"], eligible_at.isoformat())
            continue

        if not followed_back and not already_triggered and fallback_at and now >= fallback_at:
            # Y elapsed with no follow-back -- exactly one proactive DM, follow-back status stays
            # WAITING (monitoring continues; a later follow-back is still recorded below, it just
            # never triggers a second message).
            store.update(lead["row"], **{
                "Automation Status": "READY_TO_MESSAGE", "Message Trigger": "Y_TIMEOUT_PROACTIVE",
                "Message Eligible At": now.isoformat(), "Last Checked At": now.isoformat(),
                "Last Automation Action": "FOLLOW_BACK_Y_TIMEOUT",
            })
            logger.info("Row %s (@%s): no follow-back after Y=%.0fh, proactive DM now eligible", lead["row"], lead["username"], settings.follow_back_wait_hours)
            continue

        if followed_back and fb_status != "RECEIVED":
            # Late follow-back -- either already_triggered (proactive DM already went out) or a
            # follow-back landed in the same tick Y elapsed without going through the branch
            # above for some reason. Record it for stats, never send a second first-contact
            # message (message_sender's own gate is keyed on Message Status already being SENT,
            # not on this field, so this update alone can't cause a duplicate send).
            store.update(lead["row"], **{"Follow Back Status": "RECEIVED", "Follow Back At": now.isoformat(), "Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_RECEIVED_LATE"})
            logger.info("Row %s (@%s): late follow-back recorded, no second DM", lead["row"], lead["username"])
            continue

        # Nothing changed this check -- just record that we looked, so the interval-gate above
        # paces the next one correctly.
        store.update(lead["row"], **{"Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_CHECK"})
    logger.info("follow_back_monitor[%s]: checked %d row(s) this cycle", tag, checked)
    return {"checked": checked, "source": tag}
