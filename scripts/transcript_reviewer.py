#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich"]
# ///
"""
YouTube Transcript Defect Reviewer
===================================
Reviews YouTube transcripts for content defects/artifacts beyond sound annotations
and translator credits. Designed to run in parallel batches.

Detects:
  - Sound annotations (Laughter, Applause, etc.)
  - Translator/Reviewer/Transcriber credit lines
  - Encoding artifacts (BOM, HTML entities, escaped sequences)
  - Auto-generated boilerplate (Subscribe, Like, Follow, etc.)
  - Timestamp residues (00:00, [00:00:00])
  - Ad reads / sponsor segments
  - Music lyrics markers ([Verse], [Chorus])
  - Chapter markers from YouTube
  - Empty or near-empty transcripts
  - Non-ASCII control characters
  - Repeated text blocks (stutter/dupe from transcription)

Usage:
  uv run scripts/transcript_reviewer.py --db PATH --batch batch-01
  uv run scripts/transcript_reviewer.py --db PATH --batch all
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


# --- Defect Detection Rules ---
# Each rule returns a list of findings (strings) for a given text

DEFECT_DETECTORS: list[dict] = [
    {
        "name": "sound_annotations",
        "description": "Sound effect annotations in parentheses",
        "pattern": r'\((?:Laughter|Applause|Music|Cheering|Singing|Clapping|Whistling|Gasps|Sighs|Cheers|Χειροκρότημα|Γέλια|Μουσική|Ethereal music|Upbeat music|Dramatic music|Soft music)\)',
        "severity": "medium",
    },
    {
        "name": "translator_credits",
        "description": "Translator/Reviewer/Transcriber credit lines",
        "pattern": r'(?:Translator|Transcriber|Reviewer|Μετάφραση|Αναθεώρηση):?\s+[A-ZÀ-ÿΑ-Ω]',
        "severity": "medium",
    },
    {
        "name": "bom_marker",
        "description": "Byte Order Mark (BOM) character",
        "pattern": r'\ufeff',
        "severity": "low",
    },
    {
        "name": "html_entities",
        "description": "Unresolved HTML entities",
        "pattern": r'&(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-f]+);',
        "severity": "medium",
    },
    {
        "name": "html_tags",
        "description": "Residual HTML tags",
        "pattern": r'<(?:br|p|div|span|a|b|i|em|strong|ul|li|h[1-6])[^>]*>',
        "severity": "high",
    },
    {
        "name": "subscribe_boilerplate",
        "description": "YouTube subscribe/like boilerplate",
        "pattern": r'(?:subscribe|like.*button|hit.*bell|notification|turn on notifications|don\'t forget to subscribe|smash.*like)',
        "severity": "low",
    },
    {
        "name": "timestamp_residues",
        "description": "Timestamp residues from transcript formatting",
        "pattern": r'(?:\[\d{1,2}:\d{2}(?::\d{2})?\]|\b\d{1,2}:\d{2}:\d{2}\b)',
        "severity": "medium",
    },
    {
        "name": "music_lyrics_markers",
        "description": "Music lyrics section markers",
        "pattern": r'\[(?:Verse|Chorus|Bridge|Intro|Outro|Hook|Refrain|Pre-Chorus)\s*\d*\]',
        "severity": "high",
    },
    {
        "name": "chapter_markers",
        "description": "YouTube chapter markers/timestamps",
        "pattern": r'^\d{1,2}:\d{2}(?::\d{2})?\s+[A-ZΑ-Ω]',
        "severity": "low",
    },
    {
        "name": "sponsor_segments",
        "description": "Sponsor/ad read segments",
        "pattern": r'(?:this (?:video|episode) (?:is|was) (?:sponsored|brought to you) by|thanks? to .{3,30} for sponsoring|use (?:code|coupon)|discount code|special offer)',
        "severity": "low",
    },
    {
        "name": "control_characters",
        "description": "Non-printable control characters",
        "pattern": r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]',
        "severity": "medium",
    },
    {
        "name": "repeated_blocks",
        "description": "Repeated text blocks (likely transcription stutter)",
        "pattern": r'(\b\w{4,}\b(?:\s+\w+){2,5})\s+\1',
        "severity": "high",
    },
    {
        "name": "bracketed_stage_directions",
        "description": "Stage directions in brackets",
        "pattern": r'\[(?:inaudible|crosstalk|silence|pause|unclear|foreign language|speaking .+?)\]',
        "severity": "medium",
    },
    {
        "name": "auto_captions_disclaimer",
        "description": "Auto-generated captions disclaimer",
        "pattern": r'(?:auto-generated|automatically generated|machine-generated)',
        "severity": "low",
    },
    {
        "name": "description_boilerplate",
        "description": "YouTube description boilerplate in transcript",
        "pattern": r'(?:follow (?:me|us) on|(?:instagram|twitter|facebook|tiktok)\.com/|my website|check out my|patreon\.com|ko-fi\.com)',
        "severity": "low",
    },
]

# Compile patterns
for detector in DEFECT_DETECTORS:
    detector["_compiled"] = re.compile(detector["pattern"], re.IGNORECASE | re.UNICODE | re.MULTILINE)


@dataclass
class DefectFinding:
    """A single defect found in a transcript."""
    rule_name: str
    severity: str
    match_count: int
    sample_matches: list[str]  # First 3 matches as examples


@dataclass
class ItemReview:
    """Review result for a single item."""
    item_id: str
    title: str
    text_length: int
    findings: list[DefectFinding] = field(default_factory=list)
    is_empty: bool = False

    @property
    def has_defects(self) -> bool:
        return bool(self.findings) or self.is_empty

    @property
    def total_issues(self) -> int:
        return sum(f.match_count for f in self.findings)


def review_item(item_id: str, title: str, text: str) -> ItemReview:
    """Review a single item's transcript for defects."""
    review = ItemReview(
        item_id=item_id,
        title=title or "",
        text_length=len(text) if text else 0,
    )

    if not text or len(text.strip()) < 50:
        review.is_empty = True
        return review

    for detector in DEFECT_DETECTORS:
        pattern: re.Pattern = detector["_compiled"]
        matches = pattern.findall(text)
        if matches:
            # Deduplicate and take first 3 as samples
            unique_matches = list(dict.fromkeys(str(m)[:80] for m in matches))
            review.findings.append(DefectFinding(
                rule_name=detector["name"],
                severity=detector["severity"],
                match_count=len(matches),
                sample_matches=unique_matches[:3],
            ))

    return review


