"""Command Center — local dashboard server.

Zero extra installations: everything here is Python standard library
(http.server, json, subprocess, threading) plus openpyxl, which the
automation already depends on. Run it with:

    python dashboard_server.py

then open http://localhost:8765 in a browser. It's a thin face on top of
the exact same main.py / scheduler.py / state_store.py logic you already
use from the command line -- it doesn't reimplement any automation logic,
it just calls it and shows you what's in the Excel file and the log.
"""
import json
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from config import settings, get_salespeople
from env_utils import write_env_var as _write_env_var
from excel_store import ExcelStore, ExcelUnavailableError
from workbook_prep import csv_to_xlsx, resolve_and_prepare
from state_store import (
    load_state, load_global_state, load_source_state, resume, request_pause, mark_paused_safe,
    rewind, skip, set_pointer, application_lock,
)

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
LOG_FILE = BASE_DIR / "logs" / "automation.log"
PORT = int(__import__("os").environ.get("DASHBOARD_PORT", "8765"))
PY = sys.executable

# Resolved at startup by ensure_workbook_ready() -- source id -> the actual workbook path this
# running process reads/writes for that source (handles the CSV-was-just-converted-to-xlsx
# rename). Falls back to the configured path for any source that isn't resolved yet/failed prep.
RESOLVED_WORKBOOKS = {}


def _source_by_id(source_id):
    sources = get_salespeople()
    if not source_id:
        return sources[0]
    return next((s for s in sources if s["id"] == source_id), sources[0])


def workbook_path(source_id=None):
    source = _source_by_id(source_id)
    return RESOLVED_WORKBOOKS.get(source["id"]) or (BASE_DIR / source["workbook"])

# Columns pulled from the workbook for every row shown in the dashboard.
STAT_COLUMNS = [
    "Username", "Automation Status", "Follow Status", "Message Status",
    "Message Sent At", "Follow Requested At", "Follow Accepted At",
    "Filtered Reason", "Reply Status", "Seen Status", "Message Seen At",
    "Final Message", "Template Used", "Template Type", "Last Error",
    "Last Checked At", "Last Automation Action",
    "Already Following", "Profile Type", "Retry Count", "Automation Notes",
    "Follow Back Status", "Follow Back At", "Message Trigger", "Follow Cleanup Reason",
]

_proc_lock = threading.Lock()
_scheduler_proc = None  # tracked only for processes this dashboard itself started


# ---------------------------------------------------------------------------
# Excel reading (single pass, read-only -- never touches ExcelStore.update)
# ---------------------------------------------------------------------------

def read_leads(source_id=None):
    source = _source_by_id(source_id)
    # A reader opening at the exact moment the automation is mid-write (its .tmp -> .xlsx
    # rename) can transiently fail too, same underlying Windows file-locking rule as the write
    # side -- retry briefly rather than surfacing a one-off 500 to the dashboard for something
    # that resolves itself within a second.
    last_exc = None
    for attempt in range(5):
        try:
            wb = load_workbook(workbook_path(source["id"]), data_only=True, read_only=True)
            try:
                ws = wb[wb.sheetnames[0]]
                header_row = next(ws.iter_rows(min_row=1, max_row=1))
                headers = {str(c.value).strip(): c.column for c in header_row if c.value is not None}
                # Same header-name resolution excel_store.py's ExcelStore uses (candidate list +
                # normalized matching) -- this used to be its own separate, much stricter lookup
                # here (exact "Username" match, silently defaulting to raw column 2 otherwise),
                # which never raised on a workbook using a different header spelling (e.g. "User
                # Name") -- it just silently read the wrong column and showed blank/garbage rows
                # instead of the clear "Username column not found" error every other entry point
                # gives for the same file. Raising here now makes this path behave identically.
                username_col = ExcelStore._find_header_col(headers, ExcelStore._USERNAME_HEADER_CANDIDATES)
                if not username_col:
                    raise ExcelUnavailableError(
                        "Could not find a Username column in the workbook. Expected a header named one "
                        f"of: {', '.join(ExcelStore._USERNAME_HEADER_CANDIDATES)}."
                    )
                cols = {name: headers.get(name) for name in STAT_COLUMNS if name != "Username"}
                rows = []
                for row_num, r in enumerate(ws.iter_rows(min_row=2), start=2):
                    username = r[username_col - 1].value if username_col <= len(r) else None
                    if not username:
                        continue
                    # row_num, not r[0].row: a ragged CSV-converted sheet can have a blank first
                    # cell in some rows (a row with data in later columns but nothing in column
                    # A) -- in openpyxl's read_only mode that cell comes back as an EmptyCell,
                    # which has no .row attribute and raised AttributeError here.
                    item = {"row": row_num, "Source": source["id"], "SourceName": source["name"], "Username": username}
                    for name, col in cols.items():
                        item[name] = r[col - 1].value if col and col <= len(r) else None
                    rows.append(item)
                return rows
            finally:
                wb.close()
        except Exception as exc:
            last_exc = exc
            time.sleep(0.4 * (attempt + 1))
    raise last_exc


