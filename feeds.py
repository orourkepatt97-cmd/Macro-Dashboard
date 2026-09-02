"""RSS/Atom news feeds for the dashboard's News tab.

Fetching and parsing only; results are cached with a short TTL. ``app.py``
renders the cards. Robustness contract: a single unreachable, slow, or
malformed feed yields zero items for that source and never raises — the tab
always renders.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from urllib.parse import quote_plus, urlsplit, urlunsplit

import feedparser
import requests
import streamlit as st

_CACHE_TTL = 15 * 60          # 15 minutes
_TIMEOUT = 12                 # seconds per feed
_PER_FEED_LIMIT = 25         # newest N entries kept from each feed
_HEADERS = {
    # A number of gov / news endpoints reject the default urllib/python UA.
    "User-Agent": "Mozilla/5.0 (compatible; macro-dashboard RSS reader)"
}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

# Dedupe tuning.
_TITLE_RATIO = 0.85  # difflib ratio at/above which two titles are "the same story"
# Title-match dedupe only fires when the two entries were published within this
# window. Without it, the Fed feeds' verbatim recurring headlines ("Federal
# Reserve issues FOMC statement", "Powell -- Semiannual Monetary Policy Report")
# from different dates would collapse into one.
_DUP_WINDOW_SEC = 3 * 24 * 60 * 60
# Trailing " - Publisher" / " | Publisher" that Google News appends; only
# stripped when there's real headline text (>=12 chars) in front of it.
_TITLE_SUFFIX_RE = re.compile(r"(?<=.{12})\s+[-–—|]\s+[^-–—|]{2,40}$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def _google_news(query: str) -> str:
    """Google News RSS search URL for a raw query string."""
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )


# Display label -> feed URL. Order here is only cosmetic; cards are sorted by date.
#
# Notes on the non-obvious ones:
#   * "NY Fed" uses the Liberty Street Economics feed — the newyorkfed.org
#     /rss endpoints now serve HTML, not XML.
#   * "Treasury" is a Google News site-scoped search: home.treasury.gov no
#     longer publishes a working native RSS feed (every documented path 404s).
FEEDS: dict[str, str] = {
    "Fed – Speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
    "Fed – Testimony": "https://www.federalreserve.gov/feeds/testimony.xml",
    "Fed – Monetary policy": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "NY Fed": "https://libertystreeteconomics.newyorkfed.org/feed/",
    "Treasury": _google_news("site:home.treasury.gov"),
    "Google News – Treasury yields": _google_news('"treasury yields"'),
    "Google News – Bond market": _google_news('"bond market"'),
    "Google News – Federal Reserve": _google_news('"Federal Reserve"'),
}

# Stable list of source labels for the sidebar filter.
SOURCES: list[str] = list(FEEDS)


def _entry_datetime(entry: feedparser.FeedParserDict) -> datetime | None:
    """Best available publish/update time as a tz-aware UTC datetime, or None."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_image(entry: feedparser.FeedParserDict) -> str | None:
    """First usable image URL for an entry, or None if the feed provides none."""
    for media in (entry.get("media_thumbnail"), entry.get("media_content")):
        if media:
            url = media[0].get("url")
            if url:
                return url

    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return link.get("href")

    for enclosure in entry.get("enclosures", []):
        if str(enclosure.get("type", "")).startswith("image/") and enclosure.get("href"):
            return enclosure.get("href")

    image = entry.get("image")
    if isinstance(image, dict) and image.get("href"):
        return image["href"]

    # Last resort: an <img> embedded in the summary / content HTML.
    blobs = [entry.get("summary", "")]
    blobs += [c.get("value", "") for c in entry.get("content", []) or []]
    for blob in blobs:
        match = _IMG_RE.search(blob or "")
        if match:
            return match.group(1)

    return None


def _clean_summary(raw: str, limit: int = 240) -> str:
    """Strip tags, decode HTML entities, collapse whitespace, truncate."""
    text = unescape(_TAG_RE.sub("", raw or ""))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _summary_for(raw: str, title: str) -> str:
    """Cleaned summary, or "" when it just repeats the headline.

    Compared on alphanumerics only (so a mangled ' - Publisher' separator still
    counts as a match), and also dropped when the summary merely *leads* with the
    full headline — Google News often appends the rest of a story cluster's
    headlines run together, which adds nothing readable.
    """
    summary = _clean_summary(raw)
    if not summary:
        return ""
    norm = lambda s: _WS_RE.sub(" ", _NON_ALNUM_RE.sub(" ", s.casefold())).strip()
    nsum, ntitle = norm(summary), norm(title)
    if nsum == ntitle or (len(ntitle) >= 20 and nsum.startswith(ntitle)):
        return ""
    return summary


def _normalize_url(url: str) -> str:
    """Canonical form for URL dedupe.

    Lowercases scheme + host, drops a leading ``www.``, and removes the entire
    query string and fragment along with any trailing slash or ``/amp``.
    Deliberately aggressive: news articles are path-addressed, so throwing away
    the query collapses the tracking-token (``utm_*``, ``fbclid`` …) and
    syndication variants that produce most of the duplicates.
    """
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    if not parts.netloc:
        return raw.lower()

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    if path.endswith("/amp") or path.endswith(".amp"):
        path = path[:-4]
    return urlunsplit(((parts.scheme or "https").lower(), host, path or "/", "", ""))


