import csv
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "idea-ledger" / "scripts" / "ledger.py"

HEADER = (
    "id,date_surfaced,title,platform,source_brief,status,score,confidence,"
    "killer_assumption,last_updated,notes\n"
)


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
    )


def _setup(tmp_path):
    ledger = tmp_path / "ledger.csv"
    ledger.write_text(
        HEADER
        + "20260925-nursing-study-app,2026-09-25,Nursing Study App,Both,x,triaged_no,,low,crowded,2026-09-25,old note\n"
    )
    brief = tmp_path / "ideas.md"
    brief.write_text(
        "# Mobile app ideas\n\n"
        "## 1. Nursing study app\n- **Platform:** Both\n\n"
        "## 2. Tide chart for anglers\n- **Platform:** iOS\n"
    )
    return ledger, brief


def _rows(ledger):
    with ledger.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_add_without_flags_refuses_duplicates(tmp_path):
    ledger, brief = _setup(tmp_path)
    result = _run("add", "--brief", str(brief), "--ledger", str(ledger))
    assert result.returncode == 3
    assert len(_rows(ledger)) == 1


def test_skip_duplicates_adds_new_and_notes_repeat(tmp_path):
    ledger, brief = _setup(tmp_path)
    result = _run("add", "--brief", str(brief), "--ledger", str(ledger), "--skip-duplicates")
    assert result.returncode == 0, result.stderr
    rows = _rows(ledger)
    assert [r["title"] for r in rows] == ["Nursing Study App", "Tide chart for anglers"]
    original = rows[0]
    assert original["status"] == "triaged_no"
    assert original["notes"].startswith("old note; Seen again ")
    assert "'Nursing study app'" in original["notes"]
    assert original["last_updated"] == rows[1]["date_surfaced"]


def test_force_still_adds_everything(tmp_path):
    ledger, brief = _setup(tmp_path)
    result = _run("add", "--brief", str(brief), "--ledger", str(ledger), "--force")
    assert result.returncode == 0, result.stderr
    assert len(_rows(ledger)) == 3
