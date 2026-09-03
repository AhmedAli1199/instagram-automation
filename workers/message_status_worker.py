import logging
from datetime import datetime, timezone
from excel_store import ExcelStore
from instagram_action_client import InstagramActionClient, InstagramSessionError
from config import get_salespeople
from state_store import pause_requested

logger = logging.getLogger(__name__)

def run(source=None):
    if source is None:
        source = get_salespeople()[0]
    tag = source["id"]
    store=ExcelStore(source["workbook"]); ig=InstagramActionClient(); checked=0
    for lead in store.priority_rows():
        if pause_requested(): break
        data=store.get_row(lead["row"])
        if data.get("Message Status")!="SENT": continue
        try:
            snap=ig.conversation_snapshot(lead["username"],since=data.get("Message Sent At"))
        except InstagramSessionError:
            raise
        except Exception as exc:
            logger.error("Row %s (@%s): unexpected error checking message status: %s", lead["row"], lead["username"], exc, exc_info=True)
            store.update(lead["row"], **{"Last Error":str(exc),"Last Automation Action":"MESSAGE_STATUS_CHECK_ERROR"}); continue
        checked+=1; now=datetime.now(timezone.utc).isoformat()
        if snap.get("reply"):
            logger.info("Row %s (@%s): reply detected, stopping automation for manual takeover", lead["row"], lead["username"])
            old_reason=str(data.get("Filtered Reason") or "")
            late=old_reason in {"SEEN_NO_REPLY","UNSEEN_7_DAYS"}
            notes=str(data.get("Automation Notes") or "").strip()
            if late:
                historical=f"Historical filter before late reply: {old_reason}."
                notes=(notes+" | "+historical).strip(" |")
            store.update(lead["row"], **{"Automation Status":"MANUAL_CONVERSATION","Reply Status":"REPLIED_LATE" if late else "REPLIED","Filtered Reason":"" if late else old_reason,"Automation Notes":notes,"Do Not ReFollow":"YES","Last Checked At":now,"Last Automation Action":"LATE_REPLY_DETECTED" if late else "REPLY_DETECTED"}); continue
        seen=snap.get("seen_status") or "UNKNOWN"; fields={"Seen Status":seen,"Last Checked At":now,"Last Automation Action":"MESSAGE_STATUS_CHECK"}
        # Lock the first reliable Seen timestamp; never move it forward later.
        if seen=="SEEN" and snap.get("last_seen_at") and not data.get("Message Seen At"):
            fields["Message Seen At"]=snap.get("last_seen_at")
            logger.info("Row %s (@%s): message seen at %s", lead["row"], lead["username"], snap.get("last_seen_at"))
        store.update(lead["row"], **fields)
    logger.info("message_status_worker[%s]: checked %d sent message(s) this cycle", tag, checked)
    return {"checked":checked,"source":tag}