def _title_key(title: str) -> str:
    """Lowercased, de-punctuated headline with any ' - Publisher' suffix removed
    — the string fed to :class:`difflib.SequenceMatcher`."""
    text = _TITLE_SUFFIX_RE.sub("", (title or "").strip().lower())
    return _WS_RE.sub(" ", _NON_ALNUM_RE.sub(" ", text)).strip()


def _source_rank(label: str) -> int:
    """Lower = more authoritative. Primary institution feeds beat the Treasury
    site-search, which beats the general Google News aggregator queries."""
    if label.startswith("Google News"):
        return 2
    if label == "Treasury":
        return 1
    return 0


def _prefer(a: dict, b: dict) -> dict:
    """Pick which of two duplicate entries to keep: the earliest published
    version, then — when the timestamps are equal or missing — the one from the
    more authoritative source. The survivor backfills a missing image/summary
    from the other so collapsing a dupe never drops a thumbnail."""
    da, db = a["published"], b["published"]
    if da and db and da != db:
        winner, loser = (a, b) if da < db else (b, a)
    elif _source_rank(b["source"]) < _source_rank(a["source"]):
        winner, loser = b, a
    else:
        winner, loser = a, b

    if not winner.get("image") and loser.get("image"):
        winner = {**winner, "image": loser["image"]}
    if not winner.get("summary") and loser.get("summary"):
        winner = {**winner, "summary": loser["summary"]}
    return winner


def _within_window(a: datetime | None, b: datetime | None) -> bool:
    """True when both timestamps exist and are <= :data:`_DUP_WINDOW_SEC` apart."""
    return bool(a and b) and abs((a - b).total_seconds()) <= _DUP_WINDOW_SEC


def _dedupe(items: list[dict]) -> list[dict]:
    """Two passes: exact match on the normalized URL, then near-identical titles
    (``SequenceMatcher`` ratio >= :data:`_TITLE_RATIO`) among entries published
    within :data:`_DUP_WINDOW_SEC` of each other. The title pass is what
    collapses the heavily overlapping Google News queries against each other,
    plus the same wire story carried by multiple outlets."""
    by_url: dict[str, dict] = {}
    for item in items:
        key = _normalize_url(item["link"])
        existing = by_url.get(key)
        by_url[key] = item if existing is None else _prefer(existing, item)

    kept: list[dict] = []
    keys: list[tuple[str, datetime | None]] = []
    # Oldest first so the first entry seeded for each cluster is the original.
    for item in sorted(by_url.values(), key=lambda it: it["published"] or _EPOCH):
        tkey = _title_key(item["title"])
        when = item["published"]
        for i, (other_key, other_when) in enumerate(keys):
            if not tkey or not other_key or not _within_window(when, other_when):
                continue
            matcher = SequenceMatcher(None, tkey, other_key)
            if (
                matcher.real_quick_ratio() >= _TITLE_RATIO
                and matcher.quick_ratio() >= _TITLE_RATIO
                and matcher.ratio() >= _TITLE_RATIO
            ):
                kept[i] = _prefer(kept[i], item)
                keys[i] = (_title_key(kept[i]["title"]), kept[i]["published"])
                break
        else:
            kept.append(item)
            keys.append((tkey, when))
    return kept


def _fetch_one(source: str, url: str) -> list[dict]:
    """Fetch and parse one feed. Never raises — returns [] on any failure."""
    try:
        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception:  # noqa: BLE001 — any failure must be contained to this feed
        return []

    items: list[dict] = []
    for entry in parsed.entries[:_PER_FEED_LIMIT]:
        title = (entry.get("title") or "").strip()
        link = entry.get("link") or ""
        if not title or not link:
            continue
        items.append(
            {
                "source": source,
                "title": title,
                "link": link,
                "published": _entry_datetime(entry),
                "image": _entry_image(entry),
                "summary": _summary_for(entry.get("summary", ""), title),
            }
        )
    return items


@st.cache_data(ttl=_CACHE_TTL, show_spinner="Loading news…")
def fetch_all_news() -> dict:
    """All feeds, fetched concurrently.

    Returns ``{"items": [...], "counts": {source: n, ...}}`` where ``items`` is
    every article, deduplicated and sorted newest first (undated articles sink
    to the bottom). ``counts`` is the raw per-feed fetch count (before dedupe),
    so the UI can still flag a feed that came back empty.
    """
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        batches = list(pool.map(lambda kv: _fetch_one(*kv), FEEDS.items()))

    counts = {source: len(batch) for source, batch in zip(FEEDS, batches)}

    items = _dedupe([item for batch in batches for item in batch])
    items.sort(key=lambda it: it["published"] or _EPOCH, reverse=True)

    return {"items": items, "counts": counts}
