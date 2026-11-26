"""XDA Developers source via public RSS feeds (free, no auth required).

XDA has no public API and its WordPress REST API is disabled on the
portal (``/wp-json/`` returns 404), so this source reads the official
RSS feeds instead — the same feeds XDA itself publishes for readers:

- Portal (news, reviews, app coverage):
  https://www.xda-developers.com/feed/
- Forum "Android Apps and Games" (devs launching apps, users requesting
  features): https://xdaforums.com/f/android-apps-and-games.530/index.rss
- Forum "General Questions and Answers" (144K threads of "is there an
  app that..." pain): https://xdaforums.com/f/general-questions-and-answers.456/index.rss

RSS has no keyword search, so the fetcher pulls the latest entries from
each feed and filters locally by token overlap with the seed query
(the same post-filter pattern Hacker News uses for its title check).
Entries with no overlap at all are dropped; the rest keep an honest
relevance score so the pipeline's reranker and the daily aggregator's
pain-language filter decide what survives.

All feeds are fetched with stdlib urllib via lib.http; parsing is
stdlib xml.etree. No login, no token, no cookies.
"""

import html
import math
import re
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from . import http, log
from .relevance import token_overlap_relevance

# (feed label, RSS url). Forum ids verified live 2026-11-26.
FEEDS: list[tuple[str, str]] = [
    ("XDA Portal", "https://www.xda-developers.com/feed/"),
    ("Android Apps and Games", "https://xdaforums.com/f/android-apps-and-games.530/index.rss"),
    ("General Questions and Answers", "https://xdaforums.com/f/general-questions-and-answers.456/index.rss"),
]

# Namespaces used by the XDA feeds.
NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "slash": "http://purl.org/rss/1.0/modules/slash/",
}

# Minimum token-overlap relevance for an entry to survive local filtering.
# Lenient on purpose: the daily aggregator applies the pain-language regex
# as the real gate; this only drops entries with nothing to do with the seed.
MIN_RELEVANCE = 0.20

# Truncation for the plain-text excerpt carried into the pool.
EXCERPT_CHARS = 600


def _log(msg: str):
    log.source_log("XDA", msg)


def _strip_html(text: str) -> str:
    """Strip HTML tags and decode entities from an RSS content blob."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_pub_date(raw: str) -> Optional[str]:
    """Convert an RFC-2822 pubDate to YYYY-MM-DD, or None if unparsable."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return None


def _thread_id(link: str, guid: str) -> str:
    """Best-effort stable id: numeric thread id from the URL, else the guid."""
    match = re.search(r"\.(\d+)/?$", (link or "").rstrip("/"))
    if match:
        return match.group(1)
    if guid:
        return guid
    return ""


def _parse_feed_xml(text: str, feed_label: str) -> List[Dict[str, Any]]:
    """Parse one RSS document into raw entry dicts."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        _log(f"Could not parse {feed_label} feed XML: {e}")
        return []
    entries: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        content_el = item.find("content:encoded", NS)
        raw_content = content_el.text if content_el is not None else ""
        excerpt = _strip_html(raw_content)
        if not excerpt:
            excerpt = _strip_html(item.findtext("description") or "")
        author_el = item.find("dc:creator", NS)
        author = (author_el.text or "").strip() if author_el is not None else ""
        comments_el = item.find("slash:comments", NS)
        try:
            comments = int((comments_el.text or "0").strip())
        except ValueError:
            comments = 0
        entries.append({
            "id": _thread_id(link, (item.findtext("guid") or "").strip()),
            "title": title,
            "url": link,
            "author": author,
            "date": _parse_pub_date((item.findtext("pubDate") or "").strip()),
            "excerpt": excerpt[:EXCERPT_CHARS],
            "comments": comments,
            "forum": feed_label,
        })
    return entries


def search_xda(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> Dict[str, Any]:
    """Fetch the latest XDA portal + forum RSS entries.

    Args:
        topic: Search topic. Accepted for interface parity; the RSS feeds
            have no keyword search, so filtering happens in
            parse_xda_response against this query.
        from_date: Start date (YYYY-MM-DD). Accepted for interface parity;
            RSS has no date filter, so out-of-window entries are dropped
            downstream by normalization.
        to_date: End date (YYYY-MM-DD). Same as from_date.
        depth: 'quick', 'default', or 'deep'. Accepted for interface
            parity; the kept-item count is governed by the pipeline's
            per-stream limit, not here.

    Returns:
        Dict with an ``entries`` list of raw entry dicts. A feed that
        fails to fetch or parse is logged and skipped; if every feed
        fails the list is empty with an ``error`` note. Never raises for
        a single-feed failure, so the batch keeps running.
    """
    _log(f"Fetching {len(FEEDS)} RSS feeds")
    entries: List[Dict[str, Any]] = []
    errors: List[str] = []
    for label, url in FEEDS:
        try:
            text = http.request("GET", url, timeout=30, raw=True)
        except http.HTTPError as e:
            _log(f"{label} feed failed: {e}")
            errors.append(f"{label}: {e}")
            continue
        except Exception as e:
            _log(f"{label} feed failed: {e}")
            errors.append(f"{label}: {e}")
            continue
        if not isinstance(text, str) or not text.strip():
            _log(f"{label} feed returned empty body")
            errors.append(f"{label}: empty body")
            continue
        feed_entries = _parse_feed_xml(text, label)
        _log(f"{label}: {len(feed_entries)} entries")
        entries.extend(feed_entries)
    result: Dict[str, Any] = {"entries": entries}
    if errors:
        result["error"] = "; ".join(errors)
    return result


def parse_xda_response(response: Dict[str, Any], query: str = "") -> List[Dict[str, Any]]:
    """Filter XDA entries by query relevance and map to normalized items.

    Keeps entries whose title + excerpt have at least MIN_RELEVANCE token
    overlap with the query (or all entries when no query is given),
    most relevant first. The pipeline caps the stream length afterwards.

    Args:
        response: Raw dict from search_xda (an ``entries`` list).
        query: Original search query for token-overlap relevance scoring.

    Returns:
        List of item dicts ready for normalization.
    """
    entries = response.get("entries") or []
    scored: List[tuple[float, Dict[str, Any]]] = []
    for entry in entries:
        text = f"{entry.get('title', '')} {entry.get('excerpt', '')}".strip()
        if query:
            relevance = token_overlap_relevance(query, text)
            if relevance < MIN_RELEVANCE:
                continue
        else:
            relevance = 0.5
        comments = entry.get("comments") or 0
        engagement_boost = min(0.2, math.log1p(comments) / 40)
        scored.append((min(1.0, relevance + engagement_boost), entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    _log(f"Relevance filter kept {len(scored)}/{len(entries)} entries")
    items = []
    for i, (relevance, entry) in enumerate(scored):
        items.append({
            "id": str(entry.get("id") or f"XDA{i + 1}"),
            "title": entry.get("title", ""),
            "url": entry.get("url", ""),
            "author": entry.get("author", ""),
            "date": entry.get("date"),
            "engagement": {
                "comments": entry.get("comments") or 0,
            },
            "relevance": round(relevance, 2),
            "why_relevant": f"XDA thread about {entry.get('title', 'topic')[:60]}",
            "text": entry.get("excerpt", ""),
            "forum": entry.get("forum", ""),
        })
    return items
