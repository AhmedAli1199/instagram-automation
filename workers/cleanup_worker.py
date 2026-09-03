import logging
from datetime import datetime, timezone, timedelta
from config import settings, get_salespeople
from excel_store import ExcelStore
from instagram_action_client import InstagramActionClient, InstagramSessionError
from state_store import pause_requested, outward_action_lock

logger = logging.getLogger(__name__)

def parse_dt(v):
    try:
        dt=datetime.fromisoformat(str(v).replace("Z","+00:00")) if v else None
        return dt if not dt or dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception: return None

def _final_reply_check(ig,lead,data):
    return ig.conversation_snapshot(lead["username"],since=data.get("Message Sent At")).get("reply",False)

def _attempt_unfollow(store,ig,lead,data,reason,reply_status,now):
    with outward_action_lock() as locked:
        if not locked: return False
        try:
            returned=ig.unfollow(lead["username"]); verified=ig.verify_unfollow(lead["username"])
        except InstagramSessionError:
            raise
        except Exception as exc:
            store.update(lead["row"], **{"Automation Status":"FILTERED","Follow Status":"UNFOLLOW_FAILED","Reply Status":reply_status,"Filtered Reason":reason,"Do Not ReFollow":"YES","Last Error":str(exc),"Last Checked At":now.isoformat(),"Next Check At":(now+timedelta(hours=settings.status_check_hours)).isoformat(),"Last Automation Action":"FILTERED_UNFOLLOW_RETRY"}); return False
    if verified:
        store.update(lead["row"], **{"Automation Status":"FILTERED","Follow Status":"UNFOLLOWED","Reply Status":reply_status,"Filtered Reason":reason,"Do Not ReFollow":"YES","Last Error":"","Last Checked At":now.isoformat(),"Last Automation Action":"UNFOLLOWED_FILTERED_VERIFIED"}); return True
    store.update(lead["row"], **{"Automation Status":"FILTERED","Follow Status":"UNFOLLOW_FAILED","Reply Status":reply_status,"Filtered Reason":reason,"Do Not ReFollow":"YES","Last Checked At":now.isoformat(),"Next Check At":(now+timedelta(hours=settings.status_check_hours)).isoformat(),"Last Automation Action":"UNFOLLOW_NOT_VERIFIED_RETRY"}); return False

def run(source=None):
    if source is None:
        source = get_salespeople()[0]
    tag = source["id"]
    store=ExcelStore(source["workbook"]); ig=InstagramActionClient(); now=datetime.now(timezone.utc); cleaned=0
    for lead in store.priority_rows():
        if pause_requested(): break
        data=store.get_row(lead["row"])
        if data.get("Message Status")!="SENT": continue
        if data.get("Reply Status") in {"REPLIED","REPLIED_LATE"}: continue
        # Retry a previously failed cleanup only at its scheduled time.
        if str(data.get("Follow Status") or "")=="UNFOLLOW_FAILED":
            nxt=parse_dt(data.get("Next Check At"))
            if nxt and now<nxt: continue
            reason=str(data.get("Filtered Reason") or "UNSEEN_7_DAYS"); rs="SEEN_NO_REPLY" if reason=="SEEN_NO_REPLY" else "UNSEEN_EXPIRED"
            if _attempt_unfollow(store,ig,lead,data,reason,rs,now): cleaned+=1
            continue
        seen_status=str(data.get("Seen Status") or "UNKNOWN").upper(); sent_at=parse_dt(data.get("Message Sent At")); seen_at=parse_dt(data.get("Message Seen At")); reason=None; rs=None
        # SEEN without a usable timestamp cannot own the 48h path; fall back to general 7-day rule.
        if seen_status=="SEEN" and seen_at:
            if now-seen_at>=timedelta(hours=settings.seen_no_reply_hours): reason="SEEN_NO_REPLY"; rs="SEEN_NO_REPLY"
        elif sent_at and now-sent_at>=timedelta(days=settings.unseen_expiry_days):
            reason="UNSEEN_7_DAYS"; rs="UNSEEN_EXPIRED"
        if not reason: continue
        try:
            if _final_reply_check(ig,lead,data):
                logger.info("Row %s (@%s): late reply found on final check, aborting cleanup", lead["row"], lead["username"])
                store.update(lead["row"], **{"Automation Status":"MANUAL_CONVERSATION","Reply Status":"REPLIED_LATE" if reason=="SEEN_NO_REPLY" else "REPLIED","Do Not ReFollow":"YES","Last Checked At":now.isoformat(),"Last Automation Action":"REPLY_DETECTED_FINAL_CHECK"}); continue
        except InstagramSessionError:
            raise
        except Exception as exc:
            # Leave this lead for next cycle rather than unfollowing on an unverified reply-check.
            logger.error("Row %s (@%s): unexpected error during final reply check: %s", lead["row"], lead["username"], exc, exc_info=True)
            continue
        logger.info("Row %s (@%s): no reply (%s), unfollowing", lead["row"], lead["username"], reason)
        if _attempt_unfollow(store,ig,lead,data,reason,rs,now): cleaned+=1
    logger.info("cleanup_worker[%s]: unfollowed %d lead(s) this cycle", tag, cleaned)
    return {"cleaned":cleaned,"source":tag}
