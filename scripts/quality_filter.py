#!/usr/bin/env python3
"""
Quality Filter for AI Conversations → UltraRAG Curation
Separates knowledge-rich conversations from low-value ones.

Usage:
    python3 scripts/quality_filter.py [--dry-run] [--verbose] [--threshold SCORE]
"""

import os
import re
import sys
import shutil
import argparse
from pathlib import Path
from typing import Optional
from datetime import datetime

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required. Install with: pip3 install pyyaml")
    sys.exit(1)

# ─── Paths ─────────────────────────────────────────────────────────────────────

OUTPUT_DIR = Path(__file__).parent.parent / "output"
EXCLUDED_DIR = OUTPUT_DIR / "_excluded"

SOURCES = {
    "chatgpt": (OUTPUT_DIR / "chatgpt-full" / "conversations", "chatgpt-full"),
    "claude":  (OUTPUT_DIR / "claude-full" / "conversations",  "claude-full"),
    "gemini":  (OUTPUT_DIR / "gemini" / "conversations",        "gemini"),
}

# ─── Quality Threshold ─────────────────────────────────────────────────────────
# Score ≥ this → keep for UltraRAG
MIN_SCORE_KEEP = 40

# ─── Research Detection (content-based, mirrors research_index.py) ─────────────

GEMINI_RESEARCH_PATTERNS = [
    "here's a research plan",
    "i've completed your research",
    "start research",
    "έναρξη έρευνας",
    "research plan for that topic",
    "feel free to ask me follow-up questions",
]

CHATGPT_RESEARCH_PATTERNS = [
    "deep research",
    "conducted a comprehensive",
    "sources consulted",
    "i conducted a deep",
    "thorough research",
    "extensive research",
]

CLAUDE_RESEARCH_PATTERNS = [
    "research allowance",
    "research tool",
    "web_search",
    "searching the web",
    "i'll research this",
    "i'll use the research",
]

def detect_research(body_lower: str, source: str, meta: Optional[dict] = None) -> bool:
    """Returns True if conversation contains research content."""
    if meta and meta.get("research_type"):
        return True
    if meta and "id" in meta:
        # New-format renders list tool calls in the body ("*[tools: web_search x1]*"),
        # so the bare tool name is no longer a research signal there.
        body_lower = body_lower.replace("web_search", "")
    if source == "gemini":
        for p in GEMINI_RESEARCH_PATTERNS:
            if p in body_lower:
                return True
    if source == "chatgpt":
        for p in CHATGPT_RESEARCH_PATTERNS:
            if p in body_lower:
                return True
    if source == "claude":
        for p in CLAUDE_RESEARCH_PATTERNS:
            if p in body_lower:
                return True
    # Fallback: check all patterns
    for p in CHATGPT_RESEARCH_PATTERNS + CLAUDE_RESEARCH_PATTERNS + GEMINI_RESEARCH_PATTERNS:
        if p in body_lower:
            return True
    return False

# ─── Hard Exclusion Patterns (automatic, no appeal) ───────────────────────────

# Title → automatic exclusion if ALSO chars < 10000
IMAGE_TITLE_PATTERNS = [
    r"\bsticker(s)?\b",
    r"\bresize.*image",
    r"\bremove.*watermark",
    r"\bimage.*edit(ing)?\b",
    r"\bphoto.*edit(ing)?\b",
    r"\bdall.?e\b",
    r"\bmidjourney\b",
    r"\bcreate.*poster\b",
    r"\bcreate.*flyer\b",
    r"\bcreate.*logo\b",
    r"\btelegram.*sticker",
    r"\bimage.*prompt\b",
    r"\bflux.*image\b",
]

ADMIN_TITLE_PATTERNS = [
    r"^remind me\b",
    r"\bgoogle.*task",
    r"^υπενθύμιση\b",
    r"\bθύμισε μου\b",
]

TRIVIAL_TITLE_PATTERNS = [
    r"^new chat$",
    r"^untitled$",
    r"^new conversation$",
]

# Content patterns that indicate failed/refused requests (auto-exclude if short)
FAILED_CONTENT_PATTERNS = [
    r"i'?m sorry.{0,30}can'?t (generate|create|help|assist)",
    r"i (cannot|can't) (generate|create) (images|photos|pictures)",
    r"i'?m unable to (generate|create|produce)",
    r"this request.{0,30}violates",
    r"i apologize.{0,50}(can'?t|cannot|don'?t)",
]

