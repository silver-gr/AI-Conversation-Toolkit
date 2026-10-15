#!/usr/bin/env python3
"""Checks for research_index / quality_filter frontmatter detection and rebuild_indexes.

Run: /Users/silver/venv/bin/python3 scripts/tests/test_research_quality.py
"""

import io
import sys
import tempfile
import contextlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import research_index as ri
import quality_filter as qf
import rebuild_indexes as rb


def fm_block(source, title, date, research_type="null", conv_id=None, chars=9000, messages=4):
    lines = ["---", "type: conversation", f'title: "{title}"', f"date: {date}", f"source: {source}"]
    if conv_id:
        lines.append(f'id: "{conv_id}"')
    lines += ["model: null", f"messages: {messages}", f"characters: {chars}", "has_code: false",
              "topics:", "  - test", f"research_type: {research_type}", "---", ""]
    return "\n".join(lines)


def body(text, size=6000):
    return f"# T\n\n## Conversation\n\n### USER (10:00)\n\nquestion here\n\n### ASSISTANT (10:01)\n\n{text}\n\n" + "x" * size + "\n"


def write(d: Path, name: str, content: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


def test_research_index(tmp: Path):
    cg = tmp / "chatgpt-full" / "conversations"
    write(cg, "20261001_dr.md", fm_block("chatgpt", "DR", "2026-10-01T10:00:00", "chatgpt_deep_research", "c-1") + body("report"))
    write(cg, "20261001_plain.md", fm_block("chatgpt", "Plain", "2026-10-01T11:00:00", "null", "c-2") + body("hi", 100))

    cl = tmp / "claude-full" / "conversations"
    write(cl, "20261002_res.md", fm_block("claude", "Res", "2026-10-02T10:00:00", "claude_research", "u-1") + body("done"))
    write(cl, "20261002_ws.md", fm_block("claude", "WS", "2026-10-02T11:00:00", "claude_web_search", "u-2") + body("done"))
    # New-format, null research_type: incidental tool line and phrase must not trigger
    write(cl, "20261002_incidental.md", fm_block("claude", "Inc", "2026-10-02T12:00:00", "null", "u-3")
          + body("*[tools: web_search x1]*\nThis is a research task about web_search."))
    # Legacy (no id): body heuristics still apply
    write(cl, "20250101_legacy.md", fm_block("claude", "Legacy", "2025-01-01T10:00:00") + body("used web_search"))
    write(cl, "20250102_legacy_rt.md", fm_block("claude", "LegacyRT", "2025-01-02T10:00:00") + body("a research task"))

    got = {r["file"]: r["detection_method"] for r in ri.scan_source(tmp / "chatgpt-full", "chatgpt", ri.detect_chatgpt_deep_research)}
    assert got == {"20261001_dr.md": "deep_research_report"}, got

    got = {r["file"]: r["detection_method"] for r in ri.scan_source(tmp / "claude-full", "claude", ri.detect_claude_research)}
    assert got == {
        "20261002_res.md": "claude_research_tool",
        "20261002_ws.md": "web_search_tool",
        "20250101_legacy.md": "web_search_tool",
        "20250102_legacy_rt.md": "research_task",
    }, got

    out = tmp / "RESEARCH_INDEX.md"
    with contextlib.redirect_stdout(io.StringIO()):
        ri.create_research_index(tmp / "claude-full", tmp / "chatgpt-full", tmp / "gemini", out)
    text = out.read_text(encoding="utf-8")
    assert "- **Total Research Conversations**: 5" in text
    assert "| `deep_research_report` |" in text and "| `claude_research_tool` |" in text


def test_quality_filter(tmp: Path):
    assert qf.detect_research("nothing here", "claude", {"research_type": "claude_web_search"})
    assert not qf.detect_research("nothing here", "claude", {"research_type": None})
    assert not qf.detect_research("nothing here", "claude")

    conv = tmp / "qf" / "claude-full" / "conversations"
    # Short single-message file would normally be excluded; research_type force-keeps it
    p = write(conv, "20261003_short.md", fm_block("claude", "Short", "2026-10-03T10:00:00", "claude_research", "u-9", chars=900, messages=1) + "hi\n")
    score, reasons, force_keep, keep_reason = qf.score_conversation(p, "claude")
    assert force_keep and keep_reason == "research", (score, reasons)
    p2 = write(conv, "20261003_short_null.md", fm_block("claude", "Short", "2026-10-03T11:00:00", "null", "u-10", chars=900, messages=1) + "hi\n")
    assert not qf.score_conversation(p2, "claude")[2]

    # --dry-run writes the .dryrun report and leaves the real report and files alone
    out_dir = tmp / "qf"
    real_report = write(out_dir, "QUALITY_FILTER_REPORT.md", "previous applied run\n")
    saved = qf.OUTPUT_DIR, qf.SOURCES
    qf.OUTPUT_DIR, qf.SOURCES = out_dir, {"claude": (conv, "claude-full")}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            qf.run_filter(dry_run=True)
    finally:
        qf.OUTPUT_DIR, qf.SOURCES = saved
    assert real_report.read_text() == "previous applied run\n"
    assert "DRY RUN" in (out_dir / "QUALITY_FILTER_REPORT.dryrun.md").read_text()
    assert p2.exists() and not (out_dir / "_excluded").exists()


def test_rebuild_indexes(tmp: Path):
    conv = tmp / "rb" / "gemini" / "conversations"
    write(conv, "20260101_a.md", fm_block("gemini", "Older", "2026-01-01T10:00:00", conv_id="g-1", chars=1234, messages=2) + "body\n")
    long_title = "A|B " + "y" * 80
    write(conv, "20260201_b.md", fm_block("gemini", long_title, "2026-02-01T10:00:00", conv_id="g-2", chars=56789, messages=3) + "body\n")
    write(conv, "20260301_c.md", '---\ntitle: "line one\n  line two"\ndate: 2026-03-01T10:00:00\nmessages: 1\ncharacters: 10\n---\nbody\n')
    write(conv, "20260401_bad.md", "---\ntitle: [unclosed\n---\nbody\n")
    write(conv, "20260501_nofm.md", "# no frontmatter\n")

    lines, n, failures = rb.build_index(conv, "GEMINI")
    assert n == 3, n
    assert sorted(f for f, _ in failures) == ["20260401_bad.md", "20260501_nofm.md"], failures
    assert lines[0] == "# GEMINI Conversation Index"
    assert lines[4] == "**Total conversations: 3**"
    assert lines[6] == "| Date | Title | Messages | Characters |"
    rows = lines[8:]
    assert rows[0] == "| 2026-03-01 | [line one line two](conversations/20260301_c.md) | 1 | 10 |", rows[0]
    assert rows[1] == f"| 2026-02-01 | [{('A-B ' + 'y' * 80)[:60]}](conversations/20260201_b.md) | 3 | 56,789 |", rows[1]
    assert rows[2] == "| 2026-01-01 | [Older](conversations/20260101_a.md) | 2 | 1,234 |", rows[2]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        for t in (test_research_index, test_quality_filter, test_rebuild_indexes):
            t(Path(d))
            print(f"ok {t.__name__}")
    print("all passed")
