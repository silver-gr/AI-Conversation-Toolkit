#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich"]
# ///
"""
YouTube Transcript Cleaner
==========================
Removes non-content artifacts from YouTube transcripts in the x3lixi unified.db:
  - Sound annotations: (Laughter), (Applause), (Χειροκρότημα), etc.
  - Translator/Reviewer credit lines at the start of transcripts
  - Extensible via CLEANUP_RULES list

Usage:
  uv run scripts/transcript_cleaner.py --db PATH --item-id ID --dry-run
  uv run scripts/transcript_cleaner.py --db PATH --item-id ID --apply
  uv run scripts/transcript_cleaner.py --db PATH --all-youtube --dry-run
  uv run scripts/transcript_cleaner.py --db PATH --all-youtube --apply
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()

# ---------------------------------------------------------------------------
# Cleanup Rules
# ---------------------------------------------------------------------------
# Each rule is a dict with:
#   name        – human-readable identifier
#   description – what it does
#   pattern     – regex pattern (compiled at load time)
#   replacement – what to replace with (usually "" or " ")
#   scope       – "body" (anywhere) or "transcript_start" (only first ~200 chars
#                 of the ## Transcript section)
# ---------------------------------------------------------------------------

CLEANUP_RULES: list[dict] = [
    # --- Sound Annotations (English) ---
    {
        "name": "sound_annotations_en",
        "description": "Remove English sound annotations in parentheses",
        "pattern": r'\s*\((?:Laughter|Applause|Applaus|Music|Cheering|Singing|Clapping|Cheers and applause|Cheers|Whistling|Gasps|Sighs)\)\s*',
        "replacement": " ",
        "scope": "body",
    },
    # --- Sound Annotations (Greek) ---
    {
        "name": "sound_annotations_el",
        "description": "Remove Greek sound annotations in parentheses",
        "pattern": r'\s*\((?:Χειροκρότημα|Χειροκροτήματα|Γέλια|Γέλιο|Μουσική|Επευφημίες|Τραγούδι)\)\s*',
        "replacement": " ",
        "scope": "body",
    },
    # --- Translator/Reviewer Credits (English) ---
    {
        "name": "translator_credits_en",
        "description": "Remove TED-style Translator/Transcriber + Reviewer credits",
        # Matches credit lines at transcript start. Handles:
        #   - Multi-word names (Peter van de Ven, Camille Martínez)
        #   - Newlines between Translator/Reviewer lines
        #   - Names with accents, dots, hyphens
        # Stops before actual content via lookahead for common sentence starters
        "pattern": r'^(?:Translator|Transcriber):?\s+[^\n]*?(?:\n\s*)?(?:Reviewer):?\s+[^\n]*?(?=\s+(?:When|Thank|So,?|If |We |It |The |By |How |I\s|Hello|Good|Please|In |A |An |Hi |Imagine|Humans|Don|My |You|What|This|\xef\xbb\xbf|"))',
        "replacement": "",
        "scope": "transcript_start",
    },
    # --- Translator/Reviewer Credits (Greek) ---
    {
        "name": "translator_credits_el",
        "description": "Remove Greek-style Μετάφραση/Αναθεώρηση credits",
        "pattern": r'^(?:Μετάφραση):?\s+[^\n]*?(?:\n\s*)?(?:Αναθεώρηση):?\s+[^\n]*?(?=\s+(?:Όταν|Ευχαριστ|Σήμερα|Αν |Εμείς|Καλη|Παρακαλ|Γεια|Φανταστ|Θα |Είμαι|Το |Η |Ο |Μια|Ένα))',
        "replacement": "",
        "scope": "transcript_start",
    },
]

# Compile patterns once
for rule in CLEANUP_RULES:
    rule["_compiled"] = re.compile(rule["pattern"], re.IGNORECASE | re.UNICODE | re.MULTILINE)


@dataclass
class CleanupResult:
    """Result of cleaning a single item."""
    item_id: str
    title: str
    original_length: int
    cleaned_length: int
    rules_applied: list[str] = field(default_factory=list)
    matches_found: dict[str, int] = field(default_factory=dict)
    original_text: str = ""
    cleaned_text: str = ""

    @property
    def changed(self) -> bool:
        return self.original_length != self.cleaned_length

    @property
    def chars_removed(self) -> int:
        return self.original_length - self.cleaned_length


def find_transcript_section_start(text: str) -> int:
    """Find the start of the ## Transcript section in the extracted_text."""
    match = re.search(r'^## Transcript\s*\n', text, re.MULTILINE)
    if match:
        return match.end()
    # Fallback: start of text
    return 0