def read_leads_multi(source_id):
    """source_id of None/"ALL" reads every configured source and concatenates them (each row
    tagged with its Source/SourceName, from read_leads above); a specific id reads just that one
    source. One source failing to read (locked, missing) is logged and skipped rather than
    breaking the whole combined view -- matches the same failure-isolation principle as the
    scheduler itself."""
    if source_id and source_id != "ALL":
        return read_leads(source_id)
    rows = []
    for source in get_salespeople():
        try:
            rows.extend(read_leads(source["id"]))
        except Exception as exc:
            print(f"WARNING: could not read {source['name']}'s workbook for the dashboard: {exc}")
    return rows


def is_today(value):
    if not value:
        return False
    try:
        tz = ZoneInfo(settings.timezone)
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz).date() == datetime.now(tz).date()
    except Exception:
        return False


def status_of(row, key):
    return str(row.get(key) or "").strip().upper()


TAB_FILTERS = {
    "dmed_today": lambda r: is_today(r.get("Message Sent At")),
    "followed_today": lambda r: is_today(r.get("Follow Requested At")) or is_today(r.get("Follow Accepted At")),
    "replied": lambda r: status_of(r, "Reply Status") in ("REPLIED", "REPLIED_LATE"),
    "processing": lambda r: status_of(r, "Automation Status") == "PROCESSING",
    "waiting_approval": lambda r: status_of(r, "Automation Status") == "WAITING_FOLLOW_APPROVAL",
    "waiting_follow_back": lambda r: status_of(r, "Automation Status") == "WAITING_FOLLOW_BACK",
    "manual_review": lambda r: status_of(r, "Automation Status") == "MANUAL_REVIEW",
    "filtered": lambda r: status_of(r, "Automation Status") == "FILTERED",
    "sent": lambda r: status_of(r, "Message Status") == "SENT",
}

TAB_COLUMNS = ["Username", "Automation Status", "Follow Status", "Message Status",
               "Final Message", "Message Sent At", "Seen Status", "Reply Status", "Filtered Reason",
               "Last Error", "Source", "SourceName"]

# Every column shown for a diagnostics-tab lookup -- deliberately wider than TAB_COLUMNS since
# the point here is seeing the full trail behind a decision (why did this lead skip a follow?),
# not a clean at-a-glance table.
DIAGNOSTIC_COLUMNS = TAB_COLUMNS + [
    "Already Following", "Follow Requested At", "Follow Accepted At", "Profile Type",
    "Last Checked At", "Last Automation Action", "Retry Count", "Automation Notes",
    "Follow Back Status", "Follow Back At", "Message Trigger", "Message Eligible At",
    "Follow Back Deadline At", "Follow Cleanup Reason", "Unfollowed At",
]

# The three fields that make up a lead's internal "queue" state -- every distinct value the
# automation actually wrote for each of these, across the whole workbook, is what powers the
# diagnostics breakdown (not a hardcoded list, so a status this list doesn't know about still
# shows up rather than silently vanishing).
STATUS_FIELDS = ["Automation Status", "Follow Status", "Message Status", "Follow Back Status"]


