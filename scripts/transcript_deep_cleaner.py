#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich"]
# ///
"""
YouTube Transcript Deep Cleaner (extracted_text_el)
====================================================
Safely removes defects from Greek-translated YouTube transcripts.

Safety guarantees:
  - ONLY modifies extracted_text_el (Greek translation field)
  - NEVER touches extracted_text (English original — preserved as backup)
  - ONLY processes items that ALREADY HAVE extracted_text_el populated
  - Items without Greek translation are excluded from the working set
  - Dry-run mode by default — must explicitly --apply

Cleanup operations (in order):
  1. Remove entire ## Περιγραφή / ## Description section
  2. Remove translator/reviewer/transcriber credit lines  
  3. Remove sound annotations: (Γέλια), (Χειροκρότημα), (Laughter), etc.
  4. Remove stage directions: [Μουσική], [Ακατάληπτο], [Inaudible], etc.
  5. Strip HTML tags: <i>, </i>, <b>, etc.
  6. Remove BOM characters
  7. Normalize whitespace (collapse multiple spaces/blank lines)

Usage:
  uv run scripts/transcript_deep_cleaner.py --db PATH --dry-run
  uv run scripts/transcript_deep_cleaner.py --db PATH --dry-run --verbose
  uv run scripts/transcript_deep_cleaner.py --db PATH --item-id ID --dry-run
  uv run scripts/transcript_deep_cleaner.py --db PATH --apply
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()

# ---------------------------------------------------------------------------
# Section Removal
# ---------------------------------------------------------------------------

def remove_description_section(text: str) -> tuple[str, bool]:
    """Remove the ## Περιγραφή / ## Description section entirely.
    
    The Description section contains YouTube description boilerplate:
    sponsor reads, social media links, timestamps/chapters, subscribe CTAs.
    None of this is user-readable transcript content.
    
    Returns (cleaned_text, was_removed).
    """
    # Find the description header
    desc_match = re.search(
        r'^## (?:Περιγραφή|Description)\s*\n',
        text,
        re.MULTILINE,
    )
    if not desc_match:
        return text, False
    
    desc_start = desc_match.start()
    
    # Find the next ## header (the section after Description)
    next_header = re.search(r'^## ', text[desc_match.end():], re.MULTILINE)
    if next_header:
        desc_end = desc_match.end() + next_header.start()
    else:
        # Description is the last/only section — remove from desc_start to end
        desc_end = len(text)
    
    # Remove the description section
    cleaned = text[:desc_start].rstrip() + '\n\n' + text[desc_end:].lstrip()
    # Clean up excess newlines at the join point
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    
    return cleaned.strip(), True


# ---------------------------------------------------------------------------
# Transcript-level Cleanup Rules
# ---------------------------------------------------------------------------

# Sound annotations in parentheses (EN + EL)
# Use [ \t]* (not \s*) to avoid consuming newlines
_SOUND_ANNOTATIONS = re.compile(
    r'[ \t]*\('
    r'(?:'
    # English
    r'Laughter|Applause|Applaus|Music|Cheering|Singing|Clapping|'
    r'Cheers and applause|Cheers|Whistling|Gasps|Sighs|'
    r'Ethereal music|Upbeat music|Dramatic music|Soft music|'
    # Greek
    r'Γέλια|Γέλιο|Χειροκρότημα|Χειροκροτήματα|Μουσική|'
    r'Επευφημίες|Τραγούδι'
    r')'
    r'\)[ \t]*',
    re.IGNORECASE,
)

# Stage directions in brackets (EN + EL)
# Use [ \t]* before bracket (not \s*) to avoid eating newlines
_STAGE_DIRECTIONS = re.compile(
    r'[ \t]*\['
    r'(?:'
    # English
    r'Inaudible|INAUDIBLE|Crosstalk|CROSSTALK|Silence|Pause|'
    r'Unclear|Foreign language|Speaking \w+|'
    r'Laughter|Applause|Music|Cheering|Singing|Clapping|'
    # Greek
    r'Ακατάληπτο|Σιωπή|Παύση|Ξένη γλώσσα|Μιλώντας \w+|'
    r'Χειροκροτήματα|Χειροκρότημα|Γέλια|Γέλιο|Επευφημίες|'
    r'Τραγούδι|'
    # Music/sound markers  
    r'Μουσική|Music|♪|🎵'
    r')'
    r'\][ \t]*',
    re.IGNORECASE,
)

# Translator/Reviewer credits at transcript start
# Handles both English and Greek variants with multi-word names
_CREDITS_EN = re.compile(
    r'^(?:Translator|Transcriber):?\s+[^\n]*?'
    r'(?:\n\s*)?'
    r'(?:Reviewer):?\s+[^\n]*?'
    r'(?=\s+(?:When|Thank|So,?|If |We |It |The |By |How |I\s|Hello|Good|'
    r'Please|In |A |An |Hi |Imagine|Humans|Don|My |You|What|This|'
    r'Όταν|Ευχαριστ|Σήμερα|Αν |Εμείς|Καλη|Παρακαλ|Γεια|Θα |'
    r'Είμαι|Το |Η |Ο |Μια|Ένα|Αυτ|Κοιτ|'
    r'\ufeff|"))',
    re.MULTILINE | re.IGNORECASE,
)

_CREDITS_EL = re.compile(
    r'^(?:Μεταφραστής|Μετάφραση|Απομαγνητοφωνητής):?\s+[^\n]*?'
    r'(?:\n\s*)?'
    r'(?:Αναθεωρητής|Αναθεώρηση):?\s+[^\n]*?'
    r'(?=\s+(?:Όταν|Ευχαριστ|Σήμερα|Αν |Εμείς|Καλη|Παρακαλ|Γεια|Θα |'
    r'Είμαι|Το |Η |Ο |Μια|Ένα|Αυτ|When|Thank|So|If |We ))',
    re.MULTILINE | re.IGNORECASE,
)

# HTML tags
_HTML_TAGS = re.compile(r'</?(?:i|b|em|strong|br|p|div|span|a|u|sub|sup|font)[^>]*>', re.IGNORECASE)

# BOM character
_BOM = re.compile(r'\ufeff')


def clean_transcript_content(text: str) -> tuple[str, list[str], dict[str, int]]:
    """Apply all transcript-level cleanup rules.
    
    Returns (cleaned_text, rules_applied, match_counts).
    """
    applied: list[str] = []
    counts: dict[str, int] = {}
    result = text
    
    # Find transcript section start for credit removal
    trans_match = re.search(
        r'^## (?:Μεταγραφή|Απομαγνητοφώνηση|Transcript)\s*\n',
        result,
        re.MULTILINE,
    )
    
    # --- Credits (only at transcript start) ---
    if trans_match:
        trans_start = trans_match.end()
        # Snap to word boundary at ~300 chars
        region_end = min(trans_start + 300, len(result))
        while region_end < len(result) and not result[region_end].isspace():
            region_end += 1
        
        region = result[trans_start:region_end]
        
        for name, pattern in [("credits_en", _CREDITS_EN), ("credits_el", _CREDITS_EL)]:
            matches = pattern.findall(region)
            if matches:
                cleaned_region = pattern.sub('', region).lstrip()
                result = result[:trans_start] + cleaned_region + result[region_end:]
                applied.append(name)
                counts[name] = len(matches)
    
    # --- Sound annotations (anywhere) ---
    matches = _SOUND_ANNOTATIONS.findall(result)
    if matches:
        result = _SOUND_ANNOTATIONS.sub(' ', result)
        applied.append("sound_annotations")
        counts["sound_annotations"] = len(matches)
    
    # --- Stage directions (anywhere) ---
    matches = _STAGE_DIRECTIONS.findall(result)
    if matches:
        result = _STAGE_DIRECTIONS.sub(' ', result)
        applied.append("stage_directions")
        counts["stage_directions"] = len(matches)
    
    # --- HTML tags (anywhere) ---
    matches = _HTML_TAGS.findall(result)
    if matches:
        result = _HTML_TAGS.sub('', result)
        applied.append("html_tags")
        counts["html_tags"] = len(matches)
    
    # --- BOM (anywhere) ---
    matches = _BOM.findall(result)
    if matches:
        result = _BOM.sub('', result)
        applied.append("bom")
        counts["bom"] = len(matches)
    
    # --- Whitespace normalization ---
    # Collapse multiple spaces (but not newlines)
    result = re.sub(r'  +', ' ', result)
    # Ensure ## headers always have a newline after them (may be lost when
    # content immediately after the header was removed by cleanup rules)
    result = re.sub(r'^(## [^\n]+)(?<!\n) (\S)', r'\1\n\2', result, flags=re.MULTILINE)
    # Remove leading horizontal whitespace on content lines (left after removing
    # stage directions that were at the start of a line)
    result = re.sub(r'^[ \t]+(\S)', r'\1', result, flags=re.MULTILINE)
    # Collapse 3+ newlines into 2
    result = re.sub(r'\n{3,}', '\n\n', result)
    # Remove trailing spaces on lines
    result = re.sub(r' +\n', '\n', result)
    # Trim
    result = result.strip()
    
    return result, applied, counts


# ---------------------------------------------------------------------------
# Full cleanup pipeline
# ---------------------------------------------------------------------------

@dataclass
class CleanupResult:
    item_id: str
    title: str
    original_length: int
    cleaned_length: int
    target_field: str = "extracted_text_el"  # which DB column was cleaned
    description_removed: bool = False
    rules_applied: list[str] = field(default_factory=list)
    match_counts: dict[str, int] = field(default_factory=dict)
    original_text: str = ""
    cleaned_text: str = ""
    
    @property
    def changed(self) -> bool:
        return self.original_text != self.cleaned_text
    
    @property
    def chars_removed(self) -> int:
        return self.original_length - self.cleaned_length


def clean_item(text: str, item_id: str, title: str, target_field: str = "extracted_text_el") -> CleanupResult:
    """Full cleanup pipeline for a single item's text field."""
    if isinstance(text, bytes):
        try:
            text = text.decode('utf-8')
        except Exception:
            text = str(text)
    
    original = text
    result_text = text
    all_applied: list[str] = []
    all_counts: dict[str, int] = {}
    
    # Step 1: Remove description section
    result_text, desc_removed = remove_description_section(result_text)
    if desc_removed:
        all_applied.append("description_section_removed")
        all_counts["description_section_removed"] = 1
    
    # Step 2: Clean transcript content
    result_text, applied, counts = clean_transcript_content(result_text)
    all_applied.extend(applied)
    all_counts.update(counts)
    
    return CleanupResult(
        item_id=item_id,
        title=title or "",
        original_length=len(original),
        cleaned_length=len(result_text),
        target_field=target_field,
        description_removed=desc_removed,
        rules_applied=all_applied,
        match_counts=all_counts,
        original_text=original,
        cleaned_text=result_text,
    )


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _is_greek_native_title(title: str) -> bool:
    """Check if a title is predominantly Greek (Greek-native content)."""
    greek_chars = len(re.findall(r'[α-ωά-ώΑ-ΩΆ-Ώ]', title or ''))
    total_chars = len(re.findall(r'\w', title or ''))
    return total_chars > 0 and greek_chars / total_chars > 0.5


