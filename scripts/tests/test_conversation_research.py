#!/usr/bin/env python3
"""Checks for conversation_research.py (search scoring, excerpts, lanes, quote verification).

Run: python3 scripts/tests/test_conversation_research.py
"""

import io
import sys
import json
import tempfile
import contextlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conversation_research as cr


def conv(source, title, date, turns, research_type="null"):
    ai = "GEMINI" if source == "gemini" else "ASSISTANT"
    lines = ["---", "type: conversation", f'title: "{title}"', f"date: {date}", f"source: {source}",
             'id: "x"', "model: null", f"messages: {len(turns)}", "characters: 100", "has_code: false",
             "topics: []", f"research_type: {research_type}", "---", "", f"# {title}", "", "## Conversation", ""]
    for i, text in enumerate(turns):
        lines += [f"### {'USER' if i % 2 == 0 else ai} (10:0{i})", "", text, ""]
    return "\n".join(lines)


def write(d: Path, name: str, content: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


def search_args(out: Path, *terms, **kw) -> SimpleNamespace:
    base = dict(terms=list(terms), output_dir=out, source=None, date_from=None, date_to=None, include_excluded=False,
                min_score=4, max_files=60, batch_chars=250_000, skip_from=None)
    return SimpleNamespace(**{**base, **kw})


def test_fold_and_terms():
    assert cr.fold("Ύπνος ΥΠΝΟΣ") == "υπνοσ υπνοσ"
    assert len(cr.fold("Ελληνικά ÀÉÎ")) == len("Ελληνικά ÀÉÎ")
    terms = cr.compile_terms(["υπν", "deep work"])
    found = lambda text, i=0: bool(terms[i][1].search(cr.fold(text)))
    assert found("Δεν έχω ΎΠΝΟ") and found("ο ύπνος μου")
    assert not found("ξύπνησα νωρίς")                      # word-start by default
    assert cr.compile_terms(["*υπν"])[0][1].search(cr.fold("ξύπνησα"))
    assert found("some Deep\n Work today", 1)


def test_score_roles():
    terms = cr.compile_terms(["sleep"])
    user = cr.score_conversation(conv("claude", "Chat", "2026-01-01", ["I can't sleep", "ok"]), "Chat", terms)
    ai = cr.score_conversation(conv("claude", "Chat", "2026-01-01", ["hi", "sleep matters"]), "Chat", terms)
    titled = cr.score_conversation(conv("claude", "Sleep plan", "2026-01-01", ["hi", "ok"]), "Sleep plan", terms)
    assert (user["user_msgs"], user["ai_hits"], user["score"]) == (1, 0, 6), user
    assert (ai["user_msgs"], ai["ai_hits"], ai["score"]) == (0, 1, 2), ai
    assert titled["title_hit"] and titled["score"] == 12, titled
    # Gemini speaker headings count as AI text
    g = cr.score_conversation(conv("gemini", "G", "2026-01-01", ["hi", "sleep sleep"]), "G", terms)
    assert (g["user_msgs"], g["ai_hits"]) == (0, 2), g
    assert cr.score_conversation(conv("claude", "Chat", "2026-01-01", ["hi", "ok"]), "Chat", terms) is None


def test_excerpt():
    long_ai = "filler " * 3000 + "the sleep passage " + "filler " * 3000
    text = conv("claude", "T", "2026-01-01", ["short user question", long_ai, "x" * 3000 + " sleep at the end", "no match " * 2000])
    out = cr.build_excerpt(text, cr.compile_terms(["sleep"]), window=40)
    assert "short user question" in out
    assert "the sleep passage" in out and out.count("filler") < 20
    assert "[3,0" in out and "sleep at the end" in out      # long user turn trimmed, match kept
    assert "[no match, " in out


def test_search_lanes_and_skip(tmp: Path):
    out = tmp / "out"
    cg = out / "chatgpt-full" / "conversations"
    write(cg, "20250101_a.md", conv("chatgpt", "Sleep log", "2025-01-01T10:00:00", ["my sleep is bad", "try this"]))
    write(cg, "20250102_a2.md", conv("chatgpt", "Sleep again", "2025-01-02T10:00:00", ["hi", "ok"]))
    write(cg, "20250601_b.md", conv("chatgpt", "B", "2025-06-01T10:00:00", ["ύπνος χάλια", "ok"]))
    write(cg, "20240101_c.md", conv("chatgpt", "C", "2024-01-01T10:00:00", ["hi", "unrelated"]))
    write(out / "_excluded" / "chatgpt-full", "20250701_d.md", conv("chatgpt", "D", "null", ["sleep?", "ok"]))
    write(out / "memories", "health.md", "# Health\nsleep notes\n")

    r = cr.run_search(search_args(out, "sleep", "υπν", batch_chars=1, max_files=2))
    assert r["searched"] == 4 and r["matched"] == 3, r
    names = lambda files: [f["path"].split("/")[-1] for f in files]
    assert names(r["candidates"]) == ["20250101_a.md", "20250102_a2.md", "20250601_b.md"]
    assert [c["score"] for c in r["candidates"]] == [16, 12, 6]
    # Best of each quarter beats a higher score from an already covered quarter
    assert [names(b["files"]) for b in r["batches"]] == [["20250101_a.md"], ["20250601_b.md"]]
    assert [b["id"] for b in r["batches"]] == ["lane_01", "lane_02"] and r["batches"][0]["from"] == "2025-01-01"
    assert r["term_files"] == {"sleep": 2, "υπν": 1}
    assert r["context_docs"][0]["path"].endswith("memories/health.md")
    assert r["corpus"]["chatgpt"] == {"files": 4, "first": "2024-01-01", "last": "2025-06-01"}

    # Excluded files are opt-in; a null date falls back to the filename prefix
    r = cr.run_search(search_args(out, "sleep", include_excluded=True, date_from="2025-07-01"))
    assert [c["excluded"] for c in r["candidates"]] == [True] and r["candidates"][0]["date"] == "2025-07-01"

    # A second pass skips files scheduled in the first and continues lane numbering
    first = write(tmp, "pass1.json", json.dumps(cr.run_search(search_args(out, "sleep", max_files=1))))
    r = cr.run_search(search_args(out, "sleep", "υπν", skip_from=first))
    assert [names(b["files"]) for b in r["batches"]] == [["20250102_a2.md", "20250601_b.md"]]
    assert r["batches"][0]["id"] == "lane_02" and len(r["skipped"]) == 1


def test_verify(tmp: Path):
    src = write(tmp / "conv", "x.md", conv("claude", "T", "2026-01-01",
                                            ["Δεν **κοιμάμαι** καλά — ξυπνάω στις 4 το πρωί", "Try “magnesium” first."]))
    run = tmp / "run"
    findings = [
        {"file": str(src), "quote": "δεν κοιμαμαι καλα - ξυπναω"},          # accents, markdown, dash differ
        {"file": str(src), "quote": "Δεν κοιμάμαι … στις 4 το πρωί"},        # ellipsis segments in order
        {"file": str(src), "quote": "Try magnesium first."},                 # curly quotes dropped
        {"file": str(src), "quote": "I sleep perfectly"},                    # fabricated
        {"file": str(tmp / "missing.md"), "quote": "anything"},
        {"file": str(src), "quote": None},
    ]
    write(run, "lane_01.json", "```json\n" + json.dumps({"findings": findings}, ensure_ascii=False) + "\n```")
    write(run, "lane_02.json", "not json")
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        code = cr.verify_run(run)
    assert code == 1
    marks = [f["verified"] for f in json.loads((run / "lane_01.json").read_text(encoding="utf-8"))["findings"]]
    assert marks == [True, True, True, False, False, None], marks
    report = buf.getvalue()
    assert "6 findings, 5 quoted, 3 verified, 2 failed" in report and "1 unparseable" in report
    assert "file not found" in report and "quote not found" in report


if __name__ == "__main__":
    for t in (test_fold_and_terms, test_score_roles, test_excerpt):
        t()
        print(f"ok {t.__name__}")
    with tempfile.TemporaryDirectory() as d:
        for t in (test_search_lanes_and_skip, test_verify):
            t(Path(d) / t.__name__)
            print(f"ok {t.__name__}")
    print("all passed")
