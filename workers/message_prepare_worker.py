import logging
from excel_store import ExcelStore
from instagram_action_client import InstagramActionClient, InstagramProfileNotFound, InstagramSessionError
from templates import load_templates, pick_random_filled
from language_service import detect_language, translate, display_name
from name_classifier import looks_like_person_name, clean_first_name
from config import settings, get_salespeople
from state_store import pause_requested

logger = logging.getLogger(__name__)

def _choose_message(templates, full_name, city):
    """Random PER pick if the name looks like a real person's name and personalization data
    is available; otherwise a random GEN pick. Never sends "Hey {name}" to an obvious
    brand/business account name."""
    usable_name = clean_first_name(full_name) if looks_like_person_name(full_name) else None
    if usable_name:
        chosen, final = pick_random_filled(templates, "PER", first_name=usable_name, city=city, location=city)
        if chosen:
            return chosen, final, True
    chosen, final = pick_random_filled(templates, "GEN")
    return chosen, final, False

def run(limit=None, source=None):
    if source is None:
        source = get_salespeople()[0]
    tag = source["id"]
    store=ExcelStore(source["workbook"]); current=store.message_buffer_count(); capacity=max(0,settings.message_ready_buffer-current)
    if capacity<=0:
        logger.info("message_prepare_worker[%s]: buffer full (%d/%d), skipping", tag, current, settings.message_ready_buffer)
        return {"prepared":0,"buffer_full":True,"source":tag}
    if limit is not None: capacity=min(capacity,limit)
    ig=InstagramActionClient()
    templates_en=load_templates(settings.messages_file)
    templates_he=load_templates(settings.messages_file_he)
    prepared=0
    for lead in store.priority_rows():
        if pause_requested(): break
        if prepared>=capacity: break
        data=store.get_row(lead["row"])
        if data.get("Automation Status")!="READY_TO_MESSAGE": continue
        if str(data.get("Message Status") or "").upper() in {"READY","SCHEDULED","SENDING","SENT","UNCERTAIN"}: continue
        if data.get("Filtered Reason") or store.do_not_refollow(lead["row"]): continue
        if store.apply_hard_filter_if_needed(lead["row"],allow_blank=True): continue
        if not store.validate_automation_username(lead["row"],lead["username"]): continue
        try:
            if ig.has_previous_conversation(lead["username"]):
                logger.info("Row %s (@%s): previous conversation exists, filtering", lead["row"], lead["username"])
                store.update(lead["row"], **{"Automation Status":"FILTERED","Filtered Reason":"PREVIOUS_CONVERSATION","Do Not ReFollow":"YES","Message Status":"NOT_READY","Last Automation Action":"PREVIOUS_CONVERSATION_FILTER"}); continue
            p=ig.get_profile(lead["username"])
        except InstagramProfileNotFound:
            logger.warning("Row %s (@%s): username no longer found, filtering", lead["row"], lead["username"])
            store.update(lead["row"], **{"Automation Status":"FILTERED","Filtered Reason":"USERNAME_NOT_FOUND","Do Not ReFollow":"YES","Last Automation Action":"PROFILE_NOT_FOUND"}); continue
        except InstagramSessionError:
            raise
        except Exception as exc:
            logger.error("Row %s (@%s): unexpected error during message prep: %s", lead["row"], lead["username"], exc, exc_info=True)
            store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"FAILED","Filtered Reason":"MESSAGE_PREPARE_ERROR","Last Error":str(exc),"Last Automation Action":"MESSAGE_PREPARE_ERROR"}); continue
        full=(p.get("full_name") or data.get("Full Name") or "").strip(); city=p.get("city")

        override=str(data.get("Language Override") or "").strip().upper(); translated=""; detected=""; translation_language=""

        if override=="HEB":
            # Approved Hebrew library: a real pre-written, pre-approved message, never a live
            # machine translation of the English one.
            chosen, final, personalized = _choose_message(templates_he, full, city)
            if not chosen or not final:
                logger.error("Row %s (@%s): no usable Hebrew template", lead["row"], lead["username"])
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"FAILED","Filtered Reason":"MESSAGE_PREPARE_FAILED","Last Error":"No usable Hebrew template"}); continue
            detected="HEB_OVERRIDE"; translation_language="Hebrew"; send_content=final
        else:
            chosen, final, personalized = _choose_message(templates_en, full, city)
            if not chosen or not final:
                logger.error("Row %s (@%s): no usable message template", lead["row"], lead["username"])
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"FAILED","Filtered Reason":"MESSAGE_PREPARE_FAILED","Last Error":"No usable message template"}); continue
            send_content=final
            try:
                code=detect_language(p.get("biography") or "")
                if code:
                    detected=display_name(code); translation_language=display_name(code)
                    try:
                        translated=translate(final,code); send_content=final+"\n\n"+translated
                    except Exception:
                        translated=""; translation_language=""; send_content=final
            except Exception as exc:
                logger.error("Row %s (@%s): translation failed: %s", lead["row"], lead["username"], exc, exc_info=True)
                store.update(lead["row"], **{"Automation Status":"MANUAL_REVIEW","Message Status":"FAILED","Filtered Reason":"TRANSLATION_FAILED","Last Error":str(exc)}); continue

        logger.info("Row %s (@%s): message prepared (template=%s type=%s personalized=%s)", lead["row"], lead["username"], chosen.template_id, chosen.kind, personalized)
        store.update(lead["row"], **{"Message Status":"READY","Template Used":chosen.template_id,"Template Type":chosen.kind,"Personalization Used":"YES" if personalized else "NO","Final Message":final,"Detected Language":detected,"Translation Language":translation_language,"Translated Message":translated,"Automation Notes":send_content,"Last Automation Action":"MESSAGE_PREPARED"}); prepared+=1
    logger.info("message_prepare_worker[%s]: prepared %d message(s) this cycle", tag, prepared)
    return {"prepared":prepared,"buffer_count_after":current+prepared,"source":tag}
