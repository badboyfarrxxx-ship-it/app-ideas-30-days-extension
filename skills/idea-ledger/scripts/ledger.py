#!/usr/bin/env python3
"""CSV-backed ledger for app ideas surfaced by the daily-brief pipeline.

Four subcommands: add, list, status, dedup. See ../SKILL.md for the
contract and state machine. This script is intentionally simple — no
dependencies beyond the stdlib, and no LLM reasoning. The skill markdown
decides when to call what; this script does the file I/O.

Usage examples:

  ledger.py add    --brief ideas/2026-05-20/ideas.md  --ledger ledger.csv
  ledger.py list   --ledger ledger.csv [--status <s>]
  ledger.py status --ledger ledger.csv --id <id> --new-status <s>
  ledger.py dedup  --ledger ledger.csv --title "<idea title>"
"""
from __future__ import annotations

import argparse
import csv
import difflib
import re
import sys
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

COLUMNS = [
    "id",
    "date_surfaced",
    "title",
    "platform",
    "source_brief",
    "status",
    "score",
    "confidence",
    "killer_assumption",
    "last_updated",
    "notes",
]

VALID_STATUSES = {
    "surfaced",
    "triaged_no",
    "triaged_keep",
    "needs_info",
    "feasibility_pass",
    "feasibility_fail",
    "validation_pass",
    "validation_fail",
    "building",
    "shipped",
    "abandoned",
}

# state machine: status -> set of statuses it can move to
TRANSITIONS = {
    "surfaced": {"triaged_no", "triaged_keep", "needs_info"},
    "needs_info": {"triaged_no", "triaged_keep"},
    "triaged_keep": {"feasibility_pass", "feasibility_fail"},
    "feasibility_pass": {"validation_pass", "validation_fail", "abandoned"},
    "validation_pass": {"building", "abandoned"},
    "building": {"shipped", "abandoned"},
    # terminal states
    "triaged_no": set(),
    "feasibility_fail": set(),
    "validation_fail": set(),
    "shipped": set(),
    "abandoned": set(),
}

KILL_STATUSES = {"triaged_no", "feasibility_fail", "validation_fail", "abandoned"}


@dataclass
class Row:
    id: str = ""
    date_surfaced: str = ""
    title: str = ""
    platform: str = "Unknown"
    source_brief: str = ""
    status: str = "surfaced"
    score: str = ""
    confidence: str = ""
    killer_assumption: str = ""
    last_updated: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60] or "untitled"


def _read_ledger(path: Path) -> list[Row]:
    if not path.exists():
        return []
    rows: list[Row] = []
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for raw in reader:
            rows.append(Row(**{c: (raw.get(c) or "") for c in COLUMNS}))
    return rows


def _write_ledger(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())


def _parse_brief(brief_path: Path) -> list[Row]:
    text = brief_path.read_text(encoding="utf-8")
    blocks = re.split(r"^## \d+\.\s*", text, flags=re.MULTILINE)
    if len(blocks) <= 1:
        return []
    surfaced = date.today().isoformat()
    out: list[Row] = []
    for chunk in blocks[1:]:
        title = chunk.splitlines()[0].strip()
        platform_match = re.search(r"\*\*Platform:\*\*\s*(\S[^\n]*)", chunk)
        platform_raw = (platform_match.group(1) if platform_match else "Unknown").strip()
        # Strip trailing markdown like " | iOS | Both" template artifacts
        platform = platform_raw.split("|")[0].strip() if "|" in platform_raw else platform_raw
        if not platform:
            platform = "Unknown"
        out.append(
            Row(
                id=f"{surfaced.replace('-', '')}-{_slugify(title)}",
                date_surfaced=surfaced,
                title=title,
                platform=platform,
                source_brief=str(brief_path),
                status="surfaced",
                last_updated=surfaced,
            )
        )
    return out


def _dedup_candidates(title: str, rows: list[Row], top_n: int = 3) -> list[tuple[Row, float]]:
    norm = title.lower().strip()
    scored: list[tuple[Row, float]] = []
    for row in rows:
        ratio = difflib.SequenceMatcher(None, norm, row.title.lower().strip()).ratio()
        # boost on substring containment
        if norm in row.title.lower() or row.title.lower() in norm:
            ratio = max(ratio, 0.85)
        if ratio >= 0.55:
            scored.append((row, ratio))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_n]


