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
    # today_count is checked/updated per-row below, not as one blanket cutoff -- a real follow-back
    # (Message Trigger=FOLLOW_BACK) is allowed a small overflow past daily_message_limit
    # (FOLLOW_BACK_PRIORITY_DM_OVERFLOW), while Y_TIMEOUT_PROACTIVE and everything else still stops
    # exactly at daily_message_limit as before. Incremented locally after each real send in this
    # run rather than re-querying Excel every time.
    today_count=shared_today_count("Message Sent At", get_salespeople())
    def _eligible(trigger):
        if trigger=="FOLLOW_BACK":
            return today_count<settings.daily_message_limit+settings.follow_back_priority_dm_overflow
        return today_count<settings.daily_message_limit
    # Deliberately NOT an early return even when both the normal cap and the overflow are fully
    # exhausted: a FOLLOW_BACK lead reaching that state still needs to be visited once, by the
    # per-row loop below, to be parked in PRIORITY_WAITING for first-in-line treatment next time
    # capacity opens. An early exit here would skip that entirely and the lead would just sit as
    # READY_TO_MESSAGE indistinguishable from every other queued lead, silently defeating the
    # whole point of the priority behavior the moment the account is fully capped.
    if not _eligible(None) and not _eligible("FOLLOW_BACK"):
        logger.info("message_sender[%s]: daily message limit (%d, +%d follow-back overflow) fully reached; still checking for FOLLOW_BACK leads to prioritize", tag, settings.daily_message_limit, settings.follow_back_priority_dm_overflow)
    not_before=parse_dt(load_state().get("NEXT_MESSAGE_NOT_BEFORE")); now=datetime.now(timezone.utc)
    if not_before and now<not_before:
        logger.info("message_sender[%s]: waiting for random delay window, next message not before %s", tag, not_before.isoformat())
        return {"sent":0,"next_message_not_before":not_before.isoformat(),"source":tag}
    ig=InstagramActionClient(); sent=0
    # Two passes over the same priority-sorted list: PRIORITY_WAITING leads first (a real
    # follow-back that couldn't fit even in the overflow last time it was checked -- gets first
    # crack at capacity the moment any opens up), then everyone else in normal order. A lead never
    # appears in both passes since Automation Status is exactly one value at a time.
    for priority_pass in (True, False):
      if pause_requested() or sent>=limit: break  # don't reload/re-scan priority_rows() for a second pass we already know is done
      for lead in store.priority_rows():
        if pause_requested(): break
        if sent>=limit: break
        data=store.get_row(lead["row"])
        is_priority_waiting=data.get("Automation Status")=="PRIORITY_WAITING"
        if is_priority_waiting != priority_pass: continue
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
        trigger=data.get("Message Trigger")
        if trigger:
            eligible_at=parse_dt(data.get("Message Eligible At"))
            if eligible_at and now<eligible_at: continue
        if not _eligible(trigger):
            # Only a real follow-back gets parked in PRIORITY_WAITING for first-in-line treatment
            # next time capacity opens -- everything else (Y_TIMEOUT_PROACTIVE, no trigger) just
            # stays READY_TO_MESSAGE/READY and is retried in normal priority order next cycle,
            # exactly like today.
            if trigger=="FOLLOW_BACK" and not is_priority_waiting:
                store.update(lead["row"], **{"Automation Status":"PRIORITY_WAITING","Last Automation Action":"FOLLOW_BACK_DM_OVERFLOW_FULL"})
            continue
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
            # Final, single check immediately before send: are we still following this lead? One
            # extra relationship() call, in the same already-held lock, right at the point it
            # actually matters -- no extra API traffic beyond this. If we're not (unfollowed,
            # blocked, or lost some other way since the follow was accepted), do NOT send to a
            # non-followed account and do NOT immediately re-follow here -- an immediate refollow
            # would itself be a follow/unfollow/refollow churn pattern, its own detection signal.
            # Instead the lead is routed back through the NORMAL follow pipeline (follow_worker.py
            # picks up any PROCESSING row with a blank Follow Status) for a fresh attempt on its
            # own natural pace and daily cap, reusing already-safe machinery rather than a bespoke
            # one-off action. Retry Count caps this at FOLLOW_LOST_MAX_RETRIES before giving up to
            # MANUAL_REVIEW, so a lead that keeps oscillating can't loop forever.
            try:
                still_following=bool(ig.relationship(lead["username"]).get("following"))
            except InstagramSessionError:
                raise
            except Exception as exc:
                logger.error("Row %s (@%s): unexpected error on final follow-check before send: %s", lead["row"], lead["username"], exc, exc_info=True)
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Last Error":str(exc),"Last Automation Action":"MESSAGE_PRECHECK_ERROR"}); continue
            if not still_following:
                # Dedicated field, NOT the shared "Retry Count" -- that field is reset to 0 by
                # unrelated events (a successful follow, a successful send), so reusing it here
                # would silently reset or misread this specific counter depending on what else
                # happened to the lead recently.
                lost_count=int(data.get("Follow Lost Count") or 0)+1
                if lost_count>settings.follow_lost_max_retries:
                    logger.warning("Row %s (@%s): follow lost before DM %d time(s), giving up -- manual review", lead["row"], lead["username"], lost_count)
                    store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"NOT_READY","Follow Lost Count":lost_count,"Automation Notes":f"Follow lost before DM {lost_count} times (max {settings.follow_lost_max_retries}); needs manual review.","Last Automation Action":"FOLLOW_LOST_BEFORE_DM"})
                else:
                    logger.warning("Row %s (@%s): follow lost before DM (attempt %d/%d) -- routed back to follow pipeline for a fresh attempt", lead["row"], lead["username"], lost_count, settings.follow_lost_max_retries)
                    store.update(lead["row"], **{"Automation Status":"PROCESSING","Follow Status":"","Message Status":"NOT_READY","Message Trigger":"","Message Eligible At":"","Follow Back Status":"","Follow Lost Count":lost_count,"Last Automation Action":"FOLLOW_LOST_BEFORE_DM"})
                continue
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
                save_state(NEXT_MESSAGE_NOT_BEFORE=(sent_at+timedelta(hours=delay)).isoformat()); sent+=1; today_count+=1
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
                    sent_at=datetime.now(timezone.utc); store.update(lead["row"], **{"Automation Status":"MESSAGE_SENT","Message Status":"SENT","Message Sent At":sent_at.isoformat(),"Seen Status":"UNKNOWN","Last Error":"","Last Automation Action":"MESSAGE_SENT_VERIFIED_AFTER_ERROR"}); sent+=1; today_count+=1
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
