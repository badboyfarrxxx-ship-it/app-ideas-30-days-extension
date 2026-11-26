"""Product Hunt source via the official GraphQL API v2 (free developer token).

Uses https://api.producthunt.com/v2/api/graphql with a non-expiring
developer token sent as ``Authorization: Bearer <token>``. Get one at
https://api.producthunt.com/v2/oauth/applications (create an application,
copy the Developer Token at the bottom of its page). Set it as the
``PRODUCTHUNT_API_TOKEN`` secret; without it this source simply runs
empty, like the other optional-key sources.

Key limitation (from the official docs): the API has NO full-text search
of posts. Posts can only be filtered by topic slug, date range
(postedAfter/postedBefore) and ordering. This source therefore pulls the
top posts by votes inside the requested window and lets the pipeline's
relevance scoring + the daily aggregator's pain-language filter decide
what survives — the same approach other research tools take. Product
Hunt's value here is LAUNCH / competitor signal (what just shipped, how
it landed, what commenters wish it did) rather than raw PAIN posts.

Rate limits: Product Hunt enforces a complexity budget (~6250 points per
15 minutes). A single pull here is one page of at most 50 posts with up
to 3 top comments each — comfortably inside budget, and the batch runs
this source once per seed (it is in pipeline.RAW_TOPIC_BOUND_SOURCES,
so subqueries collapse to a single fetch per run).
"""

import math
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import http, log
from .relevance import token_overlap_relevance

GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"

DEPTH_CONFIG = {
    "quick": 20,
    "default": 30,
    "deep": 50,
}

# Top comments pulled inline per post. Kept small: each comment edge adds
# to the query's complexity cost.
COMMENTS_PER_POST = 3

_POSTS_QUERY = """
query Posts($first: Int!, $postedAfter: DateTime, $postedBefore: DateTime) {
  posts(first: $first, order: VOTES, postedAfter: $postedAfter, postedBefore: $postedBefore) {
    edges {
      node {
        id
        name
        tagline
        description
        url
        votesCount
        commentsCount
        createdAt
        topics(first: 5) {
          edges {
            node {
              name
              slug
            }
          }
        }
        user {
          name
          username
        }
        comments(first: 3, order: VOTES) {
          edges {
            node {
              body
              votesCount
              createdAt
              user {
                name
                username
              }
            }
          }
        }
      }
    }
  }
}
"""


def _log(msg: str):
    log.source_log("ProductHunt", msg)


def _date_to_iso(date_str: str, end_of_day: bool = False) -> str:
    """Convert YYYY-MM-DD to an ISO-8601 UTC timestamp for the API."""
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    return f"{date_str}{suffix}"


