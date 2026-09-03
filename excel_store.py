from __future__ import annotations
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import shutil
from openpyxl import load_workbook
from config import settings
from workers.common import local_priority

logger = logging.getLogger(__name__)

AUTOMATION_HEADERS = [
    "Automation Status","Profile Type","Already Following","Follow Status",
    "Follow Requested At","Follow Accepted At","Message Status","Message Sent At",
    "Seen Status","Message Seen At","Reply Status","Template Used","Template Type",
    "Personalization Used","Final Message","Detected Language","Translation Language",
    "Translated Message","Last Checked At","Next Check At","Retry Count","Filtered Reason",
    "Last Error","Automation Notes","Last Automation Action","Language Override","Do Not ReFollow",
    "Automation Username",
    # Follow-Back Timing V2 -- see workers/follow_back_monitor.py.
    "Follow Back Status","Follow Back At","Message Fallback At","Message Eligible At",
    "Message Trigger","Follow Back Deadline At","Follow Cleanup Reason","Unfollowed At",
]

NO_REFOLLOW_REASONS = {
    "PRIORITY_ZERO","INVALID_PRIORITY","DUPLICATE","DUPLICATE_USER_ID",
    "NO_FOLLOW_ACCEPTANCE","SEEN_NO_REPLY","UNSEEN_7_DAYS","PREVIOUS_CONVERSATION",
    "DO_NOT_CONTACT","MANUAL_SUPPRESSION","BECAME_PUBLIC_WITHOUT_ACCEPTING",
    "USERNAME_CHANGED_MANUAL_REVIEW","OPEN_TO_NEW_CONNECTION_NO",
}
NO_REFOLLOW_STATUSES = {"FILTERED","MANUAL_CONVERSATION","COMPLETED"}

class ExcelUnavailableError(RuntimeError): pass

