#!/usr/bin/env python3
"""
Helpers for the conversation-research skill (.claude/skills/conversation-research/).

    search   Rank output/{chatgpt-full,claude-full,gemini}/conversations/ for a set of
             terms and pack the top matches into chronological reading batches (lanes).
    excerpt  Print a long conversation cut down to the topic: every user message, plus
             the AI passages around each match.
    verify   Check that the quotes reader agents wrote to lane_*.json exist in the cited
             conversations, and mark each finding "verified": true/false.

Matching ignores case and accents ("ύπνος", "ΥΠΝΟΣ" and "υπνος" are the same word) and
is anchored at the start of a word: "sleep" finds "sleeping" but not "asleep". Prefix a
term with * to match inside words ("*υπν" also finds "ξύπνησα").

Usage:
    python3 scripts/conversation_research.py search sleep υπν insomnia αυπν [--from 2025-01-01] [--save RUN/candidates.json]
    python3 scripts/conversation_research.py excerpt output/claude-full/conversations/20261009_x.md sleep υπν
    python3 scripts/conversation_research.py verify output/research/.runs/sleep_20261022
"""

import re
import sys
import json
import argparse
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

import research_index as ri

SOURCES = {"chatgpt-full": "chatgpt", "claude-full": "claude", "gemini": "gemini"}
# Derived documents (memories, profile, earlier reports), listed as secondary context
CONTEXT_GLOBS = ["*.md", "memories/*.md", "profile/**/*.md", "research-topics/*.md", "research/*.md"]
CONTEXT_SKIP = re.compile(r"INDEX|AUDIT|QUALITY_FILTER")
ROLE_RE = re.compile(r"^### (USER|ASSISTANT|GEMINI)\b.*$", re.M)
READ_WHOLE_MAX = 60_000   # longer conversations are read through `excerpt`
USER_TURN_MAX = 2_000     # longer user messages (pasted logs, documents) are trimmed in excerpts
MAX_PASSAGES = 15         # per message, in excerpts
NOISY_SHARE = 0.25        # a term matching more of the searched files than this is flagged
ELLIPSIS_RE = re.compile(r"\s*(?:\[(?:…|\.\.\.)\]|…|\.\.\.)\s*")
# Quote marks and markdown emphasis are dropped, dashes unified, before comparing quotes
QUOTE_TABLE = {**dict.fromkeys(map(ord, "'\"‘’‚‛“”„«»`*_>#"), None), **dict.fromkeys(map(ord, "‐‑‒–—―"), "-")}

_FOLD_TABLE: dict[int, str] = {}


def fold(text: str) -> str:
    """Lowercase and strip accents without changing length, so offsets index the original."""
    for c in set(text):
        if ord(c) not in _FOLD_TABLE:
            base = unicodedata.normalize("NFD", c)[0].lower()[0]
            _FOLD_TABLE[ord(c)] = "σ" if base == "ς" else base
    return text.translate(_FOLD_TABLE)


def read_text(path: Path) -> str:
    return unicodedata.normalize("NFC", path.read_text(encoding="utf-8", errors="replace"))


