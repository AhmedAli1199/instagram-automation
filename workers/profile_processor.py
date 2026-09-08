import logging
import random, time
from datetime import datetime, timezone
from config import settings, get_salespeople
from excel_store import ExcelStore, shared_today_count
from instagram_action_client import InstagramActionClient, InstagramProfileNotFound, InstagramSessionError
from state_store import pause_requested, pointer_index, set_pointer, last_known_excel_row, set_last_known_excel_row

logger = logging.getLogger(__name__)

def _human_pause():
    # Follows and DMs already have hour-scale random delays; profile/relationship lookups had
    # none at all, so a batch of 5 could fire in under two minutes -- itself an automation
    # signature independent of the follow/DM pacing. Space read-only checks out too.
    delay = random.uniform(settings.profile_check_min_delay_seconds, settings.profile_check_max_delay_seconds)
    logger.debug("Pausing %.1fs before next profile check", delay)
    time.sleep(delay)

def _capacity_available(store):
    # Daily follow/message caps belong to the shared Instagram account -- summed across every
    # source, not just this one's own workbook (multi-salesperson: never multiply account
    # limits). The ready-to-send buffer stays per-source: it's about how much this source has
    # queued up, not an outward action, so there's no reason to share it.
    messages_full = shared_today_count("Message Sent At", get_salespeople()) >= settings.daily_message_limit
    follows_full = shared_today_count("Follow Requested At", get_salespeople()) >= settings.daily_follow_limit
    buffer_full = store.message_buffer_count() >= settings.message_ready_buffer
    return not (follows_full and (messages_full or buffer_full))

def _process_one(store, ig, lead, dupes):
    row=lead["row"]; data=store.get_row(row); status=str(data.get("Automation Status") or "").strip().upper()
    if status not in ("","NEW","PROCESSING"): return "SKIPPED"
    reason=store.apply_hard_filter_if_needed(row,allow_blank=status in ("","NEW"))
    if reason:
        logger.info("Row %s (@%s): filtered locally (%s)", row, lead["username"], reason)
        return "FILTERED"
    if not lead["username"]:
        store.update(row, **{"Automation Status":"MANUAL_REVIEW","Filtered Reason":"MISSING_USERNAME","Last Automation Action":"LOCAL_FILTER"}); return "MANUAL"
    if not lead["user_id"]:
        store.update(row, **{"Automation Status":"MANUAL_REVIEW","Filtered Reason":"MISSING_USER_ID","Last Automation Action":"LOCAL_FILTER"}); return "MANUAL"
    if lead["user_id"] in dupes:
        store.update(row, **{"Automation Status":"MANUAL_REVIEW","Filtered Reason":"DUPLICATE_USER_ID","Do Not ReFollow":"YES","Last Automation Action":"LOCAL_FILTER"}); return "MANUAL"
    if not store.validate_automation_username(row,lead["username"]): return "MANUAL"
    if store.do_not_refollow(row): return "SKIPPED"
    store.update(row, **{"Automation Status":"PROCESSING"})
    logger.info("Row %s (@%s): checking Instagram profile + relationship", row, lead["username"])
    try:
        profile=ig.get_profile(lead["username"])
    except InstagramProfileNotFound:
        logger.warning("Row %s (@%s): username not found on Instagram, filtering", row, lead["username"])
        store.update(row, **{"Automation Status":"FILTERED","Filtered Reason":"USERNAME_NOT_FOUND","Do Not ReFollow":"YES","Last Automation Action":"PROFILE_NOT_FOUND"}); return "FILTERED"
    except InstagramSessionError:
        raise  # login/session itself is broken -- a real reason to pause the whole scheduler
    except Exception as exc:
        # Any other unexpected failure on this one lead must not take down the whole cycle --
        # quarantine just this row and keep going, rather than letting it bubble up and pause
        # automation for every other lead too (and get retried against this same row forever).
        logger.error("Row %s (@%s): unexpected error during profile check: %s", row, lead["username"], exc, exc_info=True)
        store.update(row, **{"Automation Status":"MANUAL_REVIEW","Filtered Reason":"PROFILE_CHECK_ERROR","Last Error":str(exc),"Last Automation Action":"PROFILE_CHECK_ERROR"}); return "MANUAL"
    try:
        # Checked before any follow decision: no point sending a follow request to someone we
        # already have a conversation with.
        if ig.has_previous_conversation(lead["username"]):
            logger.info("Row %s (@%s): previous conversation exists, filtering before follow", row, lead["username"])
            store.update(row, **{"Automation Status":"FILTERED","Filtered Reason":"PREVIOUS_CONVERSATION","Do Not ReFollow":"YES","Last Automation Action":"PREVIOUS_CONVERSATION_FILTER"}); return "FILTERED"
        relationship=ig.relationship(lead["username"])
    except InstagramSessionError:
        raise
    except Exception as exc:
        logger.error("Row %s (@%s): unexpected error during relationship check: %s", row, lead["username"], exc, exc_info=True)
        store.update(row, **{"Automation Status":"MANUAL_REVIEW","Filtered Reason":"PROFILE_CHECK_ERROR","Last Error":str(exc),"Last Automation Action":"PROFILE_CHECK_ERROR"}); return "MANUAL"
    private=bool(profile.get("is_private")); following=bool(relationship.get("following")); outgoing=bool(relationship.get("outgoing_request"))
    fields={"Profile Type":"PRIVATE" if private else "PUBLIC","Already Following":"YES" if following else "NO","Last Checked At":datetime.now(timezone.utc).isoformat(),"Last Automation Action":"PROFILE_CHECK"}
    if following: fields.update({"Automation Status":"READY_TO_MESSAGE","Follow Status":"ACCEPTED"})
    elif outgoing: fields.update({"Automation Status":"WAITING_FOLLOW_APPROVAL","Follow Status":"REQUESTED"})
    else:
        # Corrected rule (per client flowchart): follow every new lead first, public or private.
        # Public follows are auto-accepted instantly by Instagram, so follow_worker will move
        # public leads straight to READY_TO_MESSAGE right after following; private leads wait
        # for approval in WAITING_FOLLOW_APPROVAL as before.
        fields.update({"Automation Status":"PROCESSING","Follow Status":""})
    logger.info("Row %s (@%s): private=%s following=%s outgoing_request=%s -> %s", row, lead["username"], private, following, outgoing, fields["Automation Status"])
    store.update(row, **fields); return "PROCESSED"

