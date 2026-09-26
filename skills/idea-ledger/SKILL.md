---
name: idea-ledger
description: Maintain a CSV ledger of every app idea the daily-brief pipeline has ever surfaced, along with its fate (triaged, feasibility-tested, validated, building, shipped, abandoned). Prevents the user re-litigating the same idea three weeks later when it surfaces again from a different seed. Use whenever the user says "log this idea", "have I seen this idea before", "what did we kill last month", "show the ledger", "/idea-ledger", or mentions tracking ideas across briefs. The ledger is the source of truth for "what's the status of idea X" — the GitHub PR history is too noisy. Research/data-only: do not synthesize new ideas or make build decisions from this skill.
argument-hint: <subcommand> — one of `add <brief.md>`, `list [--status <s>]`, `status <id> <new-status>`, `dedup <title>`
allowed-tools: Read, Bash, Write
user-invocable: true
---

# Idea Ledger

A flat-file CSV ledger that lives alongside the daily briefs (default location: `ledger.csv` in the current working directory — typically the `App-Ideas` private repo). Append-mostly: rows are added when ideas first surface, and a small set of fields update over time as ideas move through the funnel.

## Schema

| Column | Type | Notes |
|---|---|---|
| `id` | string | `YYYYMMDD-<slug-of-title>` — stable, unique, sortable |
| `date_surfaced` | YYYY-MM-DD | First daily-brief the idea appeared in |
| `title` | string | The "## N. <Title>" line from `ideas.md`, stripped of numbering |
| `platform` | enum | `Android` / `iOS` / `Both` / `Unknown` |
| `source_brief` | path | Relative path to the brief that first surfaced it |
| `status` | enum | See state machine below |
| `score` | int | 1–10 if feasibility-tested, else blank |
| `confidence` | enum | `HIGH` / `MEDIUM` / `LOW` if feasibility-tested |
| `killer_assumption` | string | Filled when status moves to `*_fail` or `abandoned` |
| `last_updated` | YYYY-MM-DD | Auto-updated on status change |
| `notes` | string | Free-text, semicolon-separated |

### Status state machine

```
surfaced
   ├── triaged_no              (brief-triage: OBVIOUS NO)
   ├── triaged_keep            (brief-triage: WORTH FEASIBILITY)
   │      ├── feasibility_pass (app-idea-feasibility: score ≥ 7 + PASS)
   │      │      ├── validation_pass    (real-world test passed)
   │      │      │      ├── building
   │      │      │      │      └── shipped
   │      │      │      └── abandoned
   │      │      └── validation_fail
   │      └── feasibility_fail
   └── needs_info              (brief-triage: NEEDS INFO — re-check later)
```

Status only ever moves forward, never backward. If you reconsider, append a new row with a fresh id and link the prior row in `notes`.

## Workflow

The skill is invoked with a subcommand. The Python helper does the file I/O; the skill markdown is the contract that tells you which subcommand to call when.

### `add <brief.md>` — ingest a new brief

For each `## N. <Title>` heading in the brief, append a row with `status=surfaced` and pull `platform` from the brief's `**Platform:**` line. Before appending, **run a dedup check** against existing rows: if a near-duplicate title exists (substring match either direction, or ≥80% character overlap on lowercase titles), flag it and prompt the user to confirm or merge.

```bash
uv run --frozen python3 skills/idea-ledger/scripts/ledger.py add \
  --brief path/to/ideas/2026-05-20/ideas.md \
  --ledger ledger.csv
```

The script prints any flagged dedup candidates and exits non-zero unless the user passes `--force` (each dedup match must be reviewed manually). `--skip-duplicates` adds only the unflagged ideas and notes each repeat on its matching existing row, which keeps that row's status. The daily-brief workflow uses it.

### `list [--status <s>]` — show ledger state

```bash
uv run --frozen python3 skills/idea-ledger/scripts/ledger.py list \
  --ledger ledger.csv \
  --status feasibility_pass
```

Default (no `--status`): show all rows, sorted by `last_updated` desc, formatted as a markdown table.

### `status <id> <new-status>` — move an idea forward

```bash
uv run --frozen python3 skills/idea-ledger/scripts/ledger.py status \
  --ledger ledger.csv \
  --id 20260520-async-meeting-prep \
  --new-status feasibility_fail \
  --killer-assumption "WTP quote was a hedge, not a commitment" \
  --score 4 \
  --confidence MEDIUM
```

Validates the transition against the state machine. Rejects backward moves.

### `dedup <title>` — pre-check before ingesting a new idea

```bash
uv run --frozen python3 skills/idea-ledger/scripts/ledger.py dedup \
  --ledger ledger.csv \
  --title "Pre-session prep doc for solo therapists"
```

Outputs the top 3 candidate matches with their ids and statuses. The user reads this *before* writing a feasibility verdict to avoid wasting a research pass on an already-killed idea.

## Top-level rules

- **Append-mostly.** Status updates are the only in-place edits. Never edit `title`, `id`, `date_surfaced`, or `source_brief` after creation.
- **One ledger per founder, not per brief.** The CSV is shared across all daily briefs. Don't shard by month.
- **Dedup is a prompt, not an auto-merge.** If two ideas look the same, surface them; let the human decide. False merges destroy history.
- **Killer assumption is required when killing.** Status moves to `triaged_no`, `feasibility_fail`, `validation_fail`, or `abandoned` MUST set `killer_assumption`. The whole point of the ledger is to remember *why* — without it the ledger is just a list.
- **Don't gate ingestion on feasibility.** Even ideas you triage_no immediately get a row. That's how you spot the same dead idea surfacing 4 times across 4 different seeds — a signal the seeds need an audit (`/seed-audit`).
- **Don't create the ledger automatically.** If `ledger.csv` doesn't exist, ask the user to confirm the path before initializing it. Accidentally creating a ledger in the wrong dir produces orphan data.