def apply_rules(text: str, rules: list[dict]) -> tuple[str, list[str], dict[str, int]]:
    """Apply cleanup rules to text. Returns (cleaned_text, applied_rule_names, match_counts)."""
    applied: list[str] = []
    match_counts: dict[str, int] = {}
    result = text

    transcript_start = find_transcript_section_start(result)

    for rule in rules:
        pattern: re.Pattern = rule["_compiled"]
        scope = rule["scope"]

        if scope == "transcript_start":
            # Only apply within the first ~300 chars after ## Transcript
            # Snap region_end to the nearest word boundary to avoid cutting mid-word
            tentative_end = min(transcript_start + 300, len(result))
            # Walk forward to the next whitespace/newline to avoid splitting a word
            while tentative_end < len(result) and not result[tentative_end].isspace():
                tentative_end += 1
            region_end = tentative_end
            region = result[transcript_start:region_end]
            matches = pattern.findall(region)
            if matches:
                cleaned_region = pattern.sub(rule["replacement"], region)
                # Strip only leading whitespace (preserve trailing to maintain join)
                cleaned_region = cleaned_region.lstrip()
                result = result[:transcript_start] + cleaned_region + result[region_end:]
                applied.append(rule["name"])
                match_counts[rule["name"]] = len(matches)
        else:  # scope == "body"
            matches = pattern.findall(result)
            if matches:
                result = pattern.sub(rule["replacement"], result)
                applied.append(rule["name"])
                match_counts[rule["name"]] = len(matches)

    # Normalize multiple spaces into single space (but preserve newlines)
    result = re.sub(r'  +', ' ', result)
    # Remove trailing spaces on lines
    result = re.sub(r' +\n', '\n', result)

    return result, applied, match_counts


def clean_item(
    db_path: Path,
    item_id: str,
    dry_run: bool = True,
    field_name: str = "extracted_text",
) -> CleanupResult | None:
    """Clean a single item's transcript."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            f"SELECT id, title, {field_name} FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if not row:
            console.print(f"[red]Item {item_id} not found[/red]")
            return None

        item_id_db, title, text = row
        if not text:
            console.print(f"[yellow]Item {item_id} has no {field_name}[/yellow]")
            return None

        cleaned, applied, counts = apply_rules(text, CLEANUP_RULES)

        result = CleanupResult(
            item_id=item_id_db,
            title=title or "",
            original_length=len(text),
            cleaned_length=len(cleaned),
            rules_applied=applied,
            matches_found=counts,
            original_text=text,
            cleaned_text=cleaned,
        )

        if not dry_run and result.changed:
            conn.execute(
                f"UPDATE items SET {field_name} = ? WHERE id = ?",
                (cleaned, item_id),
            )
            conn.commit()
            console.print(f"[green]✓ Updated item {item_id}[/green]")

        return result
    finally:
        conn.close()


def clean_all_youtube(
    db_path: Path,
    dry_run: bool = True,
    field_name: str = "extracted_text",
) -> list[CleanupResult]:
    """Clean all YouTube items' transcripts."""
    conn = sqlite3.connect(str(db_path))
    results: list[CleanupResult] = []

    try:
        rows = conn.execute(
            f"SELECT id, title, {field_name} FROM items WHERE source = 'youtube' AND {field_name} IS NOT NULL AND {field_name} != ''",
        ).fetchall()

        console.print(f"[cyan]Processing {len(rows)} YouTube items...[/cyan]")

        for item_id, title, text in rows:
            cleaned, applied, counts = apply_rules(text, CLEANUP_RULES)

            result = CleanupResult(
                item_id=item_id,
                title=title or "",
                original_length=len(text),
                cleaned_length=len(cleaned),
                rules_applied=applied,
                matches_found=counts,
                original_text=text[:200],  # Only keep snippet for memory
                cleaned_text=cleaned[:200],
            )

            if result.changed:
                results.append(result)
                if not dry_run:
                    conn.execute(
                        f"UPDATE items SET {field_name} = ? WHERE id = ?",
                        (cleaned, item_id),
                    )

        if not dry_run and results:
            conn.commit()
            console.print(f"[green]✓ Updated {len(results)} items[/green]")

        return results
    finally:
        conn.close()