def run(limit=None, source=None):
    # `source` identifies which salesperson's workbook/state to use ({"id","name","workbook",
    # "state"}). In single-workbook mode (the default) this defaults to the one configured
    # source, which resolves to the exact same workbook_path/state_file this project has always
    # used -- so calling run() with no source, as every pre-multi-salesperson caller still does,
    # behaves exactly as before. Each source gets its own full profile_batch_limit allowance per
    # cycle -- deliberately not shared/divided, per how this was scoped.
    if source is None:
        source = get_salespeople()[0]
    store=ExcelStore(source["workbook"]); state_path=source["state"]
    limit=limit or settings.profile_batch_limit
    tag=source["id"]
    if not _capacity_available(store):
        # "this cycle" here means only profile_processor's OWN intake for this one worker call --
        # every other worker (follow-back monitoring, reply/seen checks, 7-day unfollow cleanup,
        # dashboard reads) still runs this same cycle regardless. Worded explicitly below after
        # this exact phrase was read as "the whole scheduler stopped."
        logger.info("profile_processor[%s]: daily/buffer capacity reached -- pausing NEW profile intake only; follow-back monitoring, reply/seen checks, and 7-day unfollow cleanup are unaffected and continue normally", tag)
        return {"processed":0,"capacity_satisfied":True,"source":tag}
    dupes=store.duplicate_user_ids(); ig=InstagramActionClient(); processed=0

    # First process rows appended after the last known physical Excel row. This avoids
    # losing new rows when their H/G values would sort before the saved priority pointer.
    known=last_known_excel_row(state_path)
    for lead in store.physical_rows_after(known):
        if pause_requested(): break
        if processed>=limit or not _capacity_available(store): break
        if not lead["username"] and not lead["user_id"]:
            set_last_known_excel_row(lead["row"], state_path); continue
        if processed>0: _human_pause()
        _process_one(store,ig,lead,dupes); processed+=1; set_last_known_excel_row(lead["row"], state_path)

    if processed>=limit or not _capacity_available(store):
        logger.info("profile_processor[%s]: processed %d new rows this cycle", tag, processed)
        return {"processed":processed,"pointer_index":pointer_index(state_path),"source":tag}

    rows=store.priority_rows(); i=min(pointer_index(state_path),len(rows))
    while i<len(rows) and processed<limit and _capacity_available(store):
        if pause_requested(): break
        if processed>0: _human_pause()
        lead=rows[i]; _process_one(store,ig,lead,dupes); processed+=1; i+=1
        next_uid=rows[i]["user_id"] if i<len(rows) else ""; next_row=rows[i]["row"] if i<len(rows) else ""
        set_pointer(i,next_uid,next_row,state_path=state_path)
    logger.info("profile_processor[%s]: processed %d rows this cycle (pointer now %d)", tag, processed, i)
    return {"processed":processed,"pointer_index":i,"source":tag}