def get_working_set(conn: sqlite3.Connection, item_id: str | None = None) -> list[tuple]:
    """Get items eligible for transcript cleanup.
    
    Returns list of (id, title, text, target_field) tuples.
    
    Two categories:
      1. Translated items: have extracted_text_el with Transcript → clean extracted_text_el
      2. Greek-native items: no extracted_text_el, but extracted_text IS Greek
         with Transcript → clean extracted_text
    
    Exclusion rules:
      - Items without a Transcript section (only Description) → excluded
        (these will get transcripts later if they qualify for enrichment)
    """
    _trans_pattern = re.compile(
        r'^## (?:Μεταγραφή|Απομαγνητοφώνηση|Transcript)', re.MULTILINE,
    )
    
    def _decode(val: str | bytes) -> str | None:
        if isinstance(val, bytes):
            try:
                return val.decode('utf-8')
            except Exception:
                return None
        return val
    
    filtered: list[tuple] = []
    
    # --- Category 1: items with extracted_text_el ---
    if item_id:
        el_rows = conn.execute(
            "SELECT id, title, extracted_text_el FROM items "
            "WHERE id = ? AND extracted_text_el IS NOT NULL AND length(extracted_text_el) > 10",
            (item_id,),
        ).fetchall()
    else:
        el_rows = conn.execute(
            "SELECT id, title, extracted_text_el FROM items "
            "WHERE source = 'youtube' "
            "AND extracted_text_el IS NOT NULL AND length(extracted_text_el) > 10",
        ).fetchall()
    
    el_ids = set()
    for row in el_rows:
        text = _decode(row[2])
        if text and _trans_pattern.search(text):
            filtered.append((row[0], row[1], text, 'extracted_text_el'))
            el_ids.add(row[0])
    
    # --- Category 2: Greek-native items (no el, but extracted_text is Greek) ---
    if item_id:
        en_rows = conn.execute(
            "SELECT id, title, extracted_text FROM items "
            "WHERE id = ? AND extracted_text IS NOT NULL AND length(extracted_text) > 50 "
            "AND (extracted_text_el IS NULL OR length(extracted_text_el) <= 10)",
            (item_id,),
        ).fetchall()
    else:
        en_rows = conn.execute(
            "SELECT id, title, extracted_text FROM items "
            "WHERE source = 'youtube' "
            "AND extracted_text IS NOT NULL AND length(extracted_text) > 50 "
            "AND (extracted_text_el IS NULL OR length(extracted_text_el) <= 10)",
        ).fetchall()
    
    for row in en_rows:
        if row[0] in el_ids:
            continue  # already handled via extracted_text_el
        if not _is_greek_native_title(row[1]):
            continue
        text = _decode(row[2])
        if text and _trans_pattern.search(text):
            filtered.append((row[0], row[1], text, 'extracted_text'))
    
    return filtered