# ─── Soft Scoring Patterns (title-based adjustments) ──────────────────────────

SOFT_EXCLUDE_TITLE = [
    # Pure translation requests (unless very long)
    r"^translate\b.{0,40}(to|into)\b",
    r"\btranslate (to|into)\b",

    # Simple admin/meta questions
    r"^(connecting|setting up|how to use)\b",

    # Image creation (soft penalty)
    r"\bimage\b.*\bgenerat",
    r"\bgenerat.*\bimage\b",
    r"\bcreate.*\bimage\b",
    r"\bdesign.*\bimage\b",
]

# ─── Frontmatter Parser ────────────────────────────────────────────────────────

def parse_frontmatter(filepath: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter → (meta, body)."""
    content = filepath.read_text(encoding="utf-8", errors="replace")
    if not content.startswith("---"):
        return {}, content
    try:
        end = content.index("---", 3)
        meta = yaml.safe_load(content[3:end]) or {}
        body = content[end + 3:].strip()
        return meta, body
    except (ValueError, yaml.YAMLError):
        return {}, content


# ─── Scorer ────────────────────────────────────────────────────────────────────

def score_conversation(filepath: Path, source: str) -> tuple[int, list[str], bool, str]:
    """
    Returns: (score, reasons, force_keep, keep_reason)
    - force_keep: always keep regardless of score
    - keep_reason: why it's force-kept (for report)
    """
    meta, body = parse_frontmatter(filepath)
    body_lower = body[:3000].lower()   # sample for pattern matching
    reasons: list[str] = []
    score = 60  # Base

    title = str(meta.get("title", filepath.stem)).strip()
    title_lower = title.lower()
    characters = int(meta.get("characters", 0))
    messages = int(meta.get("messages", 0))
    has_code = bool(meta.get("has_code", False))
    topics = [str(t).lower() for t in (meta.get("topics") or [])]

    # ── 1. Hard exclusion: near-empty ──────────────────────────────────────
    if characters < 300:
        return -100, [f"chars={characters} → HARD EXCLUDE (near-empty)"], False, ""

    if messages == 1 and characters < 500:
        return -100, [f"msgs=1 + chars={characters} → HARD EXCLUDE (no exchange)"], False, ""

    # ── 2. Force keep: research conversations ──────────────────────────────
    is_research = detect_research(body_lower, source, meta)
    if is_research:
        reasons.append("RESEARCH detected → force keep")
        # Extra bonus for substantive research
        if characters > 10000:
            reasons.append(f"chars={characters} (substantive research)")
        return score + 40, reasons, True, "research"

    # ── 3. Force keep: very long conversations ────────────────────────────
    if characters > 40000 and messages >= 3:
        reasons.append(f"LONG: chars={characters}, msgs={messages} → force keep")
        return score + 30, reasons, True, "long_content"

    # ── 4. Character count scoring ─────────────────────────────────────────
    char_adj = 0
    if   characters < 800:   char_adj = -45
    elif characters < 1500:  char_adj = -30
    elif characters < 2500:  char_adj = -20
    elif characters < 4000:  char_adj = -10
    elif characters < 7000:  char_adj = 0
    elif characters < 15000: char_adj = +10
    elif characters < 30000: char_adj = +20
    else:                    char_adj = +25

    if char_adj != 0:
        score += char_adj
        reasons.append(f"chars={characters} → {char_adj:+d}")

    # ── 5. Message count scoring ───────────────────────────────────────────
    msg_adj = 0
    if   messages <= 1:  msg_adj = -30
    elif messages == 2:  msg_adj = -8
    elif messages <= 3:  msg_adj = 0
    elif messages <= 7:  msg_adj = +5
    elif messages <= 14: msg_adj = +10
    else:                msg_adj = +15

    # Content-density correction: a single-message conversation with
    # substantial content (e.g. a long code dump or detailed prompt) is
    # not "empty" — soften the harsh msgs=1 penalty proportionally.
    if messages <= 1 and characters >= 3000:
        density_relief = min(20, (characters - 3000) // 500)
        msg_adj += density_relief
        if density_relief:
            reasons.append(f"density relief ({characters}c in 1 msg) → +{density_relief}")

    if msg_adj != 0:
        score += msg_adj
        reasons.append(f"msgs={messages} → {msg_adj:+d}")

    # ── 6. Trivial title patterns (soft, only if also short) ───────────────
    is_trivial_title = False
    for pattern in TRIVIAL_TITLE_PATTERNS:
        if re.search(pattern, title_lower, re.IGNORECASE):
            is_trivial_title = True
            if characters < 10000:  # Only penalize if also short
                score -= 20
                reasons.append(f"trivial title ({pattern[:20]}) + short → -20")
            break

    # ── 7. Image generation title patterns (strong penalty if short) ───────
    for pattern in IMAGE_TITLE_PATTERNS:
        if re.search(pattern, title_lower, re.IGNORECASE):
            penalty = -40 if characters < 8000 else -15
            score += penalty
            reasons.append(f"image title pattern → {penalty:+d}")
            break

    # ── 8. Admin patterns (hard regardless of length) ─────────────────────
    for pattern in ADMIN_TITLE_PATTERNS:
        if re.search(pattern, title_lower, re.IGNORECASE):
            score -= 40
            reasons.append(f"admin title pattern → -40")
            break

    # ── 9. Soft title penalties ────────────────────────────────────────────
    for pattern in SOFT_EXCLUDE_TITLE:
        if re.search(pattern, title_lower, re.IGNORECASE):
            penalty = -20 if characters < 5000 else -10
            score += penalty
            reasons.append(f"soft title pattern → {penalty:+d}")
            break

    # ── 10. Failed/refused content (auto-exclude if short) ────────────────
    for pattern in FAILED_CONTENT_PATTERNS:
        if re.search(pattern, body_lower, re.IGNORECASE):
            if characters < 5000:
                score -= 40
                reasons.append(f"failed content pattern → -40")
            break

    # ── 11. Quality bonuses ────────────────────────────────────────────────
    if has_code:
        score += 8
        reasons.append("has_code → +8")

    # Substantive multi-turn conversation
    if characters > 10000 and messages >= 4:
        score += 12
        reasons.append(f"substantive ({characters}c, {messages}m) → +12")

    return score, reasons, False, ""


# ─── Filter Runner ─────────────────────────────────────────────────────────────

def get_excluded_path(folder_name: str, filepath: Path) -> Path:
    return EXCLUDED_DIR / folder_name / filepath.name


def run_filter(dry_run: bool = False, verbose: bool = False, threshold: int = MIN_SCORE_KEEP):
    mode = "[DRY RUN] " if dry_run else ""
    print(f"\n{mode}Quality Filter — UltraRAG Curation")
    print(f"Threshold: score ≥ {threshold} to keep\n")

    if not dry_run:
        for _, (conv_dir, folder_name) in SOURCES.items():
            (EXCLUDED_DIR / folder_name).mkdir(parents=True, exist_ok=True)

    stats = {"total": 0, "kept": 0, "excluded": 0, "force_kept": 0, "research_kept": 0, "long_kept": 0}
    excluded_list: list[tuple] = []
    kept_list: list[tuple] = []

    for source_key, (conv_dir, folder_name) in SOURCES.items():
        if not conv_dir.exists():
            continue

        files = sorted(conv_dir.glob("*.md"))
        src_excl = src_kept = 0
        print(f"── {source_key.upper()} ({len(files)} conversations) ──")

        for filepath in files:
            stats["total"] += 1
            score, reasons, force_keep, keep_reason = score_conversation(filepath, source_key)

            if force_keep or score >= threshold:
                stats["kept"] += 1
                src_kept += 1
                if force_keep:
                    stats["force_kept"] += 1
                    if keep_reason == "research":
                        stats["research_kept"] += 1
                    elif keep_reason == "long_content":
                        stats["long_kept"] += 1
                if verbose:
                    tag = "KEEP(forced)" if force_keep else "keep"
                    print(f"  ✓ {tag} [{score:4d}] {filepath.name}")
                    for r in reasons[:2]:
                        print(f"           {r}")
                kept_list.append((source_key, folder_name, filepath, score, reasons))
            else:
                stats["excluded"] += 1
                src_excl += 1
                excluded_list.append((source_key, folder_name, filepath, score, reasons))
                if verbose:
                    print(f"  ✗ EXCL   [{score:4d}] {filepath.name}")
                    for r in reasons[:3]:
                        print(f"           {r}")
                if not dry_run:
                    dest = get_excluded_path(folder_name, filepath)
                    shutil.move(str(filepath), str(dest))

        print(f"  Kept: {src_kept}, Excluded: {src_excl}")

    # ── Summary ─────────────────────────────────────────────────────────────
    total = stats["total"] or 1  # guard against division by zero
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Total conversations:     {stats['total']}")
    print(f"Kept for UltraRAG:      {stats['kept']} ({stats['kept']/total*100:.1f}%)")
    print(f"  • Force-kept total:   {stats['force_kept']}")
    print(f"    - Research convos:  {stats['research_kept']}")
    print(f"    - Long content:     {stats['long_kept']}")
    print(f"Excluded:               {stats['excluded']} ({stats['excluded']/total*100:.1f}%)")
    if dry_run:
        print(f"\n[DRY RUN] No files moved. Run without --dry-run to apply.")

    # ── Report ───────────────────────────────────────────────────────────────
    report_path = OUTPUT_DIR / ("QUALITY_FILTER_REPORT.dryrun.md" if dry_run else "QUALITY_FILTER_REPORT.md")
    write_report(report_path, excluded_list, kept_list, stats, dry_run, threshold)
    print(f"\nReport: {report_path}")

    return stats


def write_report(report_path: Path, excluded: list, kept: list, stats: dict, dry_run: bool, threshold: int):
    total = stats["total"]
    lines = [
        "# Quality Filter Report — UltraRAG Curation",
        "",
        f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*  ",
        f"{'*DRY RUN — no files moved*  ' if dry_run else ''}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total conversations | {stats['total']} |",
        f"| Kept for UltraRAG | {stats['kept']} ({stats['kept']/total*100:.1f}%) |",
        f"| Force-kept (research) | {stats['research_kept']} |",
        f"| Force-kept (long content) | {stats['long_kept']} |",
        f"| Excluded | {stats['excluded']} ({stats['excluded']/total*100:.1f}%) |",
        f"| Quality threshold | score ≥ {threshold} |",
        "",
        "## Exclusion Criteria",
        "",
        "| Criterion | Effect |",
        "|-----------|--------|",
        "| characters < 300 | Hard exclude |",
        "| messages = 1 + chars < 500 | Hard exclude |",
        "| Research content detected | Force keep |",
        "| chars > 40k + msgs ≥ 3 | Force keep |",
        "| chars 1500-4000 | -10 to -30 |",
        "| messages ≤ 2 | -8 to -30 |",
        "| Image generation title | -40 if short |",
        "| Substantive (10k+ chars, 4+ msgs) | +12 |",
        "| has_code | +8 |",
        "",
    ]

    for src, label in [("chatgpt", "ChatGPT"), ("claude", "Claude"), ("gemini", "Gemini")]:
        src_excl = [(f, s, r) for (sk, fn, f, s, r) in excluded if sk == src]
        if not src_excl:
            continue
        lines += [
            f"## Excluded: {label} ({len(src_excl)})",
            "",
            "| Score | File | Top Reason |",
            "|-------|------|------------|",
        ]
        for fp, sc, rs in sorted(src_excl, key=lambda x: x[1]):
            reason = rs[0] if rs else ""
            lines.append(f"| {sc} | {fp.name} | {reason} |")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def restore_excluded():
    """Move all files from _excluded/ back to their source conversation dirs."""
    if not EXCLUDED_DIR.exists():
        print("Nothing to restore: _excluded/ does not exist.")
        return

    restored = 0
    for source_key, (conv_dir, folder_name) in SOURCES.items():
        excl_dir = EXCLUDED_DIR / folder_name
        if not excl_dir.exists():
            continue
        conv_dir.mkdir(parents=True, exist_ok=True)
        for fp in excl_dir.glob("*.md"):
            dest = conv_dir / fp.name
            if dest.exists():
                print(f"  SKIP (already exists): {fp.name}")
                continue
            shutil.move(str(fp), str(dest))
            restored += 1

    # Clean up empty _excluded subdirs
    for d in EXCLUDED_DIR.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    if EXCLUDED_DIR.exists() and not any(EXCLUDED_DIR.iterdir()):
        EXCLUDED_DIR.rmdir()

    print(f"Restored {restored} files from _excluded/ back to source directories.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quality filter AI conversations for UltraRAG")
    parser.add_argument("--dry-run", action="store_true", help="Preview without moving files")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-file scoring")
    parser.add_argument("--threshold", type=int, default=MIN_SCORE_KEEP, help=f"Min score to keep (default: {MIN_SCORE_KEEP})")
    parser.add_argument("--restore", action="store_true", help="Move all files from _excluded/ back to their source dirs")
    args = parser.parse_args()

    if args.restore:
        restore_excluded()
    else:
        run_filter(dry_run=args.dry_run, verbose=args.verbose, threshold=args.threshold)
