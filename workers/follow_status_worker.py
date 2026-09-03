import logging
from datetime import datetime, timezone, timedelta
from config import settings, get_salespeople
from excel_store import ExcelStore
from instagram_action_client import InstagramActionClient, InstagramProfileNotFound, InstagramSessionError
from state_store import pause_requested, outward_action_lock
from .common import start_follow_back_wait

logger = logging.getLogger(__name__)

def parse_dt(v):
    try: return datetime.fromisoformat(str(v)) if v else None
    except Exception: return None

def run(source=None):
    if source is None:
        source = get_salespeople()[0]
    tag = source["id"]
    store=ExcelStore(source["workbook"]); ig=InstagramActionClient(); now=datetime.now(timezone.utc); checked=0
    for lead in store.priority_rows():
        if pause_requested(): break
        data=store.get_row(lead["row"])
        if data.get("Automation Status")!="WAITING_FOLLOW_APPROVAL": continue
        if store.do_not_refollow(lead["row"]): continue
        last=parse_dt(data.get("Last Checked At"))
        if last and now-last<timedelta(hours=settings.status_check_hours): continue
        try:
            rel=ig.relationship(lead["username"])
            profile=ig.get_profile(lead["username"])
        except InstagramProfileNotFound:
            logger.warning("Row %s (@%s): username no longer found, filtering", lead["row"], lead["username"])
            store.update(lead["row"], **{"Automation Status":"FILTERED","Filtered Reason":"USERNAME_NOT_FOUND","Do Not ReFollow":"YES","Last Checked At":now.isoformat(),"Last Automation Action":"PROFILE_NOT_FOUND"}); continue
        except InstagramSessionError:
            raise  # login/session itself is broken -- worth pausing the whole scheduler for
        except Exception as exc:
            # One lead's unexpected failure must not stall every other pending follow-up check.
            logger.error("Row %s (@%s): unexpected error during follow-status check: %s", lead["row"], lead["username"], exc, exc_info=True)
            store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Last Error":str(exc),"Last Checked At":now.isoformat(),"Last Automation Action":"FOLLOW_STATUS_CHECK_ERROR"}); continue
        checked+=1
        if rel.get("following"):
            logger.info("Row %s (@%s): follow request accepted, waiting for follow-back", lead["row"], lead["username"])
            # Follow-Back Timing V2: T0 for the Y-hour/168-hour clock is the ORIGINAL Follow
            # Requested At (already stored, from when the request was first sent) -- NOT `now`
            # (the moment approval happened to be detected, which can be hours/days later for a
            # private profile). Falls back to `now` only if that original timestamp is somehow
            # unparseable/missing, so this never crashes on bad/legacy data.
            requested_at=parse_dt(data.get("Follow Requested At")) or now
            store.update(lead["row"], **{**start_follow_back_wait(requested_at),"Follow Status":"ACCEPTED","Follow Accepted At":now.isoformat(),"Last Checked At":now.isoformat(),"Last Automation Action":"FOLLOW_ACCEPTED"}); continue
        if profile.get("is_private") is False:
            logger.info("Row %s (@%s): profile became public without accepting, filtering", lead["row"], lead["username"])
            store.update(lead["row"], **{"Automation Status":"FILTERED","Filtered Reason":"BECAME_PUBLIC_WITHOUT_ACCEPTING","Do Not ReFollow":"YES","Last Checked At":now.isoformat(),"Last Automation Action":"FILTERED"}); continue
        requested=parse_dt(data.get("Follow Requested At"))
        if requested and now-requested>=timedelta(days=settings.private_follow_expiry_days):
            logger.info("Row %s (@%s): follow request expired after %d days, cancelling", lead["row"], lead["username"], settings.private_follow_expiry_days)
            with outward_action_lock() as locked:
                if not locked: continue
                try:
                    returned=ig.unfollow(lead["username"])
                    verified=ig.verify_unfollow(lead["username"])
                except Exception as exc:
                    logger.error("Row %s (@%s): follow-cancel failed: %s", lead["row"], lead["username"], exc, exc_info=True)
                    store.update(lead["row"], **{"Follow Status":"CANCEL_PENDING","Last Error":str(exc),"Last Checked At":now.isoformat(),"Next Check At":(now+timedelta(hours=settings.status_check_hours)).isoformat(),"Last Automation Action":"FOLLOW_CANCEL_RETRY"}); continue
            if verified:
                store.update(lead["row"], **{"Automation Status":"FILTERED","Follow Status":"EXPIRED","Filtered Reason":"NO_FOLLOW_ACCEPTANCE","Do Not ReFollow":"YES","Last Checked At":now.isoformat(),"Last Error":"","Last Automation Action":"FOLLOW_EXPIRED_VERIFIED"})
            else:
                store.update(lead["row"], **{"Follow Status":"CANCEL_PENDING","Last Checked At":now.isoformat(),"Next Check At":(now+timedelta(hours=settings.status_check_hours)).isoformat(),"Last Automation Action":"FOLLOW_CANCEL_NOT_VERIFIED"})
        else:
            store.update(lead["row"], **{"Last Checked At":now.isoformat(),"Next Check At":(now+timedelta(hours=settings.status_check_hours)).isoformat(),"Last Automation Action":"FOLLOW_STATUS_CHECK"})
    logger.info("follow_status_worker[%s]: checked %d pending follow(s)", tag, checked)
    return {"checked":checked,"source":tag}
