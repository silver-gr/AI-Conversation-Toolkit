#!/usr/bin/env python3
"""Checks for memories_to_md: input discovery, new/legacy formats, idempotency, stale backup.

Run: /Users/silver/venv/bin/python3 scripts/tests/test_memories.py
"""

import io
import json
import sys
import tempfile
import contextlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memories_to_md as m
import run_import as ri

ACCOUNT = "aaaaaaaa-0000-0000-0000-000000000000"
P1 = "11111111-0000-0000-0000-000000000000"
P2 = "22222222-0000-0000-0000-000000000000"
P3 = "33333333-0000-0000-0000-000000000000"


def snapshot(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): p.read_text(encoding="utf-8") for p in d.rglob("*") if p.is_file()}


def run(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def new_format_export(claude: Path):
    (claude / "memories").mkdir(parents=True)
    (claude / "reflections").mkdir()
    (claude / "Takeout - Sept 3rd" / "projects").mkdir(parents=True)
    memory_files = [
        {"path": "/profile.md", "content": "---\ntitle: Profile\n---\n- [stated] likes tests\n", "updated_at": "x"},
        {"path": f"/projects/{P1}/index.md", "content": '---\nname: "Δουλειά/Workflow"\n---\n', "updated_at": "x"},
        {"path": f"/projects/{P2}/index.md", "content": "---\nname: Dup Name\n---\n", "updated_at": "x"},
        {"path": "/topics/../escape.md", "content": "nope", "updated_at": "x"},
    ]
    data = {
        "account_uuid": ACCOUNT,
        "conversations_memory": "**Purpose & context**\naccount memory\n",
        "project_memories": {P1: "x3lixi stuff", P2: "second", P3: "third dup"},
        "memory_files": memory_files,
    }
    (claude / "memories" / f"{ACCOUNT}.json").write_text(json.dumps(data), encoding="utf-8")
    # P3 has no index.md: resolved from the Takeout projects fallback, colliding with P2's slug
    (claude / "Takeout - Sept 3rd" / "projects" / f"{P3}.json").write_text(
        json.dumps({"uuid": P3, "name": "Dup  Name"}), encoding="utf-8")
    reflections = {"account_uuid": ACCOUNT, "reflections": [{
        "period": "2026-04:2026-09", "created_at": "2026-10-01T00:00:00+00:00",
        "content": {"hero_title": "Hero", "hero_body": "Body text", "period": "Apr-Sep",
                    "stats": [{"label": "Chats", "n": "5", "sublabel": None}],
                    "topics": [{"title": "T", "percent": 40, "description": "desc"}],
                    "about_your_time": [{"title": "AT", "body": "at body"}],
                    "expanding_your_skills": [{"skill": "delegation", "title": "ES", "body": "es body"}],
                    "worth_thinking_about": [{"title": "WT", "body": "wt body"}]},
    }], "feedback": []}
    (claude / "reflections" / f"{ACCOUNT}.json").write_text(json.dumps(reflections), encoding="utf-8")


def test_new_format_idempotent_and_backup(tmp: Path):
    claude = tmp / "Claude"
    new_format_export(claude)
    inputs = m.find_memory_inputs(claude)
    assert inputs == [claude / "memories" / f"{ACCOUNT}.json"], inputs

    out = tmp / "output" / "memories"
    out.mkdir(parents=True)
    (out / "x3lixi-platform-3.md").write_text("stale", encoding="utf-8")

    s1 = run(m.convert_all, inputs, claude, out)
    first = snapshot(out)
    s2 = run(m.convert_all, inputs, claude, out)
    assert first == snapshot(out), "second run changed the output"
    assert s1["backed_up"] == 1 and s2["backed_up"] == 0

    backups = list((tmp / "output" / "_backup").glob("memories-*/x3lixi-platform-3.md"))
    assert len(backups) == 1 and backups[0].read_text() == "stale"

    assert set(first) == {
        "main-context.md",
        "δουλειά-workflow.md",
        "dup-name.md",
        "dup-name-33333333.md",
        "files/profile.md",
        f"files/projects/{P1}/index.md",
        f"files/projects/{P2}/index.md",
        "reflections/2026-04_2026-09.md",
    }, sorted(first)
    assert first["δουλειά-workflow.md"].startswith("# Δουλειά/Workflow\n")
    assert first["dup-name-33333333.md"].startswith("# Dup  Name\n")
    assert first["files/profile.md"] == "---\ntitle: Profile\n---\n- [stated] likes tests\n"

    refl = first["reflections/2026-04_2026-09.md"]
    assert refl.startswith('---\ntype: reflection\nperiod: "2026-04:2026-09"\ncreated: "2026-10-01T00:00:00+00:00"\n---\n# Hero\n')
    for text in ("Body text", "- Chats: 5\n", "**T** (40%) — desc", "### ES (delegation)", "wt body", "at body"):
        assert text in refl, text


def test_legacy_list_format(tmp: Path):
    claude = tmp / "Claude"
    claude.mkdir()
    legacy = [{"conversations_memory": "ctx", "project_memories": {P1: "about meditation", P2: "more meditation"}}]
    (claude / "memories.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert m.find_memory_inputs(claude) == [claude / "memories.json"]
    assert m.find_memory_inputs(tmp / "nowhere") == []

    out = tmp / "out"
    out.mkdir()
    stats = run(m.process_memories, claude / "memories.json", out)
    assert stats["processed"] == 3
    assert sorted(p.name for p in out.glob("*.md")) == [
        "main-context.md", "meditation-course-22222222.md", "meditation-course.md"]
    run(m.process_memories, claude / "memories.json", out)
    assert len(list(out.glob("*.md"))) == 3, "re-run must overwrite, not pile up -N copies"


def test_unsafe_period_and_nulls(tmp: Path):
    claude = tmp / "Claude"
    (claude / "memories").mkdir(parents=True)
    (claude / "reflections").mkdir()
    data = {"account_uuid": ACCOUNT, "conversations_memory": None, "project_memories": {P1: None},
            "memory_files": [{"path": None, "content": None}, {"path": "/a.md", "content": None}]}
    (claude / "memories" / "a.json").write_text(json.dumps(data), encoding="utf-8")
    (claude / "memories" / "b.json").write_text(json.dumps({"project_memories": None, "memory_files": None}),
                                                encoding="utf-8")
    refl = {"reflections": [{"period": "../../../escaped", "content": None},
                            {"period": "2026/01", "content": {"about_your_time": [{"title": None, "body": None}]}}]}
    (claude / "reflections" / "r.json").write_text(json.dumps(refl), encoding="utf-8")

    out = tmp / "x" / "out" / "memories"
    run(m.convert_all, m.find_memory_inputs(claude), claude, out)
    assert not list((tmp / "x").glob("*.md")) and not (tmp / "escaped.md").exists()
    files = set(snapshot(out))
    assert {"reflections/.._.._.._escaped.md", "reflections/2026_01.md", "files/a.md"} <= files, sorted(files)
    assert all(f.count("/") <= 1 for f in files if f.startswith("reflections/"))


def test_gemini_default_path():
    assert ri.DEFAULT_PATHS["gemini"] == Path("Google/My Activity/Gemini Apps/MyActivity.json")
    greek = [p for p in ri.GEMINI_ALT_PATHS if "Η δραστηριότητά μου" in str(p)]
    assert len(greek) == 2 and ri.GEMINI_ALT_PATHS[-2:] == greek


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        test_new_format_idempotent_and_backup(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_legacy_list_format(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_unsafe_period_and_nulls(Path(d))
    test_gemini_default_path()
    print("test_memories: all checks passed")