def review_batch(db_path: Path, batch_name: str, batches_file: Path) -> list[ItemReview]:
    """Review a batch of items."""
    with open(batches_file) as f:
        data = json.load(f)

    if batch_name == "all":
        item_ids = data["all_item_ids"]
    else:
        if batch_name not in data["batches"]:
            console.print(f"[red]Batch {batch_name} not found[/red]")
            return []
        item_ids = [item["id"] for item in data["batches"][batch_name]]

    conn = sqlite3.connect(str(db_path))
    reviews: list[ItemReview] = []

    for item_id in item_ids:
        row = conn.execute(
            "SELECT id, title, extracted_text FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if row:
            review = review_item(row[0], row[1], row[2])
            reviews.append(review)

    conn.close()
    return reviews


def review_from_db(db_path: Path, batch_name: str) -> list[ItemReview]:
    """Review items directly from DB with batch-style sampling."""
    conn = sqlite3.connect(str(db_path))

    if batch_name == "all":
        rows = conn.execute(
            "SELECT id, title, extracted_text FROM items WHERE source='youtube' AND extracted_text IS NOT NULL",
        ).fetchall()
    else:
        # Parse batch number and offset
        batch_num = int(batch_name.replace("batch-", ""))
        offset = (batch_num - 1) * 10
        rows = conn.execute(
            "SELECT id, title, extracted_text FROM items WHERE source='youtube' AND extracted_text IS NOT NULL ORDER BY id LIMIT 10 OFFSET ?",
            (offset,),
        ).fetchall()

    reviews = [review_item(r[0], r[1], r[2]) for r in rows]
    conn.close()
    return reviews


def save_results(reviews: list[ItemReview], output_path: Path) -> None:
    """Save review results to JSON."""
    results = []
    for review in reviews:
        result = {
            "item_id": review.item_id,
            "title": review.title,
            "text_length": review.text_length,
            "has_defects": review.has_defects,
            "is_empty": review.is_empty,
            "total_issues": review.total_issues,
            "findings": [
                {
                    "rule": f.rule_name,
                    "severity": f.severity,
                    "count": f.match_count,
                    "samples": f.sample_matches,
                }
                for f in review.findings
            ],
        }
        results.append(result)

    with open(output_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def display_summary(reviews: list[ItemReview], batch_name: str) -> None:
    """Display a summary of the review."""
    defective = [r for r in reviews if r.has_defects]

    table = Table(
        title=f"🔍 Transcript Review — {batch_name} ({len(reviews)} items, {len(defective)} with defects)",
        box=box.ROUNDED,
    )
    table.add_column("Item ID", style="dim", max_width=10)
    table.add_column("Title", max_width=50)
    table.add_column("Issues", justify="right")
    table.add_column("Defects Found")

    for r in defective:
        defect_summary = ", ".join(
            f"{f.rule_name}({f.match_count})" for f in r.findings
        )
        table.add_row(
            r.item_id[:8] + "...",
            r.title[:47] + "..." if len(r.title) > 50 else r.title,
            str(r.total_issues),
            defect_summary[:60],
        )

    console.print(table)

    # Aggregate defect stats
    defect_counts: dict[str, int] = {}
    for r in reviews:
        for f in r.findings:
            defect_counts[f.rule_name] = defect_counts.get(f.rule_name, 0) + f.match_count

    if defect_counts:
        console.print("\n[bold]Defect Type Summary:[/bold]")
        for name, count in sorted(defect_counts.items(), key=lambda x: -x[1]):
            console.print(f"  {name}: {count} occurrences")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review YouTube transcripts for defects")
    parser.add_argument("--db", type=Path, required=True, help="Path to unified.db")
    parser.add_argument("--batch", type=str, required=True, help="Batch name (e.g., batch-01) or 'all'")
    parser.add_argument("--batches-file", type=Path, default=Path("scripts/transcript_review_batches.json"))
    parser.add_argument("--output", type=Path, help="Output JSON file for results")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress display output")

    args = parser.parse_args()

    if args.batches_file.exists():
        reviews = review_batch(args.db, args.batch, args.batches_file)
    else:
        reviews = review_from_db(args.db, args.batch)

    if not args.quiet:
        display_summary(reviews, args.batch)

    output_path = args.output or Path(f"scripts/review_results_{args.batch}.json")
    save_results(reviews, output_path)
    console.print(f"\n[green]Results saved to {output_path}[/green]")


if __name__ == "__main__":
    main()
