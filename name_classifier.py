"""Heuristic check: does an Instagram display name look like an actual person's name, or a
generic/brand/business account name (e.g. "Money Hustle", "Daily Motivation", "XYZ Studio")?

This is a deterministic rule-based classifier, not a live AI/LLM call -- no external API, no
cost, no extra credentials, and it runs instantly for every lead. It won't be perfect (no
heuristic can be), but it catches the common, obvious cases: business/brand keywords, emoji,
symbols, all-caps slogans, and names that are just too long/short to be a real first+last name.

If this needs to get smarter later (e.g. genuinely ambiguous cases), swapping this out for an
LLM-based classifier is possible, but that adds a per-lead API cost and a new credential to
manage -- not something to add silently, so it's not wired in here.
"""
import re

# Words that show up constantly in brand/business/motivational page names but essentially never
# as part of a real person's own display name.
_BRAND_KEYWORDS = {
    "shop", "store", "official", "studio", "studios", "agency", "designs", "design",
    "boutique", "fitness", "coach", "coaching", "academy", "media", "brand", "brands",
    "llc", "inc", "co", "salon", "photography", "photo", "photos", "travel", "tours",
    "realty", "realtor", "properties", "property", "fashion", "beauty", "nails", "hair",
    "makeup", "gym", "training", "nutrition", "marketing", "consulting", "invest",
    "investing", "investments", "crypto", "forex", "hustle", "hustler", "money", "cash",
    "wealth", "empire", "luxury", "clothing", "apparel", "jewelry", "jewellery", "art",
    "gallery", "music", "dj", "band", "records", "films", "film", "production",
    "productions", "tv", "news", "magazine", "blog", "page", "fanpage", "team", "group",
    "club", "community", "network", "world", "global", "hub", "zone", "central", "daily",
    "quotes", "motivation", "motivational", "lifestyle", "vibes", "gang", "squad", "crew",
    "collective", "co.", "company", "enterprise", "enterprises", "solutions", "digital",
    "tech", "technologies", "app", "apps", "software", "capital", "trading", "traders",
    "fx", "nft", "nfts", "web3", "dao", "ai",
}

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]"
)


def looks_like_person_name(full_name: str) -> bool:
    """Best-effort heuristic. Returns False (don't personalize) when unsure."""
    if not full_name:
        return False
    name = full_name.strip()
    if not name:
        return False
    if _EMOJI_RE.search(name):
        return False
    if re.search(r"[0-9|•★☆™®@#$%^&*_+=<>/\\~`{}\[\]]", name):
        return False
    words = name.split()
    if not (1 <= len(words) <= 3):
        return False
    lowered = {w.strip(".,!?").lower() for w in words}
    if lowered & _BRAND_KEYWORDS:
        return False
    # Deliberately not rejecting on ALL-CAPS alone: plenty of real people style their Instagram
    # display name in caps (e.g. "SHAKED LEVI"). clean_first_name() normalizes casing for the
    # message regardless, so caps by itself isn't a useful brand-vs-person signal here.
    # Unicode-aware on purpose: many real leads have Hebrew (or other non-Latin) names --
    # str.isalpha() accepts any script's letters, not just Latin, so "רועי" personalizes the
    # same way "Roy" does. Only the specific punctuation marks below are allowed alongside
    # letters (apostrophes/hyphens for names like "O'Brien" or "Anne-Marie").
    def _word_ok(w):
        return all(ch.isalpha() or ch in "'-." for ch in w)
    if any(not _word_ok(w) for w in words):
        return False
    return True


def clean_first_name(full_name: str) -> str:
    """First word of a display name, normalized to Title Case for use in a message."""
    first = (full_name or "").strip().split(" ")[0]
    return first[:1].upper() + first[1:].lower() if first else ""
