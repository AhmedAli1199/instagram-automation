import logging
import random, time
from datetime import datetime, timezone, timedelta
from config import settings, get_salespeople
from excel_store import ExcelStore, shared_today_count
from instagram_action_client import InstagramActionClient, InstagramSessionError
from state_store import load_state, save_state, outward_action_lock, pause_requested
from .common import inside_hours

logger = logging.getLogger(__name__)

def parse_dt(v):
    try: return datetime.fromisoformat(str(v)) if v else None
    except Exception: return None

def final_content(data):
    # HEB override: "Final Message" already holds the chosen approved Hebrew-library text as-is.
    if str(data.get("Language Override") or "").strip().upper()=="HEB": return data.get("Final Message") or ""
    english=data.get("Final Message") or ""; translated=data.get("Translated Message") or ""
    return english+("\n\n"+translated if translated else "")

def run(limit=1, source=None):
    if source is None:
        source = get_salespeople()[0]
    tag = source["id"]
    if not inside_hours():
        logger.info("message_sender[%s]: outside operating hours, skipping", tag)
        return {"sent":0,"source":tag}
    store=ExcelStore(source["workbook"])
    if shared_today_count("Message Sent At", get_salespeople())>=settings.daily_message_limit:
        logger.info("message_sender[%s]: daily message limit (%d) reached", tag, settings.daily_message_limit)
        return {"sent":0,"limit_reached":True,"source":tag}
    not_before=parse_dt(load_state().get("NEXT_MESSAGE_NOT_BEFORE")); now=datetime.now(timezone.utc)
    if not_before and now<not_before:
        logger.info("message_sender[%s]: waiting for random delay window, next message not before %s", tag, not_before.isoformat())
        return {"sent":0,"next_message_not_before":not_before.isoformat(),"source":tag}
    ig=InstagramActionClient(); sent=0
    for lead in store.priority_rows():
        if pause_requested(): break
        if sent>=limit: break
        data=store.get_row(lead["row"])
        if data.get("Message Status")=="SENDING":
            text=final_content(data)
            try:
                if text and ig.verify_message_sent(lead["username"],text,since=data.get("Last Checked At") or data.get("Message Sent At")):
                    now=datetime.now(timezone.utc); store.update(lead["row"], **{"Automation Status":"MESSAGE_SENT","Message Status":"SENT","Message Sent At":data.get("Message Sent At") or now.isoformat(),"Seen Status":"UNKNOWN","Last Error":"","Last Automation Action":"MESSAGE_RECOVERED_VERIFIED"}); continue
            except InstagramSessionError:
                raise
            except Exception: pass
            store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"UNCERTAIN","Last Automation Action":"MESSAGE_UNCERTAIN_AFTER_RESTART","Automation Notes":"Automatic resend disabled to prevent duplicate DM."}); continue
        if data.get("Message Status")!="READY": continue
        # Follow-Back Timing V2 gate: a lead carrying a Message Trigger (FOLLOW_BACK or
        # Y_TIMEOUT_PROACTIVE, set by follow_back_monitor.py) isn't actually eligible to send
        # until its own Message Eligible At -- this stays READY_TO_MESSAGE/Message Status=READY
        # in the meantime, just not sent yet. A row with no Message Trigger at all (predates this
        # feature, or reached READY_TO_MESSAGE some other way) has nothing to gate on and is
        # treated as immediately eligible, same as always.
        if data.get("Message Trigger"):
            eligible_at=parse_dt(data.get("Message Eligible At"))
            if eligible_at and now<eligible_at: continue
        if data.get("Filtered Reason") or store.do_not_refollow(lead["row"]): continue
        if store.apply_hard_filter_if_needed(lead["row"],allow_blank=True): continue
        if not store.validate_automation_username(lead["row"],lead["username"]): continue
        with outward_action_lock() as locked:
            if not locked: return {"sent":sent,"action_lock_busy":True,"source":tag}
            try:
                # Final inbox check protects against a conversation that appeared after preparation.
                has_prev=ig.has_previous_conversation(lead["username"])
            except InstagramSessionError:
                raise
            except Exception as exc:
                logger.error("Row %s (@%s): unexpected error checking inbox before send: %s", lead["row"], lead["username"], exc, exc_info=True)
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Last Error":str(exc),"Last Automation Action":"MESSAGE_PRECHECK_ERROR"}); continue
            if has_prev:
                store.update(lead["row"], **{"Automation Status":"FILTERED","Filtered Reason":"PREVIOUS_CONVERSATION","Do Not ReFollow":"YES","Message Status":"NOT_READY","Last Automation Action":"PREVIOUS_CONVERSATION_FILTER"}); continue
            text=final_content(data)
            if not text:
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"FAILED","Filtered Reason":"EMPTY_FINAL_MESSAGE","Last Automation Action":"MESSAGE_BLOCKED"}); continue
            attempt_at=datetime.now(timezone.utc)
            store.update(lead["row"], **{"Message Status":"SENDING","Last Checked At":attempt_at.isoformat(),"Last Automation Action":"MESSAGE_SENDING"})
            result=None
            send_raised=False
            try:
                result=ig.send_message(lead["username"],text)
                returned_ok=bool(result and (getattr(result,"id",None) or getattr(result,"pk",None)))
                time.sleep(1)
                verified=ig.verify_message_sent(lead["username"],text,since=attempt_at-timedelta(minutes=1))
                if not (returned_ok and verified): raise RuntimeError("DM was not independently verified after send")
                sent_at=datetime.now(timezone.utc)
                store.update(lead["row"], **{"Automation Status":"MESSAGE_SENT","Message Status":"SENT","Message Sent At":sent_at.isoformat(),"Seen Status":"UNKNOWN","Retry Count":0,"Last Error":"","Last Automation Action":"MESSAGE_SENT_VERIFIED"})
                delay=random.uniform(settings.message_min_delay_hours,settings.message_max_delay_hours)
                logger.info("Row %s (@%s): DM sent and verified, next DM delayed %.1fh", lead["row"], lead["username"], delay)
                save_state(NEXT_MESSAGE_NOT_BEFORE=(sent_at+timedelta(hours=delay)).isoformat()); sent+=1
            except InstagramSessionError:
                # Session itself is broken -- don't mark this DM as merely "uncertain"; pause
                # everything, since every other pending send would fail the same way.
                raise
            except Exception as exc:
                logger.error("Row %s (@%s): DM send failed: %s", lead["row"], lead["username"], exc, exc_info=True)
                try:
                    time.sleep(1)
                    verified=ig.verify_message_sent(lead["username"],text,since=attempt_at-timedelta(minutes=1))
                    thread_exists=ig.find_thread_for_username(lead["username"]) is not None
                except Exception:
                    verified=False; thread_exists=True
                if verified:
                    sent_at=datetime.now(timezone.utc); store.update(lead["row"], **{"Automation Status":"MESSAGE_SENT","Message Status":"SENT","Message Sent At":sent_at.isoformat(),"Seen Status":"UNKNOWN","Last Error":"","Last Automation Action":"MESSAGE_SENT_VERIFIED_AFTER_ERROR"}); sent+=1
                else:
                    current=store.get_row(lead["row"]); retries=int(current.get("Retry Count") or 0)
                    # A single automatic retry is allowed only when the send did not return a
                    # DirectMessage AND no conversation/thread was created. Otherwise outcome is uncertain.
                    if result is None and not thread_exists and retries<1:
                        store.update(lead["row"], **{"Automation Status":"READY_TO_MESSAGE","Message Status":"READY","Retry Count":retries+1,"Last Error":str(exc),"Last Automation Action":"MESSAGE_RETRY_QUEUED"})
                        save_state(NEXT_MESSAGE_NOT_BEFORE=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat())
                    elif result is None and not thread_exists:
                        store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"FAILED","Filtered Reason":"FAILED_MESSAGE","Last Error":str(exc),"Last Automation Action":"MESSAGE_FAILED"})
                    else:
                        store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"UNCERTAIN","Last Error":str(exc),"Automation Notes":"DM outcome could not be verified. Automatic resend disabled.","Last Automation Action":"MESSAGE_UNCERTAIN"})
    logger.info("message_sender[%s]: sent %d DM(s) this cycle", tag, sent)
    return {"sent":sent,"source":tag}
