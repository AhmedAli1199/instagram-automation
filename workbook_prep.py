"""Shared workbook resolution/preparation logic.

This used to live only inside dashboard_server.py, which meant main.py and
scheduler.py (run directly, or auto-launched as subprocesses) never got the
CSV-to-xlsx conversion or extension-optional file matching -- they'd crash
with FileNotFoundError or "openpyxl does not support .csv" instead. Every
entry point now calls into this one module so behavior can never drift again.

Extension-optional matching: a configured name like "cc_automation_ready"
(no extension at all), or one with the "wrong" extension (e.g. someone typed
".csv" but the real file on disk is already a converted ".xlsx", or vice
versa), is resolved by trying, in order: the exact configured name, then
"<stem>.xlsx", then "<stem>.csv" -- whichever actually exists on disk wins.
This is what lets Amit just drop a file in the folder and put its base name
in .env without worrying about the extension.
"""
from pathlib import Path

from env_utils import write_env_var as _write_env_var
from excel_store import ExcelStore, ExcelUnavailableError


def repair_mojibake(value: str) -> str:
    """Best-effort fix for a common CSV export bug: UTF-8 text (e.g. Hebrew names, emoji) that
    got misread as Windows-1252 and re-saved, producing garbage like '×¨×•Ö¹×™' instead of
    'רוֹי'. Reversing that round-trip (encode as cp1252, decode as UTF-8) recovers the original
    text when the file was corrupted this way. Never raises and never guesses wrong: if the
    reversal isn't possible, the original value is returned completely unchanged."""
    if not value:
        return value
    try:
        return value.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def csv_to_xlsx(csv_path: Path) -> Path:
    """Converts a CSV leads file to a real .xlsx alongside it. The original CSV is never
    modified or deleted; if a matching .xlsx already exists here it's left alone too (never
    silently overwritten)."""
    import csv as csv_module
    from openpyxl import Workbook
    xlsx_path = csv_path.with_suffix(".xlsx")
    if xlsx_path.exists():
        print(f"{xlsx_path.name} already exists next to {csv_path.name} -- using the existing Excel file, not re-converting.")
        return xlsx_path
    wb = Workbook()
    ws = wb.active
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv_module.reader(f):
            ws.append([repair_mojibake(cell) for cell in row])
    wb.save(xlsx_path)
    print(f"Converted {csv_path.name} to {xlsx_path.name} for the automation to use "
          f"(automatically repairing garbled non-English text where possible). "
          f"Your original CSV file is untouched.")
    return xlsx_path


def find_workbook_file(configured_name: str, base_dir: Path) -> Path | None:
    """Resolves a configured filename (with or without an extension, right or wrong one) to
    whatever file actually exists in base_dir. Tries, in order: "<stem>.xlsx", then the exact
    name as given, then "<stem>.csv". Returns None if nothing matches any of those.

    ".xlsx" is checked first even when the configured name itself says ".csv" -- csv_to_xlsx()
    never deletes the original CSV, so after the first conversion both files sit side by side.
    If the exact-name-first order were used instead, a source still configured as
    "leads.csv" would keep re-resolving to the raw CSV forever even after conversion, which is
    exactly the bug this caused in shared_today_count() (re-hitting "openpyxl does not support
    .csv" on every single call, in every process that hadn't itself just performed the
    conversion and updated its own in-memory settings)."""
    stem = Path(configured_name).stem
    xlsx_candidate = base_dir / f"{stem}.xlsx"
    if xlsx_candidate.exists() and xlsx_candidate.is_file():
        return xlsx_candidate
    exact = base_dir / configured_name
    if exact.exists() and exact.is_file():
        return exact
    csv_candidate = base_dir / f"{stem}.csv"
    if csv_candidate.exists() and csv_candidate.is_file():
        return csv_candidate
    return None


def resolve_and_prepare(configured_name: str, base_dir: Path, env_key: str | None = None,
                         label: str = "workbook") -> Path | None:
    """The one function every entry point (dashboard, main.py, scheduler.py) should call to go
    from a configured filename to a ready-to-use .xlsx Path:

    1. Finds the actual file on disk, tolerating a missing/wrong extension (see
       find_workbook_file).
    2. Converts it to .xlsx first if what was found is a .csv.
    3. Ensures the automation's required columns are present.
    4. If env_key is given and the resolved name differs from what's configured (extension was
       added/changed, or a .csv got converted), persists the resolved name back to .env so
       future runs -- including subprocesses -- resolve instantly without re-scanning.

    Returns the ready .xlsx Path, or None if the file couldn't be found/prepared (already
    logged to console; caller should treat this source as unavailable for this cycle/run).
    """
    found = find_workbook_file(configured_name, base_dir)
    if found is None:
        print(f"WARNING: {label} ({configured_name}) was not found in {base_dir} "
              f"(looked for that exact name, and for '{Path(configured_name).stem}.xlsx' / "
              f"'{Path(configured_name).stem}.csv'). Put the file there" +
              (f", or update {env_key} in .env." if env_key else "."))
        return None

    path = found
    if path.suffix.lower() == ".csv":
        path = csv_to_xlsx(path)

    if env_key and path.name != configured_name:
        _write_env_var(env_key, path.name)

    try:
        ExcelStore(path).ensure_columns()
    except ExcelUnavailableError as exc:
        print(f"WARNING: could not prepare {label} ({path.name}): {exc}")
        return None

    return path
