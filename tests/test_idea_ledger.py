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


def _load_ledger_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("idea_ledger", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["idea_ledger"] = module  # dataclasses look the module up here
    spec.loader.exec_module(module)
    return module


def test_reworded_repeats_share_key_words():
    ledger = _load_ledger_module()
    pairs = [
        ("Subscription tracker that advises if subscriptions are worth keeping",
         "Subscription Worth-It Checker"),
        ("Language learning app better than Duolingo for Mandarin",
         "Duolingo alternative focused on Mandarin Chinese"),
        ("Medical/health science study app with interactive quizzes",
         "Health science study app (general)"),
    ]
    for a, b in pairs:
        assert ledger._shares_key_words(a, b), (a, b)


def test_distinct_ideas_do_not_share_key_words():
    ledger = _load_ledger_module()
    pairs = [
        ("Nursing school study companion app", "Health science study app (general)"),
        ("Radiology Tech Study & ARRT Exam Prep", "Health science study app (general)"),
        ("Mobile invoice generator for freelancers", "Temporary shared expense pool app"),
    ]
    for a, b in pairs:
        assert not ledger._shares_key_words(a, b), (a, b)