def cmd_add(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger).expanduser()
    brief_path = Path(args.brief).expanduser()
    if not brief_path.exists():
        print(f"[ledger] Brief not found: {brief_path}", file=sys.stderr)
        return 2
    if not ledger_path.exists() and not args.init:
        print(
            f"[ledger] {ledger_path} does not exist. Re-run with --init to create it.",
            file=sys.stderr,
        )
        return 2

    existing = _read_ledger(ledger_path)
    new_rows = _parse_brief(brief_path)
    if not new_rows:
        print(f"[ledger] No ideas found in {brief_path} (expected `## N. <Title>` headings).", file=sys.stderr)
        return 1

    flagged: list[tuple[Row, list[tuple[Row, float]]]] = []
    for candidate in new_rows:
        matches = _dedup_candidates(candidate.title, existing)
        if matches:
            flagged.append((candidate, matches))

    if flagged and args.skip_duplicates:
        # Record the repeat on the best-matching existing row instead of adding
        # a second row for the same idea, so its status (e.g. triaged_no) holds.
        flagged_ids = {candidate.id for candidate, _ in flagged}
        for candidate, matches in flagged:
            match = matches[0][0]
            seen = candidate.date_surfaced
            match.last_updated = seen
            note = f"Seen again {seen} as '{candidate.title}'"
            match.notes = f"{match.notes}; {note}".lstrip("; ")
            print(f"[ledger] ~ {match.id}  seen again as '{candidate.title}'")
        new_rows = [row for row in new_rows if row.id not in flagged_ids]
    elif flagged and not args.force:
        print("[ledger] Potential duplicates found. Re-run with --force to add anyway,")
        print("         or update the existing rows with `status` instead.")
        print()
        for candidate, matches in flagged:
            print(f"NEW: {candidate.title}")
            for match, ratio in matches:
                print(f"  ~{ratio:.0%}  {match.id} [{match.status}]  {match.title}")
            print()
        return 3

    existing.extend(new_rows)
    _write_ledger(ledger_path, existing)
    print(f"[ledger] Added {len(new_rows)} new row(s) to {ledger_path}.")
    for row in new_rows:
        print(f"  + {row.id}  {row.title}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger).expanduser()
    rows = _read_ledger(ledger_path)
    if args.status:
        rows = [r for r in rows if r.status == args.status]
    if not rows:
        print("(no rows match)")
        return 0
    rows.sort(key=lambda r: (r.last_updated or r.date_surfaced), reverse=True)
    print("| id | date | status | platform | title |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r.id} | {r.date_surfaced} | {r.status} | {r.platform} | {r.title} |")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.new_status not in VALID_STATUSES:
        print(f"[ledger] Unknown status: {args.new_status}", file=sys.stderr)
        return 2
    ledger_path = Path(args.ledger).expanduser()
    rows = _read_ledger(ledger_path)
    target = next((r for r in rows if r.id == args.id), None)
    if target is None:
        print(f"[ledger] No row with id={args.id}", file=sys.stderr)
        return 2
    allowed = TRANSITIONS.get(target.status, set())
    if args.new_status not in allowed:
        print(
            f"[ledger] Illegal transition: {target.status} -> {args.new_status}. "
            f"Allowed from {target.status}: {sorted(allowed) or '(terminal)'}",
            file=sys.stderr,
        )
        return 2
    if args.new_status in KILL_STATUSES and not args.killer_assumption:
        print(
            "[ledger] Status moves into a kill state — --killer-assumption is required.",
            file=sys.stderr,
        )
        return 2

    target.status = args.new_status
    target.last_updated = date.today().isoformat()
    if args.killer_assumption:
        target.killer_assumption = args.killer_assumption
    if args.score:
        target.score = args.score
    if args.confidence:
        target.confidence = args.confidence
    if args.note:
        target.notes = f"{target.notes}; {args.note}".lstrip("; ")
    _write_ledger(ledger_path, rows)
    print(f"[ledger] {target.id} -> {target.status}")
    return 0


def cmd_dedup(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger).expanduser()
    rows = _read_ledger(ledger_path)
    matches = _dedup_candidates(args.title, rows, top_n=args.top)
    if not matches:
        print("(no near-duplicates found)")
        return 0
    for row, ratio in matches:
        print(f"~{ratio:.0%}  {row.id} [{row.status}]  {row.title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--brief", required=True)
    p_add.add_argument("--ledger", required=True)
    p_add.add_argument("--force", action="store_true")
    p_add.add_argument(
        "--skip-duplicates", action="store_true",
        help="Instead of adding a flagged row, note the repeat on the matching existing row.",
    )
    p_add.add_argument("--init", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list")
    p_list.add_argument("--ledger", required=True)
    p_list.add_argument("--status", default=None)
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status")
    p_status.add_argument("--ledger", required=True)
    p_status.add_argument("--id", required=True)
    p_status.add_argument("--new-status", required=True)
    p_status.add_argument("--killer-assumption", default=None)
    p_status.add_argument("--score", default=None)
    p_status.add_argument("--confidence", default=None)
    p_status.add_argument("--note", default=None)
    p_status.set_defaults(func=cmd_status)

    p_dedup = sub.add_parser("dedup")
    p_dedup.add_argument("--ledger", required=True)
    p_dedup.add_argument("--title", required=True)
    p_dedup.add_argument("--top", type=int, default=3)
    p_dedup.set_defaults(func=cmd_dedup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
