#!/usr/bin/env python3
"""
Rebuild output/{chatgpt-full,claude-full,gemini}/INDEX.md from the YAML
frontmatter of each conversations/*.md file.

Usage:
    python3 scripts/rebuild_indexes.py [--output-dir output] [--dry-run]
"""

import sys
import argparse
from datetime import date, datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required. Install with: pip3 install pyyaml")
    sys.exit(1)

SOURCES = {
    "chatgpt-full": "CHATGPT",
    "claude-full": "CLAUDE",
    "gemini": "GEMINI",
}


def read_frontmatter(filepath: Path) -> dict:
    """Return the parsed frontmatter dict; raises ValueError if missing or invalid."""
    content = filepath.read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise ValueError("no frontmatter")
    end = content.find("\n---", 3)
    if end == -1:
        raise ValueError("unterminated frontmatter")
    meta = yaml.safe_load(content[3:end])
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a mapping")
    return meta


def build_index(conv_dir: Path, label: str) -> tuple[list[str], int, list[tuple[str, str]]]:
    """Returns (index lines, row count, [(filename, error)])."""
    rows = []
    failures = []
    for fp in sorted(conv_dir.glob("*.md")):
        try:
            meta = read_frontmatter(fp)
        except (ValueError, yaml.YAMLError, UnicodeDecodeError) as e:
            failures.append((fp.name, str(e).splitlines()[0] if str(e) else type(e).__name__))
            continue
        d = meta.get("date") or ""
        d = d.isoformat() if isinstance(d, (date, datetime)) else str(d)
        title = str(meta.get("title") or fp.stem).replace("\n", " ").replace("|", "-")[:60]
        rows.append((d, title, fp.name, int(meta.get("messages") or 0), int(meta.get("characters") or 0)))

    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [
        f"# {label} Conversation Index",
        "",
        f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
        f"**Total conversations: {len(rows)}**",
        "",
        "| Date | Title | Messages | Characters |",
        "|------|-------|----------|------------|",
    ]
    for d, title, name, messages, characters in rows:
        lines.append(f"| {d[:10]} | [{title}](conversations/{name}) | {messages} | {characters:,} |")
    return lines, len(rows), failures


def main():
    parser = argparse.ArgumentParser(description="Rebuild per-source INDEX.md files from conversation frontmatter")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Output root (default: output)")
    parser.add_argument("--dry-run", action="store_true", help="Print counts only, write nothing")
    args = parser.parse_args()

    for folder, label in SOURCES.items():
        conv_dir = args.output_dir / folder / "conversations"
        if not conv_dir.exists():
            print(f"{folder}: no conversations/ dir, skipped")
            continue
        n_files = len(list(conv_dir.glob("*.md")))
        lines, n_rows, failures = build_index(conv_dir, label)
        print(f"{folder}: {n_files} files, {n_rows} indexed, {len(failures)} failed to parse")
        for name, err in failures:
            print(f"  PARSE FAIL {name}: {err}")
        if not args.dry_run:
            index_path = args.output_dir / folder / "INDEX.md"
            index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"  Wrote {index_path}")


if __name__ == "__main__":
    main()