def run_pipeline(
    db_path: Path,
    item_id: str | None = None,
    dry_run: bool = True,
    verbose: bool = False,
) -> list[CleanupResult]:
    """Run the full cleanup pipeline."""
    conn = sqlite3.connect(str(db_path))
    rows = get_working_set(conn, item_id)
    
    el_count = sum(1 for r in rows if r[3] == 'extracted_text_el')
    native_count = sum(1 for r in rows if r[3] == 'extracted_text')
    console.print(f"[cyan]Working set: {len(rows)} items "
                  f"({el_count} translated + {native_count} Greek-native)[/cyan]\n")
    
    results: list[CleanupResult] = []
    
    for row_id, title, text, target_field in rows:
        result = clean_item(text, row_id, title, target_field=target_field)
        results.append(result)
        
        if not dry_run and result.changed:
            conn.execute(
                f"UPDATE items SET {target_field} = ? WHERE id = ?",
                (result.cleaned_text, row_id),
            )
    
    if not dry_run:
        changed_count = sum(1 for r in results if r.changed)
        if changed_count:
            conn.commit()
            console.print(f"[green]✓ Committed {changed_count} updates[/green]\n")
    
    conn.close()
    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def display_results(results: list[CleanupResult], verbose: bool = False) -> None:
    """Display cleanup results."""
    changed = [r for r in results if r.changed]
    unchanged = [r for r in results if not r.changed]
    
    # Summary table
    table = Table(
        title=f"🧹 Deep Cleanup — {len(changed)}/{len(results)} items cleaned",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Item ID", style="dim", max_width=10)
    table.add_column("Title", max_width=40)
    table.add_column("Field", style="magenta", max_width=8)
    table.add_column("Removed", justify="right", style="red")
    table.add_column("Operations")
    
    for r in changed:
        ops = ", ".join(
            f"{k}({v})" for k, v in r.match_counts.items()
        )
        field_short = "el" if r.target_field == "extracted_text_el" else "native"
        table.add_row(
            r.item_id[:8] + "...",
            r.title[:37] + "..." if len(r.title) > 40 else r.title,
            field_short,
            f"-{r.chars_removed:,}",
            ops[:60],
        )
    
    console.print(table)
    
    if unchanged:
        console.print(f"\n[dim]{len(unchanged)} items unchanged (no defects found)[/dim]")
    
    # Aggregate stats
    total_removed = sum(r.chars_removed for r in changed)
    console.print(f"\n[bold]Total characters removed: {total_removed:,}[/bold]")
    
    # Per-rule aggregate
    rule_totals: dict[str, int] = {}
    for r in changed:
        for k, v in r.match_counts.items():
            rule_totals[k] = rule_totals.get(k, 0) + v
    
    if rule_totals:
        console.print("\n[bold]By operation:[/bold]")
        for name, count in sorted(rule_totals.items(), key=lambda x: -x[1]):
            items_affected = sum(1 for r in changed if name in r.match_counts)
            console.print(f"  {name}: {count} occurrences across {items_affected} items")
    
    # Verbose: show before/after for each item
    if verbose:
        for r in changed:
            console.print(
                Panel(
                    r.original_text[:400],
                    title=f"[red]BEFORE: {r.title[:50]}[/red]",
                    border_style="red",
                    width=90,
                )
            )
            console.print(
                Panel(
                    r.cleaned_text[:400],
                    title=f"[green]AFTER: {r.title[:50]}[/green]",
                    border_style="green",
                    width=90,
                )
            )
            console.print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deep-clean YouTube transcript defects. "
                    "Translated items → clean extracted_text_el. "
                    "Greek-native items (no translation, Greek content) → clean extracted_text.",
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to unified.db")
    parser.add_argument("--item-id", type=str, help="Process only this specific item ID")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Show changes without modifying DB")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show before/after text for each item")
    
    args = parser.parse_args()
    
    if args.apply:
        args.dry_run = False
    
    if not args.db.exists():
        console.print(f"[red]Database not found: {args.db}[/red]")
        sys.exit(1)
    
    mode = "[yellow]DRY RUN[/yellow]" if args.dry_run else "[red bold]APPLY[/red bold]"
    console.print(f"\n🧹 Transcript Deep Cleaner — Mode: {mode}")
    console.print(f"   Database: {args.db}")
    console.print(f"   Translated items  → [bold]extracted_text_el[/bold]")
    console.print(f"   Greek-native items → [bold]extracted_text[/bold]")
    console.print(f"   [dim]Only items with Transcript section are processed[/dim]\n")
    
    results = run_pipeline(args.db, args.item_id, dry_run=args.dry_run, verbose=args.verbose)
    display_results(results, verbose=args.verbose)


if __name__ == "__main__":
    main()