def status_breakdown(rows):
    breakdown = {}
    for field in STATUS_FIELDS:
        counts = {}
        for r in rows:
            value = str(r.get(field) or "").strip() or "(blank)"
            counts[value] = counts.get(value, 0) + 1
        breakdown[field] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return breakdown


def build_stats(rows, shared_dmed_today=None, shared_followed_today=None):
    """shared_dmed_today/shared_followed_today are the TRUE account-wide totals (summed across
    every source, regardless of which source's rows are passed in `rows`) -- daily caps belong
    to the shared Instagram account, so these should always reflect the whole account even when
    viewing a single salesperson's tab. Falls back to counting just `rows` if not supplied
    (single-workbook mode, where they're the same number anyway)."""
    followed_today = sum(1 for r in rows if TAB_FILTERS["followed_today"](r))
    dmed_today = sum(1 for r in rows if TAB_FILTERS["dmed_today"](r))
    replies = sum(1 for r in rows if TAB_FILTERS["replied"](r))
    seen_no_reply = sum(1 for r in rows if status_of(r, "Seen Status") == "SEEN" and not TAB_FILTERS["replied"](r))
    manual_review = sum(1 for r in rows if TAB_FILTERS["manual_review"](r))
    filtered = sum(1 for r in rows if TAB_FILTERS["filtered"](r))
    funnel = {
        "processing": sum(1 for r in rows if status_of(r, "Automation Status") == "PROCESSING"),
        "waiting_approval": sum(1 for r in rows if status_of(r, "Automation Status") == "WAITING_FOLLOW_APPROVAL"),
        "waiting_follow_back": sum(1 for r in rows if status_of(r, "Automation Status") == "WAITING_FOLLOW_BACK"),
        "ready_to_message": sum(1 for r in rows if status_of(r, "Automation Status") == "READY_TO_MESSAGE"),
        "sent": sum(1 for r in rows if status_of(r, "Automation Status") == "MESSAGE_SENT"),
        "manual_conversation": sum(1 for r in rows if status_of(r, "Automation Status") == "MANUAL_CONVERSATION"),
    }
    # Follow-Back Timing V2 (spec section 11) -- "Follow Sent" is any row that ever had a Follow
    # Requested At written, regardless of where it is now in the pipeline; rates are 0 rather than
    # a divide-by-zero error when nothing's been followed yet.
    follow_sent = sum(1 for r in rows if r.get("Follow Requested At"))
    follow_back_received = sum(1 for r in rows if status_of(r, "Follow Back Status") == "RECEIVED")
    msgs_from_follow_back = sum(1 for r in rows if status_of(r, "Message Trigger") == "FOLLOW_BACK")
    msgs_from_y_timeout = sum(1 for r in rows if status_of(r, "Message Trigger") == "Y_TIMEOUT_PROACTIVE")
    no_follow_back_7d = sum(1 for r in rows if status_of(r, "Follow Back Status") == "EXPIRED_NO_RETURN")
    unfollowed_7d = sum(1 for r in rows if status_of(r, "Follow Cleanup Reason") == "NO_FOLLOW_BACK_7_DAYS")
    follow_back = {
        "follow_sent": follow_sent,
        "waiting_follow_back": funnel["waiting_follow_back"],
        "follow_back_received": follow_back_received,
        "follow_back_rate": round(follow_back_received / follow_sent, 3) if follow_sent else 0,
        "msgs_from_follow_back": msgs_from_follow_back,
        "msgs_from_y_timeout": msgs_from_y_timeout,
        "early_follow_back_dm_rate": round(msgs_from_follow_back / follow_sent, 3) if follow_sent else 0,
        "proactive_y_dm_rate": round(msgs_from_y_timeout / follow_sent, 3) if follow_sent else 0,
        "no_follow_back_7d": no_follow_back_7d,
        "no_follow_back_7d_rate": round(no_follow_back_7d / follow_sent, 3) if follow_sent else 0,
        "unfollowed_7d": unfollowed_7d,
    }
    return {
        "followed_today": followed_today,
        "dmed_today": dmed_today,
        "shared_followed_today": shared_followed_today if shared_followed_today is not None else followed_today,
        "shared_dmed_today": shared_dmed_today if shared_dmed_today is not None else dmed_today,
        "replies": replies,
        "seen_no_reply": seen_no_reply,
        "manual_review": manual_review,
        "filtered": filtered,
        "daily_follow_limit": settings.daily_follow_limit,
        "daily_message_limit": settings.daily_message_limit,
        "total_leads": len(rows),
        "funnel": funnel,
        "follow_back": follow_back,
    }