def display_result(result: CleanupResult, verbose: bool = False) -> None:
    """Display a single cleanup result."""
    if not result.changed:
        console.print(f"[dim]No changes for: {result.title}[/dim]")
        return

    table = Table(
        title=f"🧹 {result.title}",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Item ID", result.item_id)
    table.add_row("Characters removed", str(result.chars_removed))
    table.add_row("Original length", str(result.original_length))
    table.add_row("Cleaned length", str(result.cleaned_length))
    table.add_row("Rules applied", ", ".join(result.rules_applied))
    table.add_row(
        "Match counts",
        ", ".join(f"{k}: {v}" for k, v in result.matches_found.items()),
    )
    console.print(table)

    if verbose and result.changed:
        # Show diff-like comparison of the first few hundred chars
        console.print(
            Panel(
                result.original_text[:500],
                title="[red]Original (first 500 chars)[/red]",
                border_style="red",
            )
        )
        console.print(
            Panel(
                result.cleaned_text[:500],
                title="[green]Cleaned (first 500 chars)[/green]",
                border_style="green",
            )
        )


def display_summary(results: list[CleanupResult]) -> None:
    """Display summary table of all changed items."""
    changed = [r for r in results if r.changed]
    if not changed:
        console.print("[green]No items needed cleaning.[/green]")
        return

    table = Table(
        title=f"🧹 Cleanup Summary — {len(changed)} items affected",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Item ID", style="dim", max_width=38)
    table.add_column("Title", max_width=60)
    table.add_column("Chars Removed", justify="right")
    table.add_column("Rules Applied")

    for r in changed:
        table.add_row(
            r.item_id,
            r.title[:57] + "..." if len(r.title) > 60 else r.title,
            str(r.chars_removed),
            ", ".join(r.rules_applied),
        )

    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean YouTube transcript artifacts from unified.db",
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the unified.db SQLite database",
    )
    parser.add_argument(
        "--item-id",
        type=str,
        help="Process only this specific item ID",
    )
    parser.add_argument(
        "--all-youtube",
        action="store_true",
        help="Process all YouTube items",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show what would change without modifying DB (default)",
    )
    parser.add_argument(
        "--field",
        type=str,
        default="extracted_text",
        help="Database field to clean (default: extracted_text)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show before/after text snippets",
    )

    args = parser.parse_args()

    if args.apply:
        args.dry_run = False

    if not args.db.exists():
        console.print(f"[red]Database not found: {args.db}[/red]")
        sys.exit(1)

    mode = "[yellow]DRY RUN[/yellow]" if args.dry_run else "[red bold]APPLY[/red bold]"
    console.print(f"\n🧹 Transcript Cleaner — Mode: {mode}")
    console.print(f"   Database: {args.db}")
    console.print(f"   Field: {args.field}\n")

    if args.item_id:
        result = clean_item(args.db, args.item_id, dry_run=args.dry_run, field_name=args.field)
        if result:
            display_result(result, verbose=args.verbose)
    elif args.all_youtube:
        results = clean_all_youtube(args.db, dry_run=args.dry_run, field_name=args.field)
        display_summary(results)
    else:
        console.print("[red]Specify --item-id or --all-youtube[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
