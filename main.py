import argparse
import logging
from pathlib import Path
from logging_setup import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

from config import get_salespeople, settings
from excel_store import ExcelStore, ExcelUnavailableError
from instagram_action_client import InstagramSessionError
from sources import rotated_sources, advance_rotation
from state_store import (
    load_state, load_global_state, load_source_state, resume, request_pause, mark_paused_safe,
    rewind, skip, set_pointer, save_state, save_source_state, application_lock, is_running,
)
from workbook_prep import resolve_and_prepare
from workers import profile_processor, follow_worker, follow_status_worker, follow_back_monitor, message_prepare_worker, message_sender, message_status_worker, cleanup_worker

BASE_DIR = Path(__file__).resolve().parent

WORKER_LIST = [
    ("profile", profile_processor), ("follow", follow_worker), ("follow status", follow_status_worker),
    ("follow back", follow_back_monitor),
    ("message prepare", message_prepare_worker), ("message send", message_sender),
    ("message status", message_status_worker), ("cleanup", cleanup_worker),
]


def _ensure_source_ready(source):
    """Prepares one source's workbook (columns + high-water mark) and reports whether it's
    usable this run. A source whose workbook can't be opened (locked, missing, corrupted) is
    logged clearly and skipped -- it must not stop the other sources (multi-salesperson spec,
    failure isolation)."""
    try:
        env_key = f"{source['id']}_WORKBOOK" if settings.multi_salesperson else "WORKBOOK_PATH"
        resolved = resolve_and_prepare(source["workbook"], BASE_DIR, env_key=env_key,
                                        label=f"{source['name']}'s workbook")
        if resolved is None:
            return False
        # Downstream workers build their own ExcelStore(source["workbook"]) -- keep that key
        # pointed at whatever file actually exists (extension resolved / csv converted) so every
        # worker call this run opens the same, real file.
        source["workbook"] = resolved.name
        store = ExcelStore(resolved)
        state = load_source_state(source["state"])
        if int(state.get("LAST_KNOWN_EXCEL_ROW") or 1) <= 1 and int(state.get("POINTER_INDEX") or 0) == 0:
            save_source_state(source["state"], LAST_KNOWN_EXCEL_ROW=store.max_row())
        return True
    except ExcelUnavailableError as exc:
        logger.error("[%s] workbook unavailable, skipping this source this run: %s", source["id"], exc)
        print(f"WARNING: {source['name']} ({source['workbook']}) is unavailable -- skipped. {exc}")
        return False


def run_once():
    if not is_running(): raise SystemExit("run-once requires MODE=RUN. Use 'resume' first.")
    with application_lock() as locked:
        if not locked: raise SystemExit("Scheduler is already running; run-once refused.")
        sources = rotated_sources()
        logger.info("=== run-once starting (sources: %s) ===", ", ".join(s["id"] for s in sources))
        for source in sources:
            if not _ensure_source_ready(source):
                continue
            print(f"--- {source['name']} ({source['id']}) ---")
            try:
                for name, worker in WORKER_LIST:
                    logger.info("--- worker: %s [%s] ---", name, source["id"])
                    result = worker.run(source=source)
                    logger.info("%s[%s] result: %s", name, source["id"], result)
                    print(f"  {name}: {result}")
            except InstagramSessionError:
                # Global -- the same shared session would fail identically for every other
                # source too, so this must stop run-once entirely, exactly like an uncaught
                # session error always has (matches single-source behavior unchanged).
                raise
            except ExcelUnavailableError as exc:
                logger.warning("[%s] workbook became unavailable mid-run, skipping remaining steps for this source: %s", source["id"], exc)
                print(f"  WARNING: {source['name']}'s workbook became unavailable -- skipping its remaining steps this run.")
                continue
            except Exception as exc:
                logger.error("[%s] unexpected failure, skipping remaining steps for this source: %s", source["id"], exc, exc_info=True)
                print(f"  ERROR for {source['name']}: {exc} -- skipping its remaining steps this run.")
                continue
        advance_rotation(sources)
        logger.info("=== run-once complete ===")


parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command",required=True)
for cmd in ["init","run-once","pause","resume","status"]: sub.add_parser(cmd)
p=sub.add_parser("rewind"); p.add_argument("amount",type=int,default=1); p.add_argument("--source",default=None,help="Salesperson id (SP1/SP2/SP3), default: first configured source")
p=sub.add_parser("skip"); p.add_argument("amount",type=int,default=1); p.add_argument("--source",default=None)
p=sub.add_parser("set-pointer"); p.add_argument("index",type=int); p.add_argument("--source",default=None)
args=parser.parse_args()

def _resolve_source(source_id):
    sources = get_salespeople()
    if source_id is None:
        return sources[0]
    for s in sources:
        if s["id"] == source_id:
            return s
    raise SystemExit(f"Unknown --source {source_id!r}. Configured sources: {', '.join(s['id'] for s in sources)}")

if args.command=="init":
    for source in get_salespeople():
        env_key = f"{source['id']}_WORKBOOK" if settings.multi_salesperson else "WORKBOOK_PATH"
        resolved = resolve_and_prepare(source["workbook"], BASE_DIR, env_key=env_key,
                                        label=f"{source['name']}'s workbook")
        if resolved is None:
            print(f"SKIPPED {source['name']} -- see warning above.")
            continue
        store = ExcelStore(resolved)
        save_source_state(source["state"], LAST_KNOWN_EXCEL_ROW=store.max_row())
        print(f"initialized {source['name']} ({resolved.name})")
    mark_paused_safe(); print("SAFE TO EDIT EXCEL")
elif args.command=="run-once": run_once()
elif args.command=="pause":
    with application_lock() as locked:
        if locked:
            mark_paused_safe(); print("PAUSED_SAFE")
        else:
            request_pause(); print("pause requested; scheduler will mark PAUSED_SAFE between workers")
elif args.command=="resume": resume(); print("RUN")
elif args.command=="status":
    print("global:", load_global_state())
    for source in get_salespeople():
        print(f"{source['id']} ({source['name']}):", load_source_state(source["state"]))
elif args.command=="rewind": s=_resolve_source(args.source); rewind(args.amount, state_path=s["state"]); print(load_source_state(s["state"]))
elif args.command=="skip": s=_resolve_source(args.source); skip(args.amount, state_path=s["state"]); print(load_source_state(s["state"]))
elif args.command=="set-pointer": s=_resolve_source(args.source); set_pointer(args.index, state_path=s["state"]); print(load_source_state(s["state"]))