class ExcelStore:
    def __init__(self, path=None): self.path = Path(path or settings.workbook_path)

    def backup_once(self):
        backup = self.path.with_name(self.path.stem + ".backup" + self.path.suffix)
        if not backup.exists():
            try:
                shutil.copy2(self.path, backup)
                logger.info("Created Excel backup at %s", backup)
            except Exception as exc:
                logger.error("Failed to create Excel backup: %s", exc, exc_info=True)
                raise ExcelUnavailableError(f"Cannot create Excel backup: {exc}") from exc

    def _load(self):
        try: return load_workbook(self.path)
        except Exception as exc: raise ExcelUnavailableError(f"Cannot open Excel workbook: {exc}") from exc

    @staticmethod
    def _headers(ws): return {str(c.value).strip(): c.column for c in ws[1] if c.value is not None}

    def ensure_columns(self):
        self.backup_once()
        wb = self._load(); ws = wb[wb.sheetnames[0]]; headers = self._headers(ws)
        added = False
        for name in AUTOMATION_HEADERS:
            if name not in headers:
                ws.cell(1, ws.max_column + 1, name)
                added = True
        if added:
            self._atomic_save(wb)
        else:
            # Nothing to change -- skip the save+rename entirely. This is called at startup by
            # both dashboard_server.py and, moments later, the scheduler.py subprocess it
            # auto-launches; without this, both would do a real write to the same just-created
            # file within milliseconds of each other, which is a genuine collision on Windows
            # (not an external scanner) and can fail every retry, not just a transient one.
            logger.debug("ensure_columns: all automation columns already present, nothing to save.")

    def _atomic_save(self, wb):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            wb.save(tmp)
        except Exception as exc:
            try:
                if tmp.exists(): tmp.unlink()
            except Exception: pass
            raise ExcelUnavailableError(f"Excel is locked or cannot be written: {exc}") from exc
        # A brand-new or just-rewritten file can be transiently locked for a moment by something
        # external -- Windows Search Indexer, antivirus real-time scanning, OneDrive/cloud sync --
        # grabbing it right after it's created, before the immediately-following rename. This is
        # a WinError 5 (Access Denied), not a real "someone has this file open" (WinError 32), and
        # usually clears within a second or two -- but a *burst* of saves right after a workbook
        # was just created/converted (e.g. processing several new leads in one go) can land in
        # the middle of a longer external scan, so give this real headroom (up to ~20s total)
        # rather than giving up too early on a background, non-interactive process.
        attempts = 12
        last_exc = None
        for attempt in range(attempts):
            try:
                tmp.replace(self.path)
                return
            except Exception as exc:
                last_exc = exc
                delay = min(0.3 * (attempt + 1), 2.0)
                logger.warning("Rename to %s failed (attempt %d/%d): %s -- retrying in %.1fs", self.path, attempt + 1, attempts, exc, delay)
                time.sleep(delay)
        try:
            if tmp.exists(): tmp.unlink()
        except Exception: pass
        raise ExcelUnavailableError(f"Excel is locked or cannot be written: {last_exc}") from last_exc

    # Different client workbooks name/order their columns differently (e.g. one file's column A
    # is "KOG", another's is "Avatar", another spells the username column "User Name" with a
    # space) -- so identity columns are found by header name, not position. Column E/G/H are the
    # one deliberate exception: the client's own spec names those by letter ("Column H is the
    # primary priority..."), so those stay position-based below.
    #
    # Matching is normalized (lowercased, every non-alphanumeric character stripped) before
    # comparing against the candidate list below, so "User Name", "Username", "user_name",
    # "USER-NAME", and "IG Username" are all recognized as the same header without needing every
    # spacing/punctuation variant spelled out by hand -- only genuinely different wording needs
    # its own entry in the list.
    _USERNAME_HEADER_CANDIDATES = (
        "username", "user name", "ig username", "instagram username", "insta username",
        "handle", "instagram handle", "ig handle", "account username", "account handle",
        "profile username", "profile handle", "screen name", "insta handle", "ig name",
    )
    _USER_ID_HEADER_CANDIDATES = (
        "user id", "userid", "uid", "ig user id", "instagram user id", "insta user id",
        "profile id", "account id", "ig id", "instagram id", "pk",
    )
    # E/G/H candidate headers -- matched by name first (see _egh_cols below), using the client's
    # own real template wording (confirmed against the actual contract file and multiple
    # salespeople's files): column H is labeled "How important to me?" and G "How hard to reach?",
    # not literally "Priority" anywhere. A generic "priority"-style fallback is included too for
    # any future file that labels it more plainly.
    _OPEN_CONNECTION_HEADER_CANDIDATES = (
        "open to new connection", "open to new connections", "open to connect",
        "open for new connection", "new connection",
    )
    _PRIORITY_G_HEADER_CANDIDATES = (
        "how hard to reach", "hard to reach", "reach difficulty", "priority secondary",
        "secondary priority", "priority g",
    )
    _PRIORITY_H_HEADER_CANDIDATES = (
        "how important to me", "important to me", "importance", "priority primary",
        "primary priority", "priority h", "priority",
    )

    @staticmethod
    def _normalize_header(value) -> str:
        import re
        return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())

    @classmethod
    def _find_header_col(cls, headers, candidates):
        normalized_map = {}
        for k, v in headers.items():
            nk = cls._normalize_header(k)
            if nk and nk not in normalized_map:  # first occurrence wins if two headers collide once normalized
                normalized_map[nk] = v
        for name in candidates:
            n = cls._normalize_header(name)
            if n in normalized_map:
                return normalized_map[n]
        return None

    def _identity_cols(self, headers):
        username = self._find_header_col(headers, self._USERNAME_HEADER_CANDIDATES)
        user_id = self._find_header_col(headers, self._USER_ID_HEADER_CANDIDATES)
        if not username:
            raise ExcelUnavailableError(
                "Could not find a Username column in the workbook. Expected a header named one "
                f"of: {', '.join(self._USERNAME_HEADER_CANDIDATES)}."
            )
        if not user_id:
            raise ExcelUnavailableError(
                "Could not find a User ID column in the workbook. Expected a header named one "
                f"of: {', '.join(self._USER_ID_HEADER_CANDIDATES)}."
            )
        return username, user_id

    def _egh_cols(self, headers, width):
        """Resolves the real columns for E ("Open to new connection?"), G (secondary priority),
        and H (primary priority) -- returns (e_col, g_col, h_col), each either a real column
        index or None if that concept genuinely isn't present in this file.

        Header name is tried FIRST, against the client's own real template wording -- this finds
        the right column regardless of where it actually sits in a given file, which is what lets
        one file have these in a totally different order/position than another. Position 5/7/8 is
        used ONLY as a fallback, and only when that position's header cell is blank/unlabeled (a
        file following the letter-only contract convention with no header text at all) or the
        column doesn't exist yet. If a position has real header text that just doesn't match any
        known priority/connection wording (e.g. "Follow by you", "Full Name" -- a different
        automation tool's own export columns happening to land in the same spot), that is NOT
        treated as E/G/H data -- reading it as if it were would filter leads based on garbage,
        which is exactly the bug this method exists to prevent (a boolean "Follow by you" = False
        read as priority H = 0 filtered nearly an entire file)."""
        def resolve(candidates, position):
            found = self._find_header_col(headers, candidates)
            if found:
                return found
            if position is None:
                return None
            header_at_position = None
            for name, col in headers.items():
                if col == position:
                    header_at_position = name
                    break
            if header_at_position is None or not str(header_at_position).strip():
                # Position exists but has no real header text (or doesn't exist at all if width
                # is too small) -- safe to fall back to the client's letter-position convention.
                return position if width >= position else None
            # Position has real, different header text -- not E/G/H, don't misread it.
            return None

        e_col = resolve(self._OPEN_CONNECTION_HEADER_CANDIDATES, 5)
        g_col = resolve(self._PRIORITY_G_HEADER_CANDIDATES, 7)
        h_col = resolve(self._PRIORITY_H_HEADER_CANDIDATES, 8)
        return e_col, g_col, h_col

    # Sort rank only -- lower rank sorts first (processed sooner). 1 is the highest real
    # priority (processed first), 3 is the lowest of the valid values, blank is lower priority
    # than any explicit 1/2/3, and anything invalid sorts dead last. 0 is a hard filter and
    # never reaches this sort at all (see local_priority / apply_hard_filter_if_needed).
    _PRIORITY_RANK = {1: 0, 2: 1, 3: 2}

    @classmethod
    def _priority(cls, v):
        if v is None or str(v).strip() == "": return 3
        try:
            n = int(float(v)); return cls._PRIORITY_RANK.get(n, 99)
        except Exception: return 99

    def max_row(self):
        wb = self._load(); ws = wb[wb.sheetnames[0]]
        return ws.max_row

    @staticmethod
    def _original_width(headers):
        """How many columns of the client's own original data exist, before this automation's
        own columns (AUTOMATION_HEADERS) were appended on the right. Some salesperson files
        (e.g. a simple export with just index/User ID/Avatar/Profile URL/User Name/Full Name)
        never had a real column E ("Open to new connection?") or G/H (priority) at all -- for
        those files, position 5/7/8 land on this automation's OWN columns (e.g. "Automation
        Status", "Profile Type") once appended, not on real client data. Reading those as if they
        were real E/G/H values would filter out leads based on garbage, which is exactly what
        happened to Nick's file (see hard_filter_reason below). Detecting "does a real column E/
        G/H exist" this way -- rather than by header name -- matches the client's own spec, which
        defines E/G/H purely by spreadsheet position, not by what they're labeled."""
        automation_cols = {col for name, col in headers.items() if name in AUTOMATION_HEADERS}
        if not automation_cols:
            return max(headers.values()) if headers else 0
        return min(automation_cols) - 1

    def all_rows(self):
        wb = self._load(); ws = wb[wb.sheetnames[0]]; headers = self._headers(ws)
        username_col, user_id_col = self._identity_cols(headers)
        width = self._original_width(headers)
        _, g_col, h_col = self._egh_cols(headers, width)
        rows = []
        for row in range(2, ws.max_row + 1):
            username = ws.cell(row, username_col).value
            uid = ws.cell(row, user_id_col).value if user_id_col else None
            if not username and not uid: continue
            g = ws.cell(row,g_col).value if g_col else None
            h = ws.cell(row,h_col).value if h_col else None
            rows.append({"row":row,"username":str(username or "").strip().lstrip("@"),"user_id":str(uid or "").strip(),"g":g,"h":h})
        return rows

    def physical_rows_after(self, last_row):
        wb = self._load(); ws = wb[wb.sheetnames[0]]; headers = self._headers(ws)
        username_col, user_id_col = self._identity_cols(headers)
        width = self._original_width(headers)
        _, g_col, h_col = self._egh_cols(headers, width)
        result=[]
        for row in range(max(2,int(last_row)+1), ws.max_row+1):
            g = ws.cell(row,g_col).value if g_col else None
            h = ws.cell(row,h_col).value if h_col else None
            result.append({"row":row,"username":str(ws.cell(row,username_col).value or "").strip().lstrip("@"),"user_id":str(ws.cell(row,user_id_col).value or "").strip(),"g":g,"h":h})
        return result

    def priority_rows(self):
        return sorted(self.all_rows(), key=lambda x:(self._priority(x["h"]),self._priority(x["g"]),x["row"]))

    def get_row(self, row):
        wb=self._load(); ws=wb[wb.sheetnames[0]]; headers=self._headers(ws); result={"row":row}
        for name,col in headers.items(): result[name]=ws.cell(row,col).value
        return result

    def update(self, row, **fields):
        wb=self._load(); ws=wb[wb.sheetnames[0]]; headers=self._headers(ws)
        for name,value in fields.items():
            if name in headers: ws.cell(row,headers[name],value)
        self._atomic_save(wb)
        logger.debug("Excel row %s updated: %s", row, fields)

    def duplicate_user_ids(self):
        found, duplicates = {}, set()
        for lead in self.all_rows():
            uid=lead["user_id"]
            if not uid: continue
            if uid in found: duplicates.add(uid)
            else: found[uid]=lead["row"]
        return duplicates

    def hard_filter_reason(self, row, allow_blank=True):
        # Columns E/G/H ("Open to new connection?", secondary/primary priority) are resolved via
        # _egh_cols() -- header name first (against the client's own real template wording),
        # falling back to the contractual position-by-letter convention only when that position's
        # header is genuinely blank/unlabeled. See _egh_cols()'s own docstring for the full
        # reasoning and the two real-world bugs this specifically fixes: a file with too few
        # columns (position 7/8 landing on this automation's own appended columns instead of real
        # data), and a file with *enough* columns but where position 7/8 happen to be something
        # else entirely (e.g. a "Follow by you" boolean column from a different export tool,
        # misread as priority=0 and silently filtering out nearly the whole file).
        wb=self._load(); ws=wb[wb.sheetnames[0]]; headers=self._headers(ws)
        width = self._original_width(headers)
        e_col, g_col, h_col = self._egh_cols(headers, width)
        if e_col:
            open_value=str(ws.cell(row,e_col).value or "").strip().upper()
            if open_value == "NO": return "OPEN_TO_NEW_CONNECTION_NO"
        if g_col and h_col:
            g=ws.cell(row,g_col).value; h=ws.cell(row,h_col).value
            ok, reason=local_priority(g,h,allow_blank=allow_blank)
            return None if ok else reason
        return None

    def apply_hard_filter_if_needed(self, row, allow_blank=True):
        reason=self.hard_filter_reason(row,allow_blank)
        if not reason: return None
        status="FILTERED" if reason in {"PRIORITY_ZERO","OPEN_TO_NEW_CONNECTION_NO"} else "MANUAL_REVIEW"
        self.update(row, **{
            "Automation Status":status,
            "Filtered Reason":reason,
            "Do Not ReFollow":"YES" if status=="FILTERED" else "",
            "Last Automation Action":"LOCAL_FILTER",
        })
        return reason

    def has_outward_history(self, data):
        if data.get("Follow Requested At") or data.get("Message Sent At"): return True
        if str(data.get("Follow Status") or "").upper() in {"REQUESTED","ACCEPTED","UNFOLLOWED","EXPIRED","UNCERTAIN","CANCEL_PENDING"}: return True
        if str(data.get("Message Status") or "").upper() in {"SENDING","SENT","UNCERTAIN"}: return True
        return False

    def validate_automation_username(self, row, username):
        data=self.get_row(row); saved=str(data.get("Automation Username") or "").strip().lstrip("@")
        current=str(username or "").strip().lstrip("@")
        if not saved:
            self.update(row, **{"Automation Username":current})
            return True
        if saved.lower()==current.lower(): return True
        if self.has_outward_history(data):
            self.update(row, **{
                "Automation Status":"MANUAL_REVIEW","Filtered Reason":"USERNAME_CHANGED_MANUAL_REVIEW",
                "Do Not ReFollow":"YES","Last Automation Action":"USERNAME_CHANGE_DETECTED",
                "Automation Notes":f"Automation username was {saved}; Excel username is now {current}."
            })
            return False
        self.update(row, **{"Automation Username":current,"Automation Notes":f"Username updated before outward action: {saved} -> {current}"})
        return True

    def do_not_refollow(self, row):
        data=self.get_row(row)
        if str(data.get("Do Not ReFollow") or "").strip().upper() in {"YES","Y","TRUE","1"}: return True
        reason=str(data.get("Filtered Reason") or "").strip().upper(); status=str(data.get("Automation Status") or "").strip().upper(); follow=str(data.get("Follow Status") or "").strip().upper()
        return reason in NO_REFOLLOW_REASONS or status in NO_REFOLLOW_STATUSES or follow in {"UNFOLLOWED","EXPIRED"}

    def today_count(self, timestamp_header):
        tz=ZoneInfo(settings.timezone); today=datetime.now(tz).date(); wb=self._load(); ws=wb[wb.sheetnames[0]]; headers=self._headers(ws); col=headers.get(timestamp_header)
        if not col: return 0
        n=0
        for row in range(2,ws.max_row+1):
            value=ws.cell(row,col).value
            if not value: continue
            try:
                dt=value if isinstance(value,datetime) else datetime.fromisoformat(str(value))
                if dt.tzinfo is None: dt=dt.replace(tzinfo=tz)
                if dt.astimezone(tz).date()==today: n+=1
            except Exception: pass
        return n

    def message_buffer_count(self):
        # Was calling self.get_row(row) -- a full fresh workbook reload -- once PER ROW inside
        # this loop. Fine on a small test file, but a genuine N+1-style bug: confirmed directly
        # against a real 676-row file, this took 63 SECONDS for a single call. This runs at the
        # top of every profile_processor AND message_sender call, for every source, every cycle --
        # on a real multi-hundred-row file that's minutes of completely silent, unlogged delay on
        # every single cycle, which is exactly what was being reported as the automation "getting
        # stuck". Loading the workbook once and reading the two needed cells directly (same
        # pattern as today_count() below) is the fix -- one load instead of N+1.
        wb=self._load(); ws=wb[wb.sheetnames[0]]; headers=self._headers(ws)
        status_col=headers.get("Automation Status"); msg_col=headers.get("Message Status")
        n=0
        for row in range(2, ws.max_row+1):
            msg=str(ws.cell(row,msg_col).value or "").upper() if msg_col else ""
            if msg in {"READY","SCHEDULED","SENDING"}: n+=1
            elif status_col and str(ws.cell(row,status_col).value or "").upper()=="READY_TO_MESSAGE": n+=1
        return n


