import logging
import random, time
from datetime import datetime, timezone, timedelta
from config import settings, get_salespeople
from excel_store import ExcelStore, shared_today_count
from instagram_action_client import InstagramActionClient, InstagramVerificationError, InstagramSessionError
from state_store import outward_action_lock, load_state, save_state, pause_requested
from .common import inside_hours, start_follow_back_wait

logger = logging.getLogger(__name__)

def parse_dt(v):
    try: return datetime.fromisoformat(str(v)) if v else None
    except Exception: return None

def _schedule_next(now):
    delay=random.uniform(settings.follow_min_delay_hours,settings.follow_max_delay_hours)
    save_state(NEXT_FOLLOW_NOT_BEFORE=(now+timedelta(hours=delay)).isoformat())

def _record_follow_outcome(store,row,ig,username,now,verified_action):
    """Corrected rule: every lead gets a follow, public or private. A public follow is
    auto-accepted by Instagram instantly, so check the real post-follow relationship rather
    than assuming every follow means 'now waiting for approval'.

    "Follow Requested At" is set in BOTH branches -- it marks the moment the actual follow
    action was taken against Instagram (the event the daily cap in run() below counts via
    shared_today_count), regardless of whether the profile turned out to be public (instant
    accept) or private (pending approval). Previously only the private branch set it, so every
    public-profile follow -- the majority of them -- silently never counted against
    DAILY_FOLLOW_LIMIT at all, letting the real daily follow count run well past the configured
    cap."""
    rel=ig.relationship(username)
    if rel.get("following"):
        # Follow-Back Timing V2: no longer straight to READY_TO_MESSAGE -- start the Y-hour /
        # 168-hour follow-back clock instead. `now` IS the original Follow Requested At here
        # (public auto-accept happens in the same instant as the request), so it's also the
        # correct T0 for the new deadlines.
        store.update(row, **{**start_follow_back_wait(now),"Follow Status":"ACCEPTED","Follow Requested At":now.isoformat(),"Follow Accepted At":now.isoformat(),"Retry Count":0,"Last Error":"","Last Automation Action":f"{verified_action}_PUBLIC_AUTO_ACCEPTED"})
        logger.info("Row %s (@%s): follow auto-accepted (public profile), waiting for follow-back", row, username)
    else:
        store.update(row, **{"Automation Status":"WAITING_FOLLOW_APPROVAL","Follow Status":"REQUESTED","Follow Requested At":now.isoformat(),"Retry Count":0,"Last Error":"","Last Automation Action":verified_action})
        logger.info("Row %s (@%s): follow request sent, waiting for approval (private profile)", row, username)