def _iso_to_date(iso_str: str) -> Optional[str]:
    """Convert an ISO-8601 timestamp to YYYY-MM-DD, or None if unparsable."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return iso_str[:10] if len(iso_str) >= 10 else None


def search_producthunt(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = "",
) -> Dict[str, Any]:
    """Pull the top Product Hunt launches by votes inside the date window.

    Args:
        topic: Search topic. Accepted for interface parity with the other
            sources but NOT sent to the API — Product Hunt has no keyword
            search. It is used by parse_producthunt_response for relevance
            scoring.
        from_date: Start date (YYYY-MM-DD, inclusive).
        to_date: End date (YYYY-MM-DD, inclusive).
        depth: 'quick', 'default', or 'deep' (controls post count).
        token: Product Hunt developer token (PRODUCTHUNT_API_TOKEN).

    Returns:
        Dict with the raw GraphQL ``data`` payload, or ``{"posts": [],
        "error": ...}`` when the token is missing or the request fails.
        A missing token is a soft empty, not an exception, so the batch
        keeps running without this source.
    """
    count = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    if not token:
        _log("No PRODUCTHUNT_API_TOKEN set — source runs empty")
        return {"posts": [], "error": "missing PRODUCTHUNT_API_TOKEN"}

    _log(f"Fetching top {count} launches by votes ({from_date}..{to_date})")
    variables = {
        "first": count,
        "postedAfter": _date_to_iso(from_date),
        "postedBefore": _date_to_iso(to_date, end_of_day=True),
    }
    try:
        response = http.request(
            "POST",
            GRAPHQL_URL,
            headers={"Authorization": f"Bearer {token}"},
            json_data={"query": _POSTS_QUERY, "variables": variables},
            timeout=30,
        )
    except http.HTTPError as e:
        _log(f"GraphQL request failed: {e}")
        return {"posts": [], "error": str(e)}
    except Exception as e:
        _log(f"GraphQL request failed: {e}")
        return {"posts": [], "error": str(e)}

    if isinstance(response, dict) and response.get("errors"):
        _log(f"GraphQL returned errors: {response['errors']}")
        return {"posts": [], "error": str(response["errors"])[:500]}
    return response


def _comment_excerpts(comment_edges: list) -> list[dict[str, Any]]:
    """Map GraphQL comment edges to the shared top-comment shape."""
    comments = []
    for edge in comment_edges or []:
        node = edge.get("node") or {}
        body = (node.get("body") or "").strip()
        if not body:
            continue
        user = node.get("user") or {}
        comments.append({
            "author": user.get("username") or user.get("name") or "",
            "text": body[:400],
            "votes": node.get("votesCount") or 0,
        })
    return comments


def parse_producthunt_response(response: Dict[str, Any], query: str = "") -> List[Dict[str, Any]]:
    """Parse a Product Hunt GraphQL response into normalized item dicts.

    No hard keyword filter is applied: Product Hunt is a launch feed, and
    its items enter the pool as LAUNCH / competitor evidence. Relevance is
    scored by blending vote rank with token overlap against the query so
    the pipeline's reranker can downrank off-topic launches.

    Args:
        response: Raw GraphQL response dict from search_producthunt.
        query: Original search query for token-overlap relevance scoring.

    Returns:
        List of item dicts ready for normalization.
    """
    data = response.get("data") or {}
    posts = (data.get("posts") or {}).get("edges") or []
    _log(f"Parsing {len(posts)} launches")
    items = []

    for i, edge in enumerate(posts):
        node = edge.get("node") or {}
        name = (node.get("name") or "").strip()
        tagline = (node.get("tagline") or "").strip()
        description = (node.get("description") or "").strip()
        votes = node.get("votesCount") or 0
        comments_count = node.get("commentsCount") or 0
        topics = [
            (t.get("node") or {}).get("name", "")
            for t in ((node.get("topics") or {}).get("edges") or [])
        ]
        topics = [t for t in topics if t]
        user = node.get("user") or {}
        author = user.get("username") or user.get("name") or ""
        top_comments = _comment_excerpts(
            ((node.get("comments") or {}).get("edges") or [])
        )

        title = f"{name}: {tagline}" if tagline else name
        content = " ".join(part for part in [name, tagline, description, " ".join(topics)] if part)

        # Relevance: blend vote rank with token-overlap content matching,
        # mirroring the Hacker News scorer.
        rank_score = max(0.3, 1.0 - (i * 0.02))
        engagement_boost = min(0.2, math.log1p(votes) / 40)
        if query:
            content_score = token_overlap_relevance(query, content)
            relevance = min(1.0, 0.6 * rank_score + 0.4 * content_score + engagement_boost)
        else:
            relevance = min(1.0, rank_score * 0.7 + engagement_boost + 0.1)

        items.append({
            "id": str(node.get("id") or f"PH{i + 1}"),
            "title": title or f"Product Hunt launch {i + 1}",
            "url": node.get("url") or "",
            "author": author,
            "date": _iso_to_date(node.get("createdAt") or ""),
            "engagement": {
                "votes": votes,
                "comments": comments_count,
            },
            "relevance": round(relevance, 2),
            "why_relevant": f"Product Hunt launch: {title[:60]}",
            "text": description,
            "tagline": tagline,
            "topics": topics,
            "top_comments": top_comments,
        })

    return items
