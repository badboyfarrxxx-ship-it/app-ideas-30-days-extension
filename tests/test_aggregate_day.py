"""Tests for the rolling-window aggregator."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "last30days" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import aggregate_day  # noqa: E402
from lib import schema  # noqa: E402


def _candidate(candidate_id: str, score: float = 0.5, source: str = "reddit") -> schema.Candidate:
    item = schema.SourceItem(
        item_id=f"{candidate_id}-i",
        source=source,
        title=f"Title {candidate_id}",
        body="Body",
        url=f"https://example.com/{candidate_id}",
    )
    return schema.Candidate(
        candidate_id=candidate_id,
        item_id=item.item_id,
        source=source,
        title=item.title,
        url=item.url,
        snippet=item.body,
        subquery_labels=["primary"],
        native_ranks={f"primary:{source}": 1},
        local_relevance=0.7,
        freshness=80,
        engagement=None,
        source_quality=1.0,
        rrf_score=0.02,
        rerank_score=score,
        final_score=score,
        sources=[source],
        source_items=[item],
    )


def _report(
    topic: str,
    candidates: list[schema.Candidate],
    *,
    range_from: str = "2026-04-01",
    range_to: str = "2026-04-30",
    generated_at: str = "2026-04-30T00:00:00+00:00",
) -> schema.Report:
    return schema.Report(
        topic=topic,
        range_from=range_from,
        range_to=range_to,
        generated_at=generated_at,
        provider_runtime=schema.ProviderRuntime(
            reasoning_provider="claude",
            planner_model="claude-sonnet-4-6",
            rerank_model="claude-sonnet-4-6",
        ),
        query_plan=schema.QueryPlan(
            intent="opportunity_mining",
            freshness_mode="rolling_30d",
            cluster_mode="entity",
            raw_topic=topic,
            subqueries=[
                schema.SubQuery(
                    label="primary",
                    search_query=topic,
                    ranking_query=f"What's discussed about {topic}?",
                    sources=["reddit"],
                )
            ],
            source_weights={"reddit": 1.0},
        ),
        clusters=[
            schema.Cluster(
                cluster_id=f"cluster-{c.candidate_id}",
                title=c.title,
                candidate_ids=[c.candidate_id],
                representative_ids=[c.candidate_id],
                sources=c.sources,
                score=c.final_score * 100,
            )
            for c in candidates
        ],
        ranked_candidates=candidates,
        items_by_source={
            c.source: [c.source_items[0]] for c in candidates
        },
        errors_by_source={},
    )


def _write_batch(root: Path, day: str, hour: str, report: schema.Report) -> Path:
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"{hour}-{report.topic.replace(' ', '-')}.json"
    out.write_text(json.dumps(schema.to_dict(report), indent=2, sort_keys=True))
    return out


class TestMergeReports(unittest.TestCase):
    def test_merge_unions_candidates_by_id(self):
        r1 = _report("topic-A", [_candidate("c1", 0.4), _candidate("c2", 0.6)])
        r2 = _report("topic-B", [_candidate("c2", 0.9), _candidate("c3", 0.5)])
        merged = aggregate_day.merge_reports([r1, r2])
        ids = sorted(c.candidate_id for c in merged.ranked_candidates)
        self.assertEqual(["c1", "c2", "c3"], ids)
        # Higher final_score wins for duplicate candidate_ids.
        c2 = next(c for c in merged.ranked_candidates if c.candidate_id == "c2")
        self.assertEqual(0.9, c2.final_score)

    def test_merge_uses_synthetic_topic_and_full_range(self):
        r1 = _report("topic-A", [_candidate("c1")], range_from="2026-04-01", range_to="2026-04-15")
        r2 = _report("topic-B", [_candidate("c2")], range_from="2026-04-10", range_to="2026-04-30")
        merged = aggregate_day.merge_reports([r1, r2], aggregate_topic="rolling pool")
        self.assertEqual("rolling pool", merged.topic)
        self.assertEqual("2026-04-01", merged.range_from)
        self.assertEqual("2026-04-30", merged.range_to)
        self.assertEqual(["topic-A", "topic-B"], merged.artifacts["aggregated_from"])
        self.assertEqual(2, merged.artifacts["aggregated_count"])

    def test_merge_dedupes_items_by_source(self):
        cand = _candidate("c1")
        r1 = _report("topic-A", [cand])
        r2 = _report("topic-B", [cand])  # same item_id
        merged = aggregate_day.merge_reports([r1, r2])
        self.assertEqual(1, len(merged.items_by_source["reddit"]))

    def test_merge_drops_excluded_sources(self):
        keep = _candidate("keep", source="reddit")
        drop = _candidate("drop", source="polymarket")
        merged = aggregate_day.merge_reports(
            [_report("seed", [keep, drop])],
            exclude_sources=frozenset({"polymarket"}),
        )
        self.assertEqual(["keep"], [c.candidate_id for c in merged.ranked_candidates])
        self.assertEqual(["cluster-keep"], [c.cluster_id for c in merged.clusters])
        self.assertNotIn("polymarket", merged.items_by_source)

    def test_merge_clears_error_fixed_by_a_later_batch(self):
        old = _report("old", [], generated_at="2026-04-29T00:00:00+00:00")
        old.errors_by_source = {"x": "HTTP 400: Bad Request"}
        new = _report(
            "new", [_candidate("x1", source="x")],
            generated_at="2026-04-30T00:00:00+00:00",
        )
        merged = aggregate_day.merge_reports([old, new])
        self.assertNotIn("x", merged.errors_by_source)

    def test_merge_keeps_error_from_latest_batch(self):
        ok = _report(
            "ok", [_candidate("x1", source="x")],
            generated_at="2026-04-29T00:00:00+00:00",
        )
        bad = _report("bad", [], generated_at="2026-04-30T00:00:00+00:00")
        bad.errors_by_source = {"x": "HTTP 400: Bad Request"}
        merged = aggregate_day.merge_reports([ok, bad])
        self.assertEqual("HTTP 400: Bad Request", merged.errors_by_source["x"])

    def test_merge_empty_list_raises(self):
        with self.assertRaises(ValueError):
            aggregate_day.merge_reports([])


class TestLoadReportsAndWindow(unittest.TestCase):
    def test_loads_only_files_within_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = datetime.now(tz=timezone.utc)
            recent = (today - timedelta(days=2)).strftime("%Y-%m-%d")
            stale = (today - timedelta(days=45)).strftime("%Y-%m-%d")
            recent_path = _write_batch(root, recent, "0900", _report("recent", [_candidate("r1")]))
            stale_path = _write_batch(root, stale, "0900", _report("stale", [_candidate("s1")]))
            # Force the stale file's mtime to actually be 45 days old so the
            # mtime check (preferred) drops it. Otherwise the directory-name
            # fallback alone will catch it.
            stale_ts = (today - timedelta(days=45)).timestamp()
            os.utime(stale_path, (stale_ts, stale_ts))

            pairs = aggregate_day._load_reports(root, window_days=30)
            paths = sorted(p.name for p, _ in pairs)
            self.assertEqual([recent_path.name], paths)

    def test_skips_unreadable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            day_dir = root / today
            day_dir.mkdir()
            (day_dir / "valid.json").write_text(
                json.dumps(schema.to_dict(_report("ok", [_candidate("c1")])))
            )
            (day_dir / "broken.json").write_text("{ not valid json")
            pairs = aggregate_day._load_reports(root, window_days=30)
            self.assertEqual(1, len(pairs))


class TestPruneOldBatches(unittest.TestCase):
    def test_prune_removes_stale_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = datetime.now(tz=timezone.utc)
            recent = (today - timedelta(days=2)).strftime("%Y-%m-%d")
            stale = (today - timedelta(days=45)).strftime("%Y-%m-%d")
            keep = _write_batch(root, recent, "0900", _report("keep", [_candidate("c1")]))
            drop = _write_batch(root, stale, "0900", _report("drop", [_candidate("c2")]))
            stale_ts = (today - timedelta(days=45)).timestamp()
            os.utime(drop, (stale_ts, stale_ts))

            deleted = aggregate_day.prune_old_batches(root, window_days=30)
            self.assertIn(drop, deleted)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())


class TestMainEntryPoint(unittest.TestCase):
    def test_main_emits_ideas_to_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            cand = _candidate("c1")
            cand.title = "I'd pay $30/month for an indie dev tool that does X"
            cand.snippet = cand.title
            cand.source_items[0].title = cand.title
            cand.source_items[0].body = "Honestly I'd pay for this."
            _write_batch(root, today, "0900", _report("seed", [cand]))

            out_path = Path(tmp) / "brief.md"
            rc = aggregate_day.main([
                "--data-dir", str(root),
                "--window-days", "30",
                "--emit", "ideas",
                "--out", str(out_path),
            ])
            self.assertEqual(0, rc)
            self.assertTrue(out_path.exists())
            content = out_path.read_text()
            self.assertIn("# last30days BRIEF", content)
            self.assertIn("WILL-PAY", content)
            # Brief now exposes raw candidates with URL + excerpt for
            # downstream Claude classification — confirm both are present.
            self.assertIn("https://example.com/c1", content)
            self.assertIn("Honestly I'd pay for this.", content)

    def test_main_returns_nonzero_when_no_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = aggregate_day.main(["--data-dir", tmp, "--window-days", "30"])
            self.assertEqual(1, rc)


if __name__ == "__main__":
    unittest.main()