def run(limit=1, source=None):
    if source is None:
        source = get_salespeople()[0]
    tag = source["id"]
    if not inside_hours():
        logger.info("follow_worker[%s]: outside operating hours, skipping", tag)
        return {"followed":0,"source":tag}
    store=ExcelStore(source["workbook"])
    # Shared across every source -- the daily cap and the single next-follow-time both belong
    # to the Instagram account, not to any one salesperson's workbook.
    remaining=settings.daily_follow_limit-shared_today_count("Follow Requested At", get_salespeople())
    if remaining<=0:
        logger.info("follow_worker[%s]: daily follow limit (%d) reached -- pausing NEW follows only; every other worker continues normally this cycle", tag, settings.daily_follow_limit)
        return {"followed":0,"limit_reached":True,"source":tag}
    not_before=parse_dt(load_state().get("NEXT_FOLLOW_NOT_BEFORE")); now=datetime.now(timezone.utc)
    if not_before and now<not_before:
        logger.info("follow_worker[%s]: waiting for random delay window, next follow not before %s", tag, not_before.isoformat())
        return {"followed":0,"next_follow_not_before":not_before.isoformat(),"source":tag}
    ig=InstagramActionClient(); followed=0
    for lead in store.priority_rows():
        if pause_requested(): break
        if followed>=min(limit,remaining): break
        data=store.get_row(lead["row"])
        if store.apply_hard_filter_if_needed(lead["row"],allow_blank=True): continue
        if not store.validate_automation_username(lead["row"],lead["username"]): continue
        if store.do_not_refollow(lead["row"]): continue
        # Corrected rule: every lead (public or private) that reached PROCESSING without
        # already following/having an outgoing request needs a follow -- not just private ones.
        if data.get("Automation Status")!="PROCESSING": continue
        follow_status=str(data.get("Follow Status") or "").upper()
        if follow_status in {"REQUESTED","ACCEPTED","UNFOLLOWED","EXPIRED","FAILED","UNCERTAIN","CANCEL_PENDING"}: continue
        if follow_status=="FOLLOW_STARTING":
            try:
                if ig.verify_follow(lead["username"]):
                    _record_follow_outcome(store,lead["row"],ig,lead["username"],datetime.now(timezone.utc),"FOLLOW_RECOVERED_VERIFIED"); _schedule_next(datetime.now(timezone.utc)); followed+=1
                else:
                    store.update(lead["row"], **{"Automation Status":"PROCESSING","Follow Status":"RETRY_QUEUED","Retry Count":1,"Last Automation Action":"FOLLOW_RECOVERY_CONFIRMED_NOT_SENT"})
            except InstagramSessionError:
                raise
            except Exception as exc:
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Follow Status":"UNCERTAIN","Last Error":str(exc),"Last Automation Action":"FOLLOW_UNCERTAIN_AFTER_RESTART"})
            continue
        with outward_action_lock() as locked:
            if not locked: return {"followed":followed,"action_lock_busy":True,"source":tag}
            try:
                rel=ig.relationship(lead["username"])
            except InstagramSessionError:
                raise
            except Exception as exc:
                logger.error("Row %s (@%s): unexpected error checking relationship before follow: %s", lead["row"], lead["username"], exc, exc_info=True)
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Last Error":str(exc),"Last Automation Action":"FOLLOW_PRECHECK_ERROR"}); continue
            if rel.get("following"):
                # Discovering we already follow this lead (from outside this automation, or an
                # earlier run) is still "Follow confirmed, first DM not yet sent" -- routed
                # through the same Follow-Back Timing V2 wait as a fresh follow, for one
                # consistent message-eligibility path rather than a second bypass around it. No
                # real "original request" moment exists for this case, so the discovery instant
                # is used as T0 -- the best available anchor.
                now=datetime.now(timezone.utc)
                store.update(lead["row"], **{**start_follow_back_wait(now),"Follow Status":"ACCEPTED","Follow Accepted At":now.isoformat(),"Last Automation Action":"ALREADY_FOLLOWING"}); continue
            if rel.get("outgoing_request"):
                _record_follow_outcome(store,lead["row"],ig,lead["username"],datetime.now(timezone.utc),"ALREADY_PENDING"); continue
            store.update(lead["row"], **{"Follow Status":"FOLLOW_STARTING","Last Automation Action":"FOLLOW_STARTING"})
            attempt_at=datetime.now(timezone.utc)
            try:
                returned=ig.follow(lead["username"])
                time.sleep(1)
                verified=ig.verify_follow(lead["username"])
                if verified:
                    logger.info("Row %s (@%s): follow request verified, next follow delayed randomly", lead["row"], lead["username"])
                    _record_follow_outcome(store,lead["row"],ig,lead["username"],attempt_at,"FOLLOW_REQUESTED_VERIFIED" if returned else "FOLLOW_VERIFIED_AFTER_FALSE_RETURN")
                    _schedule_next(attempt_at); followed+=1; continue
                raise RuntimeError("Follow was not verified after action")
            except InstagramSessionError:
                # The session itself is broken, not this one lead -- every other lead would fail
                # the same way, so pause the whole scheduler instead of marking each one UNCERTAIN.
                raise
            except Exception as exc:
                logger.error("Row %s (@%s): follow attempt failed: %s", lead["row"], lead["username"], exc, exc_info=True)
                # Only retry automatically if relationship verification proves the action did not happen.
                try: definitely_not_sent=not ig.verify_follow(lead["username"])
                except Exception: definitely_not_sent=False
                current=store.get_row(lead["row"]); retries=int(current.get("Retry Count") or 0)
                if definitely_not_sent and retries<1:
                    store.update(lead["row"], **{"Automation Status":"PROCESSING","Follow Status":"RETRY_QUEUED","Retry Count":retries+1,"Last Error":str(exc),"Last Automation Action":"FOLLOW_RETRY_QUEUED"})
                    _schedule_next(datetime.now(timezone.utc))
                elif definitely_not_sent:
                    store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Follow Status":"FAILED","Filtered Reason":"FAILED_FOLLOW","Last Error":str(exc),"Last Automation Action":"FOLLOW_FAILED"})
                else:
                    store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Follow Status":"UNCERTAIN","Last Error":str(exc),"Automation Notes":"Follow outcome could not be verified. Automatic re-follow disabled.","Last Automation Action":"FOLLOW_UNCERTAIN"})
    logger.info("follow_worker[%s]: sent %d follow request(s) this cycle", tag, followed)
    return {"followed":followed,"source":tag}