def iso_date(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    compiled = []
    for term in terms:
        body = fold(unicodedata.normalize("NFC", term.lstrip("*").strip()))
        if not body:
            sys.exit(f"empty search term: {term!r}")
        rx = re.escape(body).replace(r"\ ", r"\s+")
        compiled.append((term, re.compile(rx if term.startswith("*") else r"(?<!\w)" + rx)))
    return compiled


def split_turns(text: str) -> tuple[int, int, list[tuple[str, str, int, int]]]:
    """Return (body start, first message start, [(role, header, start, end)]); role is user or ai."""
    body = 0
    if text.startswith("---"):
        end = text.find("\n---", 3)
        body = end + 4 if end != -1 else 0
    marks = list(ROLE_RE.finditer(text, body))
    turns = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        turns.append(("user" if m.group(1) == "USER" else "ai", m.group(0), m.end(), end))
    return body, (marks[0].start() if marks else len(text)), turns


def quarter(date: str) -> str:
    return f"{date[:4]}-Q{(int(date[5:7]) - 1) // 3 + 1}" if date else "undated"


def file_date(fm: dict, path: Path) -> str:
    d = fm.get("date", "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return d
    m = re.match(r"(\d{4})(\d{2})(\d{2})_", path.name)
    return "-".join(m.groups()) if m else ""


def research_method(source: str, text: str, fm: dict) -> str | None:
    """Same structural detection as research_index.py (RESEARCH_INDEX.md)."""
    if source == "chatgpt":
        return ri.detect_chatgpt_deep_research(text, fm)
    if source == "claude":
        return ri.detect_claude_research(text, fm)
    return ri.detect_gemini_deep_research(text)


def score_conversation(text: str, title: str, terms) -> dict | None:
    """Count where the terms occur; None when nothing matches.

    score = 10 if a term is in the title, + 4 per user message with a match (max 10),
    + 1 per 2 matches in AI text (max 10), + 2 per distinct term matched.
    """
    folded = fold(text)
    body, first, turns = split_turns(text)
    regions = [(role, s, e) for role, _, s, e in turns] or [("ai", body, len(text))]
    summary = text.find("## Summary", body, first)
    if summary != -1:
        regions.append(("ai", summary, first))
    counts = Counter()
    title_hit = False
    folded_title = fold(title)
    for name, rx in terms:
        if rx.search(folded_title):
            title_hit = True
            counts[name] += 1
    user_msgs = ai_hits = 0
    for role, s, e in regions:
        hits = 0
        for name, rx in terms:
            n = len(rx.findall(folded, s, e))
            if n:
                counts[name] += n
                hits += n
        if role == "user":
            user_msgs += hits > 0
        else:
            ai_hits += hits
    if not counts:
        return None
    score = 10 * title_hit + 4 * min(user_msgs, 10) + min(ai_hits, 20) // 2 + 2 * len(counts)
    return {"score": score, "title_hit": title_hit, "user_msgs": user_msgs, "ai_hits": ai_hits, "terms": dict(counts)}


def match_spans(folded: str, start: int, end: int, terms, window: int) -> list[tuple[int, int]]:
    spans = sorted((max(start, m.start() - window), min(end, m.end() + window))
                   for _, rx in terms for m in rx.finditer(folded, start, end))
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def build_excerpt(text: str, terms, window: int = 500) -> str:
    """Topic view of a conversation: every user message (long ones trimmed to their head
    plus matching passages) and only the matching passages of AI messages."""
    folded = fold(text)
    body, first, turns = split_turns(text)
    fm = ri._parse_yaml_frontmatter(text)
    out = [f"# {fm.get('title', '')}",
           f"{fm.get('source', '')} · {fm.get('date', '')[:10]} · {len(text):,} chars · {len(turns)} messages",
           "Excerpt: AI messages are cut to the passages around matches; […] marks omitted text.", ""]
    summary = text.find("## Summary", body, first)
    if summary != -1:
        out += [clip(text[summary:first].strip(), USER_TURN_MAX), ""]
    for role, header, s, e in turns:
        if role == "user" and e - s <= USER_TURN_MAX:
            out += [header, text[s:e].strip(), ""]
            continue
        spans = match_spans(folded, s, e, terms, window)
        if role == "user":
            cut = s + USER_TURN_MAX // 2
            spans = [(max(a, cut), b) for a, b in spans if b > cut]
            out += [f"{header}  [{e - s:,} chars, trimmed]", text[s:cut].strip() + " […]"]
        elif spans:
            out.append(f"{header}  [{len(spans)} matching passage(s) of {e - s:,} chars]")
        else:
            out.append(f"{header}  [no match, {e - s:,} chars omitted]")
            continue
        out += [f"[…] {text[a:b].strip()} […]" for a, b in spans[:MAX_PASSAGES]]
        if len(spans) > MAX_PASSAGES:
            out.append(f"[+{len(spans) - MAX_PASSAGES} more matching passages omitted]")
        out.append("")
    return "\n".join(out)


def iter_conversations(output_dir: Path, sources: set[str], include_excluded: bool):
    for folder, source in SOURCES.items():
        if source not in sources:
            continue
        dirs = [(output_dir / folder / "conversations", False)]
        if include_excluded:
            dirs.append((output_dir / "_excluded" / folder, True))
        for d, excluded in dirs:
            for path in sorted(d.glob("*.md")):
                if not path.name.startswith("INDEX"):
                    yield path, source, excluded


def search(output_dir: Path, terms, sources: set[str], date_from: str | None, date_to: str | None,
           include_excluded: bool) -> tuple[list[dict], dict, Counter, int]:
    """Return (matching conversations, per-source coverage, files per term, files searched)."""
    corpus: dict[str, dict] = {}
    term_files = Counter()
    matches = []
    searched = 0
    for path, source, excluded in iter_conversations(output_dir, sources, include_excluded):
        text = read_text(path)
        fm = ri._parse_yaml_frontmatter(text)
        date = file_date(fm, path)
        if (date_from or date_to) and not date:
            continue
        if (date_from and date < date_from) or (date_to and date > date_to):
            continue
        searched += 1
        cov = corpus.setdefault(source, {"files": 0, "first": None, "last": None})
        cov["files"] += 1
        if date:
            cov["first"] = min(cov["first"] or date, date)
            cov["last"] = max(cov["last"] or date, date)
        title = fm.get("title") or path.stem
        rec = score_conversation(text, title, terms)
        if rec is None:
            continue
        term_files.update(rec["terms"].keys())
        research = research_method(source, text, fm)
        if research and rec["ai_hits"]:
            rec["score"] += 5
        long = len(text) > READ_WHOLE_MAX
        matches.append({
            "path": str(path), "source": source, "date": date, "title": title,
            "chars": len(text), "messages": int(fm["messages"]) if fm.get("messages", "").isdigit() else 0,
            "research": research, "excluded": excluded, **rec,
            "mode": "excerpt" if long else "full",
            "cost": len(build_excerpt(text, terms)) if long else len(text),
        })
    matches.sort(key=lambda c: (-c["score"], c["date"]))
    return matches, corpus, term_files, searched


def make_batches(selected: list[dict], budget: int, start: int = 1) -> list[dict]:
    """Pack files in date order into batches of at most `budget` characters to read."""
    groups, current, cost = [], [], 0
    for c in sorted(selected, key=lambda c: (c["date"], c["path"])):
        if current and cost + c["cost"] > budget:
            groups.append(current)
            current, cost = [], 0
        current.append(c)
        cost += c["cost"]
    if current:
        groups.append(current)
    return [{"id": f"lane_{i:02d}", "from": g[0]["date"], "to": g[-1]["date"], "cost": sum(c["cost"] for c in g),
             "files": [{"path": c["path"], "mode": c["mode"], "chars": c["chars"]} for c in g]}
            for i, g in enumerate(groups, start)]


def scan_context(output_dir: Path, terms) -> list[dict]:
    hits, seen = [], set()
    for pattern in CONTEXT_GLOBS:
        for path in sorted(output_dir.glob(pattern)):
            if path in seen or CONTEXT_SKIP.search(path.name):
                continue
            seen.add(path)
            folded = fold(read_text(path))
            counts = [len(rx.findall(folded)) for _, rx in terms]
            if any(counts):
                hits.append({"path": str(path), "terms": sum(1 for n in counts if n), "hits": sum(counts)})
    hits.sort(key=lambda h: (-h["terms"], -h["hits"]))
    return hits[:15]


def run_search(args) -> dict:
    terms = compile_terms(args.terms)
    sources = set(args.source or SOURCES.values())
    matches, corpus, term_files, searched = search(args.output_dir, terms, sources, args.date_from,
                                                   args.date_to, args.include_excluded)
    skip, start = set(), 1
    if args.skip_from:
        prior = json.loads(args.skip_from.read_text(encoding="utf-8"))
        skip = set(prior["skipped"]) | {f["path"] for b in prior["batches"] for f in b["files"]}
        start = prior["next_lane"]
    candidates = [c for c in matches if c["score"] >= args.min_score]
    pool = [c for c in candidates if c["path"] not in skip]
    # The best file of every quarter first, so quiet periods still get read; then by score
    firsts = {}
    for c in pool:
        firsts.setdefault(quarter(c["date"]), c)
    selected = list(firsts.values())[:args.max_files]
    picked = {c["path"] for c in selected}
    selected += [c for c in pool if c["path"] not in picked][:args.max_files - len(selected)]
    batches = make_batches(selected, args.batch_chars, start)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "query": {"terms": args.terms, "sources": sorted(sources), "from": args.date_from, "to": args.date_to,
                  "include_excluded": args.include_excluded, "min_score": args.min_score,
                  "max_files": args.max_files, "batch_chars": args.batch_chars},
        "searched": searched,
        "corpus": corpus,
        "term_files": {t: term_files[t] for t in args.terms},
        "matched": len(matches),
        "candidates": candidates,
        "batches": batches,
        "skipped": sorted(skip),
        "next_lane": start + len(batches),
        "context_docs": scan_context(args.output_dir, terms),
    }


def print_summary(result: dict, top: int):
    searched, q = result["searched"], result["query"]
    print(f"Searched {searched:,} conversations: " + " · ".join(
        f"{s} {c['files']:,} ({c['first']} → {c['last']})" for s, c in result["corpus"].items()))
    print("\nFiles matching each term:")
    for term, n in result["term_files"].items():
        noisy = "  <- noisy: narrow it or lean on the score" if searched and n / searched > NOISY_SHARE else ""
        print(f"  {term:<28} {n:>6,}{noisy}")
    cands = result["candidates"]
    by_source = Counter(c["source"] for c in cands)
    batches = result["batches"]
    scheduled = {f["path"] for b in batches for f in b["files"]}
    by_quarter = Counter(quarter(c["date"]) for c in cands)
    read_quarter = Counter(quarter(c["date"]) for c in cands if c["path"] in scheduled)
    print(f"\nMatched {result['matched']:,} files; {len(cands):,} candidates with score >= {q['min_score']} ("
          + " · ".join(f"{s} {n}" for s, n in sorted(by_source.items()))
          + f"; {sum(1 for c in cands if c['research'])} research reports)")
    print("Candidates/scheduled by quarter: " + " · ".join(
        f"{k} {n}/{read_quarter[k]}" for k, n in sorted(by_quarter.items())))
    if result["skipped"]:
        print(f"Skipped {len(result['skipped'])} files already scheduled in an earlier pass")
    print(f"\nScheduled {len(scheduled)} files (best of each quarter, then by score) in {len(batches)} lanes"
          f" (<= {q['batch_chars']:,} chars each):")
    for b in batches:
        print(f"  {b['id']}  {b['from']} → {b['to']}  {len(b['files']):>3} files  {b['cost']:>9,} chars")
    print(f"\nTop {min(top, len(cands))} (score · date · source · R=research · chars · user msgs/AI hits):")
    for c in cands[:top]:
        print(f"  {c['score']:>4}  {c['date']}  {c['source']:<7} {'R' if c['research'] else ' '} {c['chars']:>9,}"
              f"  u{c['user_msgs']}/a{c['ai_hits']:<4} {clip(c['title'], 60)}\n        {c['path']}")
    if result["context_docs"]:
        print("\nDerived docs that mention the terms (secondary context, not primary evidence):")
        for d in result["context_docs"]:
            print(f"  {d['terms']} terms · {d['hits']:>4} hits  {d['path']}")


def normalize_quote(text: str) -> str:
    s = fold(unicodedata.normalize("NFC", text)).translate(QUOTE_TABLE)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, normalized_text: str) -> bool:
    """True when every ellipsis-separated segment of `quote` appears in order."""
    pos = 0
    for seg in ELLIPSIS_RE.split(quote):
        seg = normalize_quote(seg)
        if not seg:
            continue
        i = normalized_text.find(seg, pos)
        if i == -1:
            return False
        pos = i + len(seg)
    return True


