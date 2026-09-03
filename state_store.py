from __future__ import annotations
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock, Timeout
from config import settings

logger = logging.getLogger(__name__)

# Account-level state, shared across every source (single-workbook mode has exactly one source,
# so this is functionally "the" state either way). Never anything pointer/row related here --
# that's per-source (see SOURCE_DEFAULT_STATE) because processing SP2 must never move SP1's
# pointer, per the multi-salesperson spec.
GLOBAL_DEFAULT_STATE = {
    "MODE": "PAUSED_SAFE",
    "NEXT_MESSAGE_NOT_BEFORE": "",
    "NEXT_FOLLOW_NOT_BEFORE": "",
    "GLOBAL_ERROR": "",
    "ERROR_AT": "",
    "NEXT_SALESPERSON": "",  # multi-salesperson round-robin fairness; unused/blank in single mode
    "UPDATED_AT": "",
}

# Per-source progress. In single-workbook mode this lives in settings.state_file, exactly the
# one file this project has always used. In multi-salesperson mode each source gets its own.
SOURCE_DEFAULT_STATE = {
    "POINTER_INDEX": "0",
    "POINTER_USER_ID": "",
    "POINTER_ROW": "",
    "LAST_COMPLETED_USER_ID": "",
    "LAST_KNOWN_EXCEL_ROW": "1",
    "UPDATED_AT": "",
}


def _read_kv_file(path: Path, defaults: dict):
    if not path.exists():
        return None
    state = dict(defaults)
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key, value = key.strip(), value.strip()
        # A blank value (someone hand-cleared "KEY=" but left the line) means "no value here",
        # same as the key being absent entirely -- fall back to its safe default rather than
        # forcing an empty string, which several fields (MODE especially) treat as a real,
        # different-from-default state rather than "unset".
        if value == "" and key in defaults and defaults[key] != "":
            continue
        state[key] = value
    return state


def _write_kv_file(path: Path, defaults: dict, state: dict):
    state = dict(defaults) | state
    state["UPDATED_AT"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(f"{k}={state.get(k, '')}" for k in defaults) + "\n", encoding="utf-8")
    tmp.replace(path)
    return state


def _global_path():
    return Path(settings.global_state_file)


def _default_source_path():
    return Path(settings.state_file)


def _migrate_global_from_legacy_if_needed():
    """The very first time global_state.txt doesn't exist yet, seed it from whatever is already
    in the legacy single state.txt (if any) instead of starting blank -- so upgrading an existing
    single-workbook install to this version doesn't silently forget it was paused, mid-error, or
    mid-delay-window. This runs at most once; after global_state.txt exists it's never consulted
    again. The legacy file is only read here, never modified or deleted."""
    gpath = _global_path()
    if gpath.exists():
        return
    seed = dict(GLOBAL_DEFAULT_STATE)
    legacy = _default_source_path()
    if legacy.exists():
        combined_defaults = {**GLOBAL_DEFAULT_STATE, **SOURCE_DEFAULT_STATE}
        legacy_state = _read_kv_file(legacy, combined_defaults) or {}
        for key in GLOBAL_DEFAULT_STATE:
            if legacy_state.get(key):
                seed[key] = legacy_state[key]
        logger.info("Seeded %s from existing %s (one-time migration).", gpath, legacy)
    _write_kv_file(gpath, GLOBAL_DEFAULT_STATE, seed)


def load_global_state():
    _migrate_global_from_legacy_if_needed()
    return _read_kv_file(_global_path(), GLOBAL_DEFAULT_STATE) or dict(GLOBAL_DEFAULT_STATE)


def save_global_state(**changes):
    state = load_global_state()
    state.update({k: str(v) for k, v in changes.items()})
    return _write_kv_file(_global_path(), GLOBAL_DEFAULT_STATE, state)


def load_source_state(state_path=None):
    path = Path(state_path) if state_path else _default_source_path()
    state = _read_kv_file(path, SOURCE_DEFAULT_STATE)
    if state is None:
        state = _write_kv_file(path, SOURCE_DEFAULT_STATE, dict(SOURCE_DEFAULT_STATE))
    return state


