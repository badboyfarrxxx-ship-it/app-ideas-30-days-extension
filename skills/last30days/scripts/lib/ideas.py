"""Evidence brief renderer for end-of-day app-idea synthesis.

Selects the top-N candidates from a Report by pipeline `final_score` and
renders them as a deterministic markdown brief. Classification (PAIN /
WILL-PAY / LAUNCH / WORKFLOW), ranking, and idea articulation all happen
downstream in the Claude Code synthesis call (see
`.github/workflows/daily-brief.yml`), which has the full text of every
candidate in context and reasons over it directly.

Pure-Python, no external API calls. Deterministic plumbing only —
judgment lives in the synthesis prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from . import schema


DEFAULT_TOP_N = 40
EXCERPT_WIDTH = 280

# Sources added to the pipeline later score lower than the long-running
# ones (their evidence matches the pain-phrase seeds less directly), so a
# pure top-N by final_score never surfaces them: the 2026-11-27 brief had
# zero Product Hunt or XDA candidates in its top 40 even though both
# sources were healthy and flowing. Guarantee each of them a small number
# of slots so their finds reach the synthesis prompt and can compete for
# the top ideas. Everything else still ranks by score, and no source is
# removed.
FLOOR_SOURCES: dict[str, int] = {"producthunt": 3, "xda": 3}


@dataclass
class CandidateView:
    candidate_id: str
    title: str
    excerpt: str
    source: str
    container: str
    url: str
    engagement: int | float | None
    published_at: str | None
    final_score: float


def _engagement_total(engagement: dict[str, float | int] | None) -> int | float | None:
    if not engagement:
        return None
    nums = [v for v in engagement.values() if isinstance(v, (int, float))]
    return sum(nums) if nums else None


def _excerpt(text: str, width: int = EXCERPT_WIDTH) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[:width].rstrip() + "…"


def _view(candidate: schema.Candidate) -> CandidateView:
    primary = schema.candidate_primary_item(candidate)
    body = ""
    container = ""
    engagement: int | float | None = None
    published_at: str | None = None
    source = candidate.source or ""
    url = candidate.url or ""
    if primary:
        body = primary.body or primary.snippet or candidate.snippet or ""
        container = primary.container or ""
        engagement = _engagement_total(primary.engagement)
        published_at = primary.published_at
        source = source or primary.source
        url = url or primary.url
    if not body:
        body = candidate.snippet or ""
    if engagement is None and candidate.engagement is not None:
        engagement = candidate.engagement
    return CandidateView(
        candidate_id=candidate.candidate_id,
        title=candidate.title or "(untitled)",
        excerpt=_excerpt(body),
        source=source,
        container=container,
        url=url,
        engagement=engagement,
        published_at=published_at,
        final_score=float(candidate.final_score or 0.0),
    )


def select_candidates(
    report: schema.Report, *, top_n: int = DEFAULT_TOP_N,
) -> list[CandidateView]:
    """Return the top-N candidates by pipeline final_score as CandidateView.

    The bulk of the list is the global top-N by score. In addition, each
    source in FLOOR_SOURCES is guaranteed at least that many slots (when
    it has candidates in the pool), filled with its highest-scored
    candidates. The result stays sorted by final_score.
    """
    ranked = sorted(
        report.ranked_candidates,
        key=lambda c: float(c.final_score or 0.0),
        reverse=True,
    )
    protected: dict[str, schema.Candidate] = {}
    for source, floor in FLOOR_SOURCES.items():
        mine = [c for c in ranked if source in schema.candidate_sources(c)]
        for cand in mine[:floor]:
            protected[cand.candidate_id] = cand
    protected_ids = set(protected)
    selected = list(protected.values())
    fill = [c for c in ranked if c.candidate_id not in protected_ids]
    selected.extend(fill[: max(0, top_n - len(selected))])
    if len(selected) > top_n:
        # Floors exceed the list size; keep the highest-scored protected.
        selected.sort(key=lambda c: float(c.final_score or 0.0), reverse=True)
        selected = selected[:top_n]
    selected.sort(key=lambda c: float(c.final_score or 0.0), reverse=True)
    return [_view(c) for c in selected]


def render_brief(
    report: schema.Report, candidates: Iterable[CandidateView],
) -> str:
    """Render the evidence brief Claude will synthesize into ranked ideas."""
    candidates = list(candidates)
    healthy_sources = sorted(s for s, items in report.items_by_source.items() if items)
    failing_sources = sorted(report.errors_by_source.items())

    lines: list[str] = [
        f"# last30days BRIEF · {report.topic}",
        "",
        f"- Date range: {report.range_from} to {report.range_to}",
        f"- Generated: {report.generated_at}",
        f"- Candidates in pool: {len(report.ranked_candidates)}",
        f"- Candidates surfaced (top {len(candidates)}): {len(candidates)}",
        f"- Healthy sources ({len(healthy_sources)}): "
        + (", ".join(healthy_sources) if healthy_sources else "_none_"),
        "",
    ]

    if failing_sources:
        lines.extend(["## SOURCE HEALTH (errors observed in the rolling pool)", ""])
        for source, message in failing_sources:
            clean = " ".join((message or "no message").split())
            if len(clean) > 200:
                clean = clean[:200].rstrip() + "…"
            lines.append(f"- **{source}**: {clean}")
        lines.append("")
        lines.append(
            "_If a source you expect to inform a domain (e.g. X for live "
            "discourse, GitHub for new launches) is in this list, mention "
            "in your assessment that today's brief is missing that signal._"
        )
        lines.append("")

    lines.extend([
        "## SYNTHESIS INSTRUCTIONS (Claude Code, read carefully)",
        "",
        "You are receiving a deterministic top-N pool of social/web evidence",
        "from the last30days engine. The Python layer did NOT classify these",
        "— that is your job. Produce a ranked list of **buildable app ideas**",
        "with these rules:",
        "",
        "1. For each candidate, decide whether it shows one or more of:",
        "   - **PAIN**: an unmet need / explicit complaint / wish for a tool.",
        "   - **WILL-PAY**: a willingness-to-pay marker (named price, "
        "\"I'd pay\", current $ spend on a substitute).",
        "   - **LAUNCH**: someone shipped a relevant product. Use this as",
        "     adjacency evidence (\"X exists, but nobody has done Y for Z\"),",
        "     not as \"build X\".",
        "   - **WORKFLOW**: a stitched-together-tools workflow implying a",
        "     tooling gap.",
        "",
        "   Drop any candidate that shows none of these. When you cite a",
        "   candidate as evidence, quote the matching sentence verbatim from",
        "   its excerpt or title — do not paraphrase.",
        "",
        "2. Rank surviving candidates by **evidence strength × engagement ×",
        "   recency**. A 5000-upvote organic complaint thread outweighs a",
        "   3-upvote post with a literal \"I wish there was an app\" phrase.",
        "   Use the `eng:` and date fields on each candidate.",
        "",
        "3. Cluster duplicates: if two candidates describe the same",
        "   opportunity, merge them into one idea citing both URLs.",
        "",
        "4. Output 3-7 distinct app ideas, ordered by combined score. For",
        "   each idea: name the unmet need (one sentence), the proposed app",
        "   (one sentence), then bulleted evidence with URLs from this",
        "   brief.",
        "",
        "5. Flag any idea where you can name 2+ existing competitors —",
        "   downrank it and say so explicitly. We do NOT surface crowded",
        "   markets.",
        "",
        "6. Prefer **PAIN + WILL-PAY** combinations. PAIN alone is",
        "   interesting; PAIN + WILL-PAY is investable.",
        "",
        "7. Do not invent evidence. Every cited URL must appear in this",
        "   brief. If a candidate has no excerpt, you may not cite it.",
        "",
        "8. If a SOURCE HEALTH section appears above, your honest-assessment",
        "   line MUST mention which sources are unreliable today and how",
        "   that biases the brief (e.g. \"X is failing, so live launch",
        "   chatter is under-represented\").",
        "",
        "9. End with a one-line honest assessment: how strong is this batch,",
        "   and what topic queries should the user run next.",
        "",
        "## CANDIDATES",
        "",
    ])

    if not candidates:
        lines.extend([
            "_No candidates surfaced from the rolling pool._",
            "",
            "This usually means the topic was too narrow or the engine",
            "did not surface idea-relevant content. Suggest the user re-run",
            "with a broader topic or a community-seeded query (e.g. add",
            "`--subreddits r/SideProject,r/microsaas`).",
        ])
        return "\n".join(lines) + "\n"

    for idx, c in enumerate(candidates, start=1):
        eng_str = f" [eng:{int(c.engagement)}]" if c.engagement else ""
        date_str = f" ({c.published_at})" if c.published_at else ""
        container_str = f" / {c.container}" if c.container else ""
        lines.extend([
            f"### {idx}. {c.title}",
            "",
            f"- Source: {c.source}{container_str}{eng_str}{date_str}",
            f"- Score: {c.final_score:.2f}",
            f"- URL: {c.url}",
        ])
        if c.excerpt:
            lines.append("- Excerpt:")
            lines.append(f"  > {c.excerpt}")
        lines.append("")

    return "\n".join(lines) + "\n"
