import logging
from datetime import datetime, timezone, timedelta
from config import settings, get_salespeople
from excel_store import ExcelStore
from instagram_action_client import InstagramActionClient, InstagramProfileNotFound, InstagramSessionError
from state_store import pause_requested, outward_action_lock
from .common import start_follow_back_wait

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
            # Automation Status was NOT being updated here before -- a fully-resolved (unfollowed)
            # lead stayed showing WAITING_FOLLOW_BACK forever, which read as "still waiting" even
            # though cleanup had already completed. FILTERED matches the exact same convention
            # cleanup_worker.py's own (older) no-reply unfollow path already uses -- both mean
            # "this lead is done, no further automation" and are already correctly excluded from
            # every other worker via NO_REFOLLOW_STATUSES in excel_store.py.
            "Automation Status": "FILTERED",
            "Follow Status": "UNFOLLOWED", "Follow Back Status": "EXPIRED_NO_RETURN",
            "Follow Cleanup Reason": "NO_FOLLOW_BACK_7_DAYS", "Unfollowed At": now.isoformat(),
            "Do Not ReFollow": "YES", "Last Error": "", "Last Checked At": now.isoformat(),
            "Last Automation Action": "FOLLOW_BACK_CLEANUP_UNFOLLOWED_VERIFIED",
        })
        # Was hardcoded to say "7 days" regardless of the actually configured value -- misleading
        # when diagnosing this from the log (Amit's real FOLLOW_BACK_TIMEOUT_HOURS was 36, not 168,
        # so every one of these lines was already wrong before the timing bug above even mattered).
        logger.info("Row %s (@%s): no follow-back after %.0fh (FOLLOW_BACK_TIMEOUT_HOURS), unfollowed (verified)", lead["row"], lead["username"], settings.follow_back_timeout_hours)
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
        auto_status = data.get("Automation Status")
        follow_status = str(data.get("Follow Status") or "").upper()
        fb_status = str(data.get("Follow Back Status") or "").upper()
        reply_status = str(data.get("Reply Status") or "").upper()

        if not fb_status:
            # BACKFILL: a lead followed BEFORE this feature shipped never had Follow Back Status
            # set at all, and used to be skipped here forever as a result -- reported as "keeps
            # following new people but never unfollows old ones". Any lead we're currently
            # following, not already resolved (replied / do-not-refollow), with a real Follow
            # Requested At to compute deadlines from, gets backfilled into this tracking using
            # that ORIGINAL timestamp as T0 (not "now" -- an old follow must not get a fresh
            # 72h/168h clock just because we only just noticed it) and falls through to be
            # evaluated on this very same pass, exactly as if it had gone through the new
            # pipeline from the start.
            if follow_status != "ACCEPTED": continue  # not currently following -- nothing to evaluate
            if store.do_not_refollow(lead["row"]): continue
            if reply_status in ("REPLIED", "REPLIED_LATE"): continue  # active conversation, leave it alone
            requested_at = parse_dt(data.get("Follow Requested At"))
            if not requested_at: continue  # no timestamp to compute a deadline from
            backfill = start_follow_back_wait(requested_at)
            del backfill["Automation Status"]  # don't force MESSAGE_SENT/etc back to WAITING_FOLLOW_BACK
            store.update(lead["row"], **backfill)
            data = {**data, **backfill}
            fb_status = "WAITING"
            logger.info("Row %s (@%s): backfilled into follow-back tracking (pre-dates this feature), Follow Requested At=%s", lead["row"], lead["username"], requested_at.isoformat())
        elif auto_status not in ("WAITING_FOLLOW_BACK", "READY_TO_MESSAGE", "MESSAGE_SENT"):
            continue
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
        # A backfilled legacy lead can already have a real message sent under the old pipeline
        # (Message Status=SENT) despite never having a Message Trigger -- must NOT be treated as
        # "first time detected, needs a DM" in that case (that would flip Automation Status back
        # to READY_TO_MESSAGE for a lead that's actually done, though Message Status=SENT would
        # still correctly block message_prepare_worker/message_sender from re-sending -- this
        # keeps the displayed status accurate too, not just the send behavior safe).
        already_messaged = str(data.get("Message Status") or "").upper() in ("SENT", "SENDING", "UNCERTAIN")

        # ORDER MATTERS HERE: the proactive-DM check (a few lines down) must run BEFORE the
        # deadline/unfollow check. If a row simply doesn't get evaluated for a while -- scheduler
        # restarted or paused, a slow cycle, anything that delays its next check -- by the time it
        # IS finally checked, both "Y hours passed" and "deadline passed" can already be true at
        # once. Checking the deadline first (as this used to) would unfollow the lead without ever
        # having given it its one proactive DM -- exactly "no DM sent, then unfollowed", reported
        # on more than one lead. Checking the Y-timeout DM trigger first guarantees every lead
        # gets that one message before it can ever be unfollowed for silence, no matter how late
        # the check happens to land.
        if followed_back and not already_triggered and not already_messaged:
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

        if not followed_back and not already_triggered and not already_messaged and fallback_at and now >= fallback_at:
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

        # Deadline check comes AFTER both DM-trigger checks above (see the ordering note there).
        #
        # CRITICAL: once a message has been triggered OR already sent for this lead
        # (already_triggered/already_messaged), the follow-back deadline no longer applies AT ALL
        # -- ownership of "should we give up on this lead" transfers entirely to
        # cleanup_worker.py's own separate, independent reply-based timers
        # (SEEN_NO_REPLY_HOURS / UNSEEN_EXPIRY_DAYS). This is the actual fix for a real bug found
        # from live logs: a lead's row going unchecked for a while (any ordinary delay, not an
        # outage) meant Y and the deadline could both already be past by the time it was finally
        # checked. The earlier ordering fix made sure the DM fired first in that same pass, but did
        # nothing to stop the SAME already-passed deadline from firing again on the very next
        # check, just minutes later -- unfollowing a lead within an hour of messaging it, before it
        # had any real chance to reply. Confirmed directly from two real log timelines: DM sent,
        # then unfollowed 51 and 64 minutes later, both logged as "no follow-back after 7 days"
        # (Amit's actual configured deadline was 36 hours -- the log wording was also wrong, fixed
        # below). Two independent unfollow clocks racing each other over the same lead was the
        # root cause; a lead we've messaged is no longer this clock's problem.
        if deadline and now >= deadline and not already_triggered and not already_messaged:
            if followed_back:
                if fb_status != "RECEIVED":
                    store.update(lead["row"], **{"Follow Back Status": "RECEIVED", "Follow Back At": data.get("Follow Back At") or now.isoformat(), "Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_DEADLINE_CHECK_FOLLOWING"})
                else:
                    store.update(lead["row"], **{"Last Checked At": now.isoformat(), "Last Automation Action": "FOLLOW_BACK_DEADLINE_CHECK_FOLLOWING"})
                continue
            _attempt_unfollow(store, ig, lead, now); continue

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
