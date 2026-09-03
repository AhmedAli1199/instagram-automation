"""Multi-salesperson source ordering.

Every worker processes every source every cycle (profile checks, monitoring, follow/DM
eligibility -- all of it, for all sources, every time). What Round Robin actually controls is
*fairness for the single shared action slot*: because NEXT_FOLLOW_NOT_BEFORE / daily caps are
global, only one follow and one DM can actually go out across the whole account per window
regardless of how many sources have something eligible. Without rotation, whichever source is
checked first would always win that race. rotated_sources() rotates who goes first each cycle
so that advantage moves fairly around SP1/SP2/SP3 over time.

In single-workbook mode (settings.multi_salesperson=False) get_salespeople() already returns
exactly one source, so rotation is a no-op and this module changes nothing about that path.
"""
from config import get_salespeople
from state_store import load_global_state, save_global_state


def rotated_sources():
    sources = get_salespeople()
    if len(sources) <= 1:
        return sources
    ids = [s["id"] for s in sources]
    next_id = load_global_state().get("NEXT_SALESPERSON") or ids[0]
    start = ids.index(next_id) if next_id in ids else 0
    return sources[start:] + sources[:start]


def advance_rotation(processed_order):
    """Call once per cycle after processing, with the same list rotated_sources() returned, so
    next cycle starts with the source *after* whichever one got first dibs this time."""
    if len(processed_order) <= 1:
        return
    ids = [s["id"] for s in get_salespeople()]
    first = processed_order[0]["id"]
    idx = ids.index(first) if first in ids else 0
    save_global_state(NEXT_SALESPERSON=ids[(idx + 1) % len(ids)])