def save_source_state(state_path=None, **changes):
    path = Path(state_path) if state_path else _default_source_path()
    state = load_source_state(path)
    state.update({k: str(v) for k, v in changes.items()})
    return _write_kv_file(path, SOURCE_DEFAULT_STATE, state)


# ---------------------------------------------------------------------------------------------
# Backward-compatible flat API. Every existing caller (workers, main.py, scheduler.py,
# dashboard_server.py) keeps working unmodified: load_state()/save_state() merge the global and
# default-source layers together exactly like the old single-file state.txt used to hold
# everything in one place. In single-workbook mode the default source *is* settings.state_file,
# so this is not just API-compatible, it's byte-for-byte the same file layout as before.
# ---------------------------------------------------------------------------------------------

def load_state():
    return {**load_global_state(), **load_source_state()}


def save_state(**changes):
    global_changes = {k: v for k, v in changes.items() if k in GLOBAL_DEFAULT_STATE}
    source_changes = {k: v for k, v in changes.items() if k in SOURCE_DEFAULT_STATE}
    unknown = {k: v for k, v in changes.items() if k not in GLOBAL_DEFAULT_STATE and k not in SOURCE_DEFAULT_STATE}
    result = {}
    if global_changes or not source_changes:
        result.update(save_global_state(**global_changes))
    if source_changes:
        result.update(save_source_state(None, **source_changes))
    if unknown:
        result.update(save_global_state(**unknown))
    return result


def mode(): return load_global_state().get("MODE", "PAUSED_SAFE")
def is_running(): return mode() == "RUN"
def pause_requested(): return mode() == "PAUSE_REQUESTED"
def request_pause(): logger.info("Mode -> PAUSE_REQUESTED"); save_global_state(MODE="PAUSE_REQUESTED")
def mark_paused_safe(): logger.info("Mode -> PAUSED_SAFE"); save_global_state(MODE="PAUSED_SAFE")
def set_error(message):
    logger.error("Mode -> PAUSED_ERROR: %s", message)
    save_global_state(MODE="PAUSED_ERROR", GLOBAL_ERROR=str(message), ERROR_AT=datetime.now(timezone.utc).isoformat())
def resume(): logger.info("Mode -> RUN"); save_global_state(MODE="RUN", GLOBAL_ERROR="", ERROR_AT="")


def pointer_index(state_path=None):
    try: return max(0, int(load_source_state(state_path).get("POINTER_INDEX", "0")))
    except Exception: return 0

def set_pointer(index, user_id="", row="", state_path=None):
    save_source_state(state_path, POINTER_INDEX=max(0, int(index)), POINTER_USER_ID=user_id or "", POINTER_ROW=row or "")
def rewind(amount=1, state_path=None): set_pointer(max(0, pointer_index(state_path) - int(amount)), state_path=state_path)
def skip(amount=1, state_path=None): set_pointer(pointer_index(state_path) + int(amount), state_path=state_path)

def last_known_excel_row(state_path=None):
    try: return max(1, int(load_source_state(state_path).get("LAST_KNOWN_EXCEL_ROW", "1")))
    except Exception: return 1

def set_last_known_excel_row(row, state_path=None): save_source_state(state_path, LAST_KNOWN_EXCEL_ROW=max(1, int(row)))

@contextmanager
def _lock(path):
    lock = FileLock(str(path), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()

@contextmanager
def outward_action_lock():
    # Deliberately always the one shared lock file, never per-source: only one Follow, DM,
    # Unfollow, or cancellation may happen at a time across the whole account, regardless of
    # which source it's for (multi-salesperson spec, section 5 & 14).
    with _lock(Path(settings.action_lock_file)) as locked:
        yield locked

@contextmanager
def application_lock():
    with _lock(Path(settings.app_lock_file)) as locked:
        yield locked