def load_lane(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(str(e)) from None


def verify_run(run_dir: Path) -> int:
    """Mark each lane finding verified true/false (null when it has no quote); 1 if any failed."""
    texts: dict[str, str | None] = {}
    totals = Counter()
    failures = []
    for lane_path in sorted(run_dir.glob("lane_*.json")):
        try:
            data = load_lane(lane_path)
        except ValueError as e:
            totals["unparseable"] += 1
            print(f"{lane_path.name}: UNPARSEABLE ({e})")
            continue
        lane = Counter()
        for finding in data.get("findings", []):
            lane["findings"] += 1
            quote = (finding.get("quote") or "").strip()
            if not quote:
                finding["verified"] = None
                continue
            lane["quoted"] += 1
            path = finding.get("file", "")
            if path not in texts:
                texts[path] = normalize_quote(read_text(Path(path))) if Path(path).is_file() else None
            ok = texts[path] is not None and quote_found(quote, texts[path])
            finding["verified"] = ok
            lane["verified" if ok else "failed"] += 1
            if not ok:
                failures.append((lane_path.name, path, quote, "file not found" if texts[path] is None else "quote not found"))
        lane_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        totals.update(lane)
        print(f"{lane_path.name}: {lane['findings']} findings, {lane['quoted']} quoted, "
              f"{lane['verified']} verified, {lane['failed']} failed")
    print(f"\nTotal: {totals['findings']} findings · {totals['quoted']} quoted · {totals['verified']} verified"
          f" · {totals['failed']} failed · {totals['unparseable']} unparseable lane files")
    for name, path, quote, why in failures:
        print(f"  FAIL {name} {path}: {why}: \"{clip(quote, 80)}\"")
    return 1 if failures or totals["unparseable"] else 0


def main():
    parser = argparse.ArgumentParser(description="Search, excerpt and quote-check AI conversations for topic research")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="Rank conversations for a set of terms and plan reading lanes")
    s.add_argument("terms", nargs="+", help="Search terms; prefix with * to match inside words")
    s.add_argument("--output-dir", type=Path, default=Path("output"), help="Output root (default: output)")
    s.add_argument("--source", action="append", choices=sorted(SOURCES.values()), help="Limit to a source (repeatable)")
    s.add_argument("--from", dest="date_from", type=iso_date, help="Earliest conversation date, YYYY-MM-DD")
    s.add_argument("--to", dest="date_to", type=iso_date, help="Latest conversation date, YYYY-MM-DD (inclusive)")
    s.add_argument("--include-excluded", action="store_true", help="Also search the quality-filtered output/_excluded/")
    s.add_argument("--min-score", type=int, default=4, help="Minimum score for a candidate (default: 4)")
    s.add_argument("--max-files", type=int, default=60, help="Top candidates to schedule for reading (default: 60)")
    s.add_argument("--batch-chars", type=int, default=250_000, help="Characters to read per lane (default: 250000)")
    s.add_argument("--top", type=int, default=25, help="Candidate rows to print (default: 25)")
    s.add_argument("--skip-from", type=Path, help="Earlier candidates JSON: skip its scheduled files, continue lane numbering")
    s.add_argument("--save", type=Path, help="Write the full result (candidates, lanes, coverage) as JSON")

    e = sub.add_parser("excerpt", help="Print the topic-relevant parts of one conversation")
    e.add_argument("file", type=Path)
    e.add_argument("terms", nargs="+")
    e.add_argument("--window", type=int, default=500, help="Characters of context around each match (default: 500)")

    v = sub.add_parser("verify", help="Check lane quotes against their cited conversations")
    v.add_argument("run_dir", type=Path)

    args = parser.parse_args()
    if args.command == "search":
        result = run_search(args)
        print_summary(result, args.top)
        if args.save:
            args.save.parent.mkdir(parents=True, exist_ok=True)
            args.save.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"\nSaved {args.save}")
    elif args.command == "excerpt":
        print(build_excerpt(read_text(args.file), compile_terms(args.terms), args.window))
    else:
        sys.exit(verify_run(args.run_dir))


if __name__ == "__main__":
    main()