def shared_today_count(timestamp_header, sources):
    """Sums today_count() across every source's workbook -- used for the daily follow/message
    caps, which belong to the shared Instagram account, not to any one salesperson's file (multi-
    salesperson spec section 5: "must never multiply account limits by three"). In single-source
    mode `sources` has exactly one entry, so this is identical to that one store's own count. A
    source whose workbook can't currently be read (locked, mid-write, temporarily missing) is
    counted as 0 for this check rather than raising -- a shared limit check must not crash the
    whole cycle over one source's transient file issue (multi-salesperson spec section 14:
    failure isolation)."""
    # Lazy import: workbook_prep imports ExcelStore from this module, so importing it at module
    # level here would be circular. By call time both modules are already fully loaded.
    from workbook_prep import find_workbook_file
    base_dir = Path(__file__).resolve().parent
    total = 0
    for source in sources:
        try:
            # source["workbook"] is whatever's configured in .env -- which may still say ".csv"
            # even after a prior run converted and persisted the ".xlsx" name, if this process's
            # own `settings` was loaded before that write happened (settings is read once at
            # startup, not live-reloaded). Resolve to whatever file actually exists on disk now,
            # the same extension-tolerant way every other entry point does.
            resolved = find_workbook_file(source["workbook"], base_dir)
            path = resolved if resolved is not None else source["workbook"]
            total += ExcelStore(path).today_count(timestamp_header)
        except Exception as exc:
            logger.warning("Could not read today_count(%s) from %s for shared limit check: %s",
                            timestamp_header, source.get("workbook"), exc)
    return total