def tail_log(n=150):
    if not LOG_FILE.exists():
        return []
    try:
        with LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 300_000)  # last ~300KB is plenty for n lines
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return lines[-n:]
    except Exception as exc:
        return [f"(could not read log: {exc})"]


# ---------------------------------------------------------------------------
# Commands -- these call the exact same functions main.py's CLI calls
# ---------------------------------------------------------------------------

def run_command(action, params):
    global _scheduler_proc
    if action == "pause":
        with application_lock() as locked:
            if locked:
                mark_paused_safe()
                return {"ok": True, "message": "Paused (PAUSED_SAFE)."}
            request_pause()
            return {"ok": True, "message": "Pause requested; will apply between workers."}
    if action == "resume":
        resume()
        return {"ok": True, "message": "Resumed (mode RUN)."}
    if action == "rewind":
        source = _source_by_id(params.get("source"))
        rewind(int(params.get("amount", 1)), state_path=source["state"])
        return {"ok": True, "message": f"Rewound {source['name']}'s pointer by {params.get('amount', 1)}."}
    if action == "skip":
        source = _source_by_id(params.get("source"))
        skip(int(params.get("amount", 1)), state_path=source["state"])
        return {"ok": True, "message": f"Skipped {source['name']}'s pointer by {params.get('amount', 1)}."}
    if action == "set_pointer":
        source = _source_by_id(params.get("source"))
        set_pointer(int(params["index"]), state_path=source["state"])
        return {"ok": True, "message": f"{source['name']}'s pointer set to {params['index']}."}
    if action == "run_once":
        subprocess.Popen([PY, "main.py", "run-once"], cwd=BASE_DIR)
        return {"ok": True, "message": "run-once started in the background — watch the log."}
    if action == "start_scheduler":
        with _proc_lock:
            if _scheduler_proc and _scheduler_proc.poll() is None:
                return {"ok": False, "message": "Scheduler already running (started from this dashboard)."}
            _scheduler_proc = subprocess.Popen([PY, "scheduler.py"], cwd=BASE_DIR)
        return {"ok": True, "message": "Scheduler started."}
    if action == "stop_scheduler":
        with _proc_lock:
            if not _scheduler_proc or _scheduler_proc.poll() is not None:
                return {"ok": False, "message": "No scheduler process tracked by this dashboard to stop. "
                                                 "If it was started from a terminal, stop it there (Ctrl+C) "
                                                 "or use Pause instead — it's always safe."}
            _scheduler_proc.terminate()
        return {"ok": True, "message": "Scheduler process stopped. State is safely persisted; use Resume + Start Scheduler to continue."}
    return {"ok": False, "message": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep console quiet; automation.log already has the real trail

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/dashboard.html":
                html = DASHBOARD_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return
            if path == "/api/sources":
                sources = get_salespeople()
                self._send_json({
                    "multi": settings.multi_salesperson,
                    "sources": [{"id": s["id"], "name": s["name"]} for s in sources],
                })
                return
            if path == "/api/state":
                source_id = (qs.get("source") or [None])[0]
                source = _source_by_id(source_id)
                # Global fields (mode, shared next-times, error) merged with THIS one source's
                # own pointer/progress -- same merged shape the dashboard has always read, just
                # now explicit about which source's pointer it is.
                state = {**load_global_state(), **load_source_state(source["state"])}
                with _proc_lock:
                    scheduler_running = bool(_scheduler_proc and _scheduler_proc.poll() is None)
                state["SCHEDULER_TRACKED_RUNNING"] = scheduler_running
                # So the dashboard can render every timestamp in the automation's own operating
                # timezone (TIMEZONE in .env) instead of whichever timezone the viewer's browser
                # happens to be in -- otherwise "today" counts and displayed times can disagree.
                state["TIMEZONE"] = settings.timezone
                state["FOLLOW_BACK_WAIT_HOURS"] = settings.follow_back_wait_hours
                state["SOURCE"] = source["id"]
                self._send_json(state)
                return
            if path == "/api/stats":
                source_id = (qs.get("source") or ["ALL" if settings.multi_salesperson else None])[0]
                rows = read_leads_multi(source_id)
                if settings.multi_salesperson:
                    all_rows = rows if source_id in (None, "ALL") else read_leads_multi("ALL")
                    shared_dmed = sum(1 for r in all_rows if TAB_FILTERS["dmed_today"](r))
                    shared_followed = sum(1 for r in all_rows if TAB_FILTERS["followed_today"](r))
                    self._send_json(build_stats(rows, shared_dmed, shared_followed))
                else:
                    self._send_json(build_stats(rows))
                return
            if path == "/api/leads":
                tab = (qs.get("tab") or ["dmed_today"])[0]
                source_id = (qs.get("source") or ["ALL" if settings.multi_salesperson else None])[0]
                rows = read_leads_multi(source_id)
                # Diagnostics mode: `field`+`value` picks out an EXACT status value seen in the
                # data (e.g. Automation Status=PROCESSING) instead of one of the curated tabs
                # above -- lets you inspect any internal queue/bucket, not just the pre-picked
                # ones, and returns the wider DIAGNOSTIC_COLUMNS (full decision trail) instead
                # of the compact TAB_COLUMNS.
                field = (qs.get("field") or [None])[0]
                value = (qs.get("value") or [None])[0]
                if field:
                    target = "" if value in (None, "(blank)") else value
                    matched = [r for r in rows if str(r.get(field) or "").strip() == target]
                    out = [{k: r.get(k) for k in DIAGNOSTIC_COLUMNS} | {"row": r["row"]} for r in matched]
                    self._send_json({"field": field, "value": value, "source": source_id or "ALL", "count": len(out), "leads": out[:200]})
                    return
                fn = TAB_FILTERS.get(tab)
                matched = [r for r in rows if fn(r)] if fn else []
                out = [{k: r.get(k) for k in TAB_COLUMNS} | {"row": r["row"]} for r in matched]
                self._send_json({"tab": tab, "source": source_id or "ALL", "count": len(out), "leads": out[:200]})
                return
            if path == "/api/status_breakdown":
                source_id = (qs.get("source") or ["ALL" if settings.multi_salesperson else None])[0]
                rows = read_leads_multi(source_id)
                self._send_json({"source": source_id or "ALL", "total": len(rows), "breakdown": status_breakdown(rows)})
                return
            if path == "/api/log":
                n = int((qs.get("lines") or ["150"])[0])
                self._send_json({"lines": tail_log(n)})
                return
            self._send_json({"ok": False, "message": "Not found"}, status=404)
        except Exception as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/command":
            self._send_json({"ok": False, "message": "Not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            action = body.get("action", "")
            result = run_command(action, body)
            self._send_json(result)
        except Exception as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=500)



def _autodetect_workbook():
    """WORKBOOK_PATH in .env is what the automation actually uses -- it does NOT scan the
    folder on every run. This only fills that setting in automatically, once, the first time
    it's missing: if exactly one .xlsx file sits in this folder, use it (converting a lone .csv
    to .xlsx first if that's all there is) and remember the choice in .env for every future run
    (this process's own and any main.py/scheduler.py subprocess). If there's more than one
    candidate, or none, it asks rather than guessing."""
    configured = BASE_DIR / settings.workbook_path
    if configured.exists():
        if configured.suffix.lower() == ".csv":
            xlsx_path = csv_to_xlsx(configured)
            _write_env_var("WORKBOOK_PATH", xlsx_path.name)
            return xlsx_path
        return configured

    xlsx_candidates = [p for p in BASE_DIR.glob("*.xlsx")
                        if not p.name.endswith(".backup.xlsx") and not p.name.startswith("~$")]
    if len(xlsx_candidates) == 1:
        print(f"WORKBOOK_PATH ({settings.workbook_path}) not found; found exactly one Excel "
              f"file in this folder: {xlsx_candidates[0].name} -- using it, and saving that choice to .env.")
        _write_env_var("WORKBOOK_PATH", xlsx_candidates[0].name)
        return xlsx_candidates[0]
    if len(xlsx_candidates) > 1:
        names = ", ".join(p.name for p in xlsx_candidates)
        print(f"Multiple Excel files found ({names}) and none match WORKBOOK_PATH -- "
              f"set WORKBOOK_PATH in .env to the one to use.")
        return None

    csv_candidates = [p for p in BASE_DIR.glob("*.csv") if not p.name.startswith("~$")]
    if len(csv_candidates) == 1:
        print(f"No Excel file found, but found a CSV file: {csv_candidates[0].name} -- converting it automatically.")
        xlsx_path = csv_to_xlsx(csv_candidates[0])
        _write_env_var("WORKBOOK_PATH", xlsx_path.name)
        return xlsx_path
    if len(csv_candidates) > 1:
        names = ", ".join(p.name for p in csv_candidates)
        print(f"Multiple CSV files found ({names}) and no Excel file -- put only one leads "
              f"file in this folder, or set WORKBOOK_PATH in .env.")
        return None

    print(f"No Excel or CSV file found (looked for {settings.workbook_path} and any .xlsx/.csv "
          f"file in this folder). Put your leads spreadsheet here, or set WORKBOOK_PATH in .env.")
    return None


def _prepare_source(source):
    """Multi-salesperson mode: resolves the source's configured workbook name (tolerating a
    missing/wrong extension), converts it from CSV if that's what's found, and provisions
    columns -- via the same workbook_prep.resolve_and_prepare() every other entry point
    (main.py, scheduler.py) uses now, so behavior can't drift between them again."""
    path = resolve_and_prepare(source["workbook"], BASE_DIR, env_key=f"{source['id']}_WORKBOOK",
                                label=f"{source['name']}'s workbook")
    if path is not None:
        print(f"{source['name']} workbook OK: {path} (automation columns present).")
    return path


def ensure_workbook_ready():
    """Runs on every dashboard startup: figures out which workbook(s) to use, then adds any
    missing automation columns, so you can point this at a brand-new client workbook and it just
    works -- no manual 'python main.py init' step needed. Never touches existing data, only
    appends missing column headers (and takes the usual one-time .backup.xlsx safety copy). In
    single-workbook mode this is exactly the previous single-file auto-detect/convert/provision
    behavior; in multi-salesperson mode it does the same for each of the three configured
    sources independently -- one source failing to prepare doesn't stop the others."""
    global RESOLVED_WORKBOOKS
    if not settings.multi_salesperson:
        path = _autodetect_workbook()
        if path is not None:
            try:
                ExcelStore(path).ensure_columns()
                print(f"Workbook OK: {path} (automation columns present).")
                RESOLVED_WORKBOOKS[get_salespeople()[0]["id"]] = path
            except ExcelUnavailableError as exc:
                print(f"WARNING: could not prepare the workbook at startup: {exc}")
                print("Check the file isn't open in Excel right now, then restart the dashboard.")
        return
    for source in get_salespeople():
        path = _prepare_source(source)
        if path is not None:
            RESOLVED_WORKBOOKS[source["id"]] = path


def _auto_resume_scheduler_if_was_running():
    """If the scheduler was in RUN mode the last time this machine was on (e.g. before a
    restart/shutdown), start it again automatically -- so re-launching after a reboot just
    picks back up. If it was left PAUSED_SAFE/PAUSE_REQUESTED/PAUSED_ERROR, leave it exactly
    as it was; only an explicit Resume click should ever change that."""
    if load_state().get("MODE") != "RUN":
        return
    result = run_command("start_scheduler", {})
    print(f"Was running before -> {result['message']}")


def main():
    ensure_workbook_ready()
    _auto_resume_scheduler_if_was_running()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Command Center running at {url}  (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with _proc_lock:
            if _scheduler_proc and _scheduler_proc.poll() is None:
                print("Stopping tracked scheduler process...")
                _scheduler_proc.terminate()


if __name__ == "__main__":
    main()
