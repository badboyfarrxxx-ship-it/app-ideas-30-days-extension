#!/usr/bin/env python3
"""Rolling-window aggregator for last30days batch JSON dumps.

Reads all `*.json` Reports under a directory tree (default `data/`) within
a rolling lookback window (default 30 days), merges them into a single
`Report`-shaped object, and either writes the merged JSON to disk or
runs `--emit=ideas` over it for end-of-day synthesis.

Used by the GitHub Actions workflows:

  - `batch.yml` runs `last30days.py --save-batch data/`
    several times per day, each writing one JSON file per topic/source-group
    under `data/<YYYY-MM-DD>/<HHMM>-<slug>.json`.
  - `daily-brief.yml` runs `aggregate_day.py --window-days 30 --emit ideas`
    once per day, merging the rolling pool into the final ideas brief.

By design this aggregator does NOT call any external APIs. It just merges
already-persisted batch artifacts. Re-running the pipeline would defeat
the purpose of the batched-collection model.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the lib/ package importable when this script is run directly.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib import ideas, schema  # noqa: E402


def _within_window(file_path: Path, window_start: datetime) -> bool:
    """Decide whether a batch file is recent enough to include.

    Prefers the file's modified time. Falls back to parsing the parent
    directory name as a YYYY-MM-DD date when mtime is unreliable (some
    CI checkouts don't preserve mtimes).
    """
    try:
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
        if mtime >= window_start:
            return True
    except OSError:
        pass
    parent = file_path.parent.name
    try:
        parsed = datetime.strptime(parent, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return parsed >= window_start
    except ValueError:
        return True  # No parseable hint — keep the file rather than silently drop


def _load_reports(root: Path, window_days: int) -> list[tuple[Path, schema.Report]]:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    files = sorted(root.rglob("*.json"))
    out: list[tuple[Path, schema.Report]] = []
    for path in files:
        if not _within_window(path, cutoff):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"[aggregate] Skipping {path}: {exc}\n")
            continue
        try:
            report = schema.report_from_dict(payload)
        except (KeyError, TypeError) as exc:
            sys.stderr.write(f"[aggregate] Skipping {path} (schema mismatch): {exc}\n")
            continue
        out.append((path, report))
    return out


def merge_reports(
    reports: list[schema.Report],
    *,
    aggregate_topic: str = "rolling 30-day pool",
    exclude_sources: frozenset[str] = frozenset(),
) -> schema.Report:
    """Union batches into one Report. Dedupe candidates / clusters / items by id.

    The merged `topic` is generic ("rolling 30-day pool") because batches
    may have spanned different seed topics. The synthesis prompt downstream
    treats the candidates as a heterogeneous pool to mine for ideas, which
    is exactly the point.

    `exclude_sources` drops a source from the pool entirely: candidates
    backed only by excluded sources, their items, and their errors.
    A source's error is kept only if the most recent batch that queried
    that source also failed, so an old failure does not outlive a fix.
    """
    if not reports:
        raise ValueError("merge_reports() requires at least one report")

    # Anchor metadata on the most recent report so query_plan / provider
    # match what the caller most recently configured.
    sorted_reports = sorted(reports, key=lambda r: r.generated_at)
    anchor = sorted_reports[-1]

    earliest = min(r.range_from for r in reports)
    latest = max(r.range_to for r in reports)

    def _excluded(cand: schema.Candidate) -> bool:
        backing = set(cand.sources) or {cand.source}
        return backing <= exclude_sources

    seen_candidates: dict[str, schema.Candidate] = {}
    for report in sorted_reports:
        for cand in report.ranked_candidates:
            if _excluded(cand):
                continue
            existing = seen_candidates.get(cand.candidate_id)
            if existing is None or (cand.final_score or 0.0) > (existing.final_score or 0.0):
                seen_candidates[cand.candidate_id] = cand
    merged_candidates = sorted(
        seen_candidates.values(),
        key=lambda c: c.final_score or 0.0,
        reverse=True,
    )

    seen_clusters: dict[str, schema.Cluster] = {}
    for report in sorted_reports:
        for cluster in report.clusters:
            if not any(cid in seen_candidates for cid in cluster.candidate_ids):
                continue
            existing = seen_clusters.get(cluster.cluster_id)
            if existing is None or cluster.score > existing.score:
                seen_clusters[cluster.cluster_id] = cluster
    merged_clusters = sorted(
        seen_clusters.values(), key=lambda c: c.score, reverse=True,
    )

    merged_items: dict[str, dict[str, schema.SourceItem]] = {}
    for report in sorted_reports:
        for source, items in report.items_by_source.items():
            if source in exclude_sources:
                continue
            bucket = merged_items.setdefault(source, {})
            for item in items:
                bucket[item.item_id] = item  # last-seen wins, keeps freshest copy
    items_by_source = {src: list(b.values()) for src, b in merged_items.items()}

    errors_by_source: dict[str, str] = {}
    for report in sorted_reports:
        for source in report.items_by_source:
            errors_by_source.pop(source, None)
        errors_by_source.update(report.errors_by_source)
    for source in exclude_sources:
        errors_by_source.pop(source, None)

    warnings: list[str] = []
    seen_warnings: set[str] = set()
    for report in sorted_reports:
        for warning in report.warnings:
            if warning not in seen_warnings:
                seen_warnings.add(warning)
                warnings.append(warning)

    artifacts = dict(anchor.artifacts)
    artifacts["aggregated_from"] = [r.topic for r in sorted_reports]
    artifacts["aggregated_count"] = len(sorted_reports)

    return schema.Report(
        topic=aggregate_topic,
        range_from=earliest,
        range_to=latest,
        generated_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        provider_runtime=anchor.provider_runtime,
        query_plan=anchor.query_plan,
        clusters=merged_clusters,
        ranked_candidates=merged_candidates,
        items_by_source=items_by_source,
        errors_by_source=errors_by_source,
        warnings=warnings,
        artifacts=artifacts,
    )


def prune_old_batches(root: Path, window_days: int) -> list[Path]:
    """Delete batch JSON files older than the window. Returns deleted paths."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    deleted: list[Path] = []
    for path in sorted(root.rglob("*.json")):
        if _within_window(path, cutoff):
            continue
        try:
            path.unlink()
            deleted.append(path)
        except OSError as exc:
            sys.stderr.write(f"[aggregate] Could not prune {path}: {exc}\n")
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Root directory of batch JSON dumps (default: data)",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Rolling lookback in days (default: 30)",
    )
    parser.add_argument(
        "--emit",
        choices=["json", "ideas"],
        default="ideas",
        help="Output format (default: ideas)",
    )
    parser.add_argument(
        "--out",
        help="Write the rendered output to this file. If omitted, writes to stdout.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="After aggregating, delete batch files older than the window.",
    )
    parser.add_argument(
        "--topic",
        default="rolling 30-day pool",
        help="Synthetic topic name for the merged report (default: 'rolling 30-day pool')",
    )
    parser.add_argument(
        "--top-candidates",
        type=int,
        default=ideas.DEFAULT_TOP_N,
        help=(
            "Maximum candidates to surface when --emit=ideas "
            f"(default: {ideas.DEFAULT_TOP_N}). Classification and ranking "
            "happen in the downstream Claude synthesis call."
        ),
    )
    parser.add_argument(
        "--exclude-sources",
        default="",
        help="Comma-separated sources to leave out of the merged pool (e.g. polymarket).",
    )
    args = parser.parse_args(argv)
    exclude = frozenset(s.strip() for s in args.exclude_sources.split(",") if s.strip())

    root = Path(args.data_dir).expanduser().resolve()
    if not root.exists():
        sys.stderr.write(f"[aggregate] Data dir not found: {root}\n")
        return 2

    pairs = _load_reports(root, args.window_days)
    if not pairs:
        sys.stderr.write(
            f"[aggregate] No batch files found in {root} within the last "
            f"{args.window_days} days.\n"
        )
        return 1

    merged = merge_reports(
        [r for _, r in pairs], aggregate_topic=args.topic, exclude_sources=exclude,
    )
    sys.stderr.write(
        f"[aggregate] Merged {len(pairs)} batch file(s) into one rolling pool "
        f"(candidates={len(merged.ranked_candidates)}, "
        f"clusters={len(merged.clusters)}).\n"
    )

    if args.emit == "json":
        rendered = json.dumps(schema.to_dict(merged), indent=2, sort_keys=True)
    else:
        selected = ideas.select_candidates(merged, top_n=args.top_candidates)
        rendered = ideas.render_brief(merged, selected)

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        sys.stderr.write(f"[aggregate] Wrote {args.emit} output to {out_path}\n")
    else:
        sys.stdout.write(rendered)

    if args.prune:
        deleted = prune_old_batches(root, args.window_days)
        sys.stderr.write(f"[aggregate] Pruned {len(deleted)} stale batch file(s).\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
