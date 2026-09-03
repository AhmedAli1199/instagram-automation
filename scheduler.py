import time
import logging
from pathlib import Path
from logging_setup import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from config import settings, get_salespeople
from excel_store import ExcelStore, ExcelUnavailableError
from instagram_action_client import InstagramSessionError
from sources import rotated_sources, advance_rotation
from state_store import (
    is_running, pause_requested, mark_paused_safe, set_error, application_lock,
    load_state, load_global_state, load_source_state, save_source_state,
)
from workbook_prep import resolve_and_prepare
from workers import profile_processor, follow_worker, follow_status_worker, follow_back_monitor, message_prepare_worker, message_sender, message_status_worker, cleanup_worker

BASE_DIR = Path(__file__).resolve().parent
WORKERS=[profile_processor,follow_worker,follow_status_worker,follow_back_monitor,message_prepare_worker,message_sender,message_status_worker,cleanup_worker]


def _ensure_source_ready(source):
    """Same per-source safety net as main.py: a workbook that can't be opened this cycle (locked,
    momentarily unavailable) is logged and skipped, not fatal to the other sources or the cycle.
    Also handles CSV auto-conversion and extension-optional filename matching (see
    workbook_prep.py) -- this used to be missing here, unlike dashboard_server.py."""
    env_key = f"{source['id']}_WORKBOOK" if settings.multi_salesperson else "WORKBOOK_PATH"
    resolved = resolve_and_prepare(source["workbook"], BASE_DIR, env_key=env_key,
                                    label=f"{source['name']}'s workbook")
    if resolved is None:
        return False
    # Downstream workers build their own ExcelStore(source["workbook"]) -- keep that key pointed
    # at whatever file actually exists this cycle.
    source["workbook"] = resolved.name
    return True


def cycle():
    if pause_requested():
        logger.info("Pause requested; marking PAUSED_SAFE")
        mark_paused_safe(); return
    if not is_running():
        return
    sources = rotated_sources()
    for source in sources:
        if pause_requested():
            logger.info("Pause requested mid-cycle; marking PAUSED_SAFE")
            mark_paused_safe(); return
        if not is_running(): return
        if not _ensure_source_ready(source):
            continue
        for worker in WORKERS:
            if pause_requested():
                logger.info("Pause requested mid-cycle; marking PAUSED_SAFE")
                mark_paused_safe(); return
            if not is_running(): return
            name = worker.__name__.rsplit(".", 1)[-1]
            logger.info("--- worker: %s [%s] ---", name, source["id"])
            try:
                result = worker.run(source=source)
            except InstagramSessionError:
                # The Instagram session is global -- every source shares it, so a broken login
                # must pause the whole scheduler, not just this one source (spec: failure
                # isolation section -- "a broken Instagram session is global").
                raise
            except ExcelUnavailableError as exc:
                # This one workbook is the problem, not the account -- skip the rest of THIS
                # source's workers for this cycle and move on to the next source, rather than
                # pausing everyone over one locked/unavailable file (spec: "a locked Excel file
                # should temporarily disable only writes/actions that require that workbook").
                logger.warning("[%s] workbook unavailable mid-cycle, skipping rest of this source this cycle: %s", source["id"], exc)
                break
            except Exception as exc:
                # An unexpected failure isolated to this one source's worker must not take down
                # the other sources still queued in this same cycle.
                logger.error("[%s] worker %s failed: %s", source["id"], name, exc, exc_info=True)
                continue
            logger.info("%s[%s] result: %s", name, source["id"], result)
    advance_rotation(sources)
    if pause_requested(): mark_paused_safe()


if __name__=="__main__":
    with application_lock() as locked:
        if not locked:
            raise SystemExit("Another scheduler/process is already running.")
        try:
            for source in get_salespeople():
                if not _ensure_source_ready(source):
                    raise ExcelUnavailableError(
                        f"{source['name']}'s workbook ({source['workbook']}) could not be found/prepared at startup."
                    )
                store = ExcelStore(BASE_DIR / source["workbook"])
                state = load_source_state(source["state"])
                if int(state.get("LAST_KNOWN_EXCEL_ROW") or 1)<=1 and int(state.get("POINTER_INDEX") or 0)==0:
                    save_source_state(source["state"], LAST_KNOWN_EXCEL_ROW=store.max_row())
        except ExcelUnavailableError as exc:
            logger.error("Excel unavailable at startup: %s", exc, exc_info=True)
            set_error(exc); raise SystemExit(str(exc))
        logger.info("Scheduler started. Cycle interval: %ds. Sources: %s", settings.cycle_seconds,
                    ", ".join(s["id"] for s in get_salespeople()))
        while True:
            try:
                logger.info("=== cycle start (mode=%s) ===", load_global_state().get("MODE"))
                cycle()
                logger.info("=== cycle end ===")
            except (ExcelUnavailableError,InstagramSessionError) as exc:
                logger.error("Pausing automation due to error: %s", exc, exc_info=True)
                set_error(exc)
            except Exception as exc:
                # Unknown worker failure pauses outward automation rather than killing the process.
                logger.error("Unhandled scheduler error: %s", exc, exc_info=True)
                set_error(f"Unhandled scheduler error: {exc}")
            logger.info("Sleeping %ds until next cycle", settings.cycle_seconds)
            time.sleep(settings.cycle_seconds)
