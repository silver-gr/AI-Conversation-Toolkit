#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = [
#   "rich",
#   "httpx",
# ]
# requires-python = ">=3.11"
# ///

"""
Profile Synthesizer
===================

Generates the narrative ``PROFILE.md`` (biographical profile) and
``PATTERNS.md`` (cross-reference behavioral patterns) from the aggregated
``extractions.json`` produced by ``biography_extractor_v2.py``.

This is the LLM-synthesis sibling of ``profile_generator.py``:

    profile_generator.py   -> deterministic markdown tables (counts, no LLM)
    profile_synthesizer.py -> narrative prose profile + patterns (2 LLM calls)

It does NOT re-read the raw conversations. It aggregates the 2,000+ structured
extractions into a compact digest (top items per field with mention-counts,
date ranges, and sources, plus a monthly topic timeline) and sends that digest
to an OpenAI-compatible endpoint in two calls -- one for the profile, one for
the patterns. The deterministic header (date, model, source counts, date range)
is written by this script, not the model, so provenance cannot be hallucinated.

Backend: same OpenAI-compatible client as biography_extractor_v2.py. Defaults to
the local agy/Gemini proxy; override with --model / --base-url / --api-key or the
BIO_BASE_URL / BIO_API_KEY / BIO_MODEL environment variables. Synthesis is the
high-value creative step -- consider pointing it at a stronger model than the
extractor used (the original Mar-2026 profile was synthesized with Opus).

Usage:
    # Both files, default local proxy
    uv run scripts/profile_synthesizer.py

    # Only the profile, with a stronger model
    uv run scripts/profile_synthesizer.py --profile-only \\
        --base-url https://api.anthropic-compatible.example/v1 \\
        --api-key sk-xxx --model some-strong-model

    # Preview the digest + prompt sizes without calling the API
    uv run scripts/profile_synthesizer.py --dry-run

    # Custom input / output
    uv run scripts/profile_synthesizer.py \\
        --input output/profile/data/extractions.json \\
        --output-dir output/profile
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# --- Defaults (mirror biography_extractor_v2.py) --------------------------------
DEFAULT_INPUT = "output/profile/data/extractions.json"
DEFAULT_OUTPUT_DIR = "output/profile"

DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
DEFAULT_API_KEY = "sk-rT24hZlNDy2cbLvF6oPQcklYeESDyouT"
DEFAULT_MODEL = "gemini-3-flash-preview"

TEMPERATURE = 0.4          # a touch higher than extraction (0.2) for narrative flow
MAX_ITEMS_PER_FIELD = 30   # cap digest breadth; top-N by mention count
MAX_TIMELINE_PER_MONTH = 8
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5.0
LLM_TIMEOUT = 600.0        # synthesis outputs are long

# Category -> fields, matching biography_extractor_v2.EXTRACTION_SCHEMA.
CATEGORIES: dict[str, list[str]] = {
    "identity": ["name_mentions", "location_mentions", "language_indicators", "demographic_hints"],
    "work": ["occupation_hints", "projects_mentioned", "skills_demonstrated", "business_ventures", "work_style"],
    "health": ["physical_health", "mental_health", "sleep_patterns", "nutrition_diet", "substances", "medications"],
    "relationships": ["romantic", "family", "social", "sexuality"],
    "interests": ["hobbies", "intellectual", "entertainment", "creative"],
    "goals": ["short_term", "long_term", "dreams", "fears"],
    "challenges": ["current_problems", "recurring_patterns", "blockers", "seeking_help_for"],
    "daily_life": ["routines", "tools_apps", "living_situation", "finances"],
    "values": ["explicit_values", "implicit_values", "philosophical", "political_social"],
}


# --- LLM helpers ----------------------------------------------------------------
def _strip_markdown_fences(text: str) -> str:
    """Drop a leading ```lang fence and trailing ``` if the model wrapped output."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    return text


def call_llm(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float = TEMPERATURE,
) -> tuple[str, bool]:
    """Call an OpenAI-compatible /chat/completions endpoint (sync, with retry).

    Returns (content, was_truncated). Raises on connection failure so the caller
    can print an actionable message (e.g. "start your local proxy").
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": temperature}

    last_err: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with httpx.Client(timeout=LLM_TIMEOUT) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code in (429, 500, 502, 503):
                wait = RETRY_DELAY * (2 ** attempt)
                console.print(
                    f"[yellow]Server {resp.status_code}, waiting {wait:.0f}s "
                    f"(attempt {attempt + 1}/{RETRY_ATTEMPTS})...[/yellow]"
                )
                _sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            truncated = choice.get("finish_reason") == "length"
            return _strip_markdown_fences(content), truncated
        except (httpx.HTTPStatusError, httpx.ReadTimeout, KeyError) as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS - 1:
                wait = RETRY_DELAY * (2 ** attempt)
                console.print(f"[yellow]Attempt {attempt + 1} failed ({e}), retrying in {wait:.0f}s...[/yellow]")
                _sleep(wait)
            else:
                raise
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"Could not connect to {base_url}. Is your LLM endpoint running? "
                f"Pass --base-url/--model or set BIO_BASE_URL/BIO_MODEL."
            ) from e
    raise RuntimeError(f"All retry attempts exhausted: {last_err}")


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


# --- Aggregation ----------------------------------------------------------------
def aggregate(extractions: list[dict]) -> tuple[dict, dict, dict]:
    """Collapse per-conversation extractions into item-level aggregates.

    Returns (aggregated, timeline, stats):
      aggregated[category][field][item] = {"count", "dates": set, "sources": set}
      timeline[YYYY-MM] = list of {"topic", "richness", "source"}
      stats = {total, by_source, by_richness, date_range, name_guess}
    """
    aggregated: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        cat: {field: {} for field in fields} for cat, fields in CATEGORIES.items()
    }
    timeline: dict[str, list[dict]] = defaultdict(list)
    by_source: dict[str, int] = defaultdict(int)
    by_richness: dict[str, int] = defaultdict(int)
    dates_seen: list[str] = []

    for ext in extractions:
        meta = ext.get("_metadata", {})
        ctx = ext.get("context", {})
        source = meta.get("source", "unknown")
        date = meta.get("conversation_date", "unknown")
        richness = (ctx.get("biographical_richness") or "minimal").lower()

        by_source[source] += 1
        by_richness[richness] += 1
        if date != "unknown":
            dates_seen.append(date)

        topic = (ctx.get("main_topic") or "").strip()
        if topic and richness in ("high", "medium"):
            month = date[:7] if len(date) >= 7 else date
            timeline[month].append({"topic": topic, "richness": richness, "source": source})

        for category, fields in CATEGORIES.items():
            cat_data = ext.get(category, {})
            if not isinstance(cat_data, dict):
                continue
            for field in fields:
                values = cat_data.get(field, [])
                if not isinstance(values, list):
                    continue
                for value in values:
                    norm = str(value).strip()
                    if not norm:
                        continue
                    bucket = aggregated[category][field].setdefault(
                        norm, {"count": 0, "dates": set(), "sources": set()}
                    )
                    bucket["count"] += 1
                    if date != "unknown":
                        bucket["dates"].add(date)
                    bucket["sources"].add(source)

    dates_seen.sort()
    stats = {
        "total": len(extractions),
        "by_source": dict(by_source),
        "by_richness": dict(by_richness),
        "date_range": {
            "earliest": dates_seen[0] if dates_seen else "unknown",
            "latest": dates_seen[-1] if dates_seen else "unknown",
        },
        "name_guess": _guess_name(aggregated),
    }
    return aggregated, timeline, stats


def _guess_name(aggregated: dict) -> str:
    """Best-effort subject name from the most-mentioned identity.name_mentions item."""
    names = aggregated.get("identity", {}).get("name_mentions", {})
    if not names:
        return "User"
    top = max(names.items(), key=lambda kv: kv[1]["count"])
    return top[0]


# --- Digest builder -------------------------------------------------------------
def build_digest(aggregated: dict, timeline: dict, stats: dict, max_items: int) -> str:
    """Render the aggregates into a compact, LLM-friendly text digest."""
    lines: list[str] = []
    dr = stats["date_range"]
    lines.append("=== DATASET OVERVIEW ===")
    lines.append(f"Total conversations analyzed: {stats['total']}")
    lines.append(f"Date range: {dr['earliest']} to {dr['latest']}")
    lines.append("By source: " + ", ".join(f"{s}={n}" for s, n in sorted(stats["by_source"].items())))
    lines.append("By richness: " + ", ".join(f"{r}={n}" for r, n in sorted(stats["by_richness"].items())))
    lines.append("")
    lines.append("Note: each item below shows (mentions=N; first-last date; sources). "
                 "'mentions' = number of distinct conversations the item appeared in -- "
                 "use it to gauge how well-established a fact is.")
    lines.append("")

    lines.append("=== AGGREGATED FACTS BY CATEGORY ===")
    for category, fields in CATEGORIES.items():
        lines.append(f"\n## {category.upper()}")
        for field in fields:
            items = aggregated[category][field]
            if not items:
                continue
            ranked = sorted(items.items(), key=lambda kv: kv[1]["count"], reverse=True)[:max_items]
            lines.append(f"\n### {field}")
            for item, data in ranked:
                dates = sorted(data["dates"])
                if dates:
                    span = dates[0] if dates[0] == dates[-1] else f"{dates[0]}..{dates[-1]}"
                else:
                    span = "n/a"
                srcs = ",".join(sorted(data["sources"]))
                lines.append(f"- {item} (mentions={data['count']}; {span}; {srcs})")

    lines.append("\n=== TOPIC TIMELINE (high/medium-richness conversations by month) ===")
    for month in sorted(timeline.keys()):
        entries = timeline[month]
        # De-dupe topics within the month, keep order, cap.
        seen: set[str] = set()
        topics: list[str] = []
        for e in entries:
            t = e["topic"]
            if t not in seen:
                seen.add(t)
                topics.append(t)
            if len(topics) >= MAX_TIMELINE_PER_MONTH:
                break
        lines.append(f"- {month} ({len(entries)} convos): " + "; ".join(topics))

    return "\n".join(lines)


# --- Prompts --------------------------------------------------------------------
PROFILE_SYSTEM = (
    "You are a careful biographical analyst. You are given an aggregated digest of "
    "facts extracted from a person's AI-chat history. Write a comprehensive, honest, "
    "non-judgmental biographical profile of that person (the USER). Ground every claim "
    "in the digest -- do not invent facts. Output GitHub-flavored markdown only, no code "
    "fences, no preamble."
)

PATTERNS_SYSTEM = (
    "You are a behavioral analyst. You are given an aggregated digest of facts extracted "
    "from a person's AI-chat history. Identify the cross-cutting behavioral and thematic "
    "patterns that connect multiple life domains. Ground every pattern in the digest -- do "
    "not invent evidence. Output GitHub-flavored markdown only, no code fences, no preamble."
)


def build_profile_user(digest: str) -> str:
    return f"""Below is the aggregated digest.

{digest}

=== TASK ===
Write a biographical profile in markdown with these sections (omit a section only if the
digest has no relevant data, and note that in "Data Gaps"):

## Identity
## Work & Projects
## Health
### Mental Health
### Supplements & Nutrition
### Substance Use
### Physical Health
## Relationships
## Interests
## Goals
## Challenges
## Values & Psychology
## Data Gaps
(a table: | Category | Coverage | with Complete / Partial / Sparse / Empty and a short note)

RULES:
- Mark each factual line with [E] (explicitly stated / well-supported) or [I] (inferred).
- Where a fact is notably well-supported, append the mention count, e.g. "(40 mentions)".
- Prefer specificity ("takes magnesium glycinate nightly") over vagueness ("takes supplements").
- Use bullet points within sections. Be thorough but do not pad.
- Do NOT include a top-level H1 title or a "Generated:" line -- those are added separately.
- Do NOT fabricate. If the digest is thin on something, say so in Data Gaps rather than guessing."""


def build_patterns_user(digest: str) -> str:
    return f"""Below is the aggregated digest.

{digest}

=== TASK ===
Identify 6-9 cross-reference patterns -- recurring themes that connect two or more life
domains (e.g. Health + Work + Challenges). For EACH pattern output:

## Pattern N: <Title> [<Strength>]
**Categories:** <comma-separated domains>

<1-2 sentence description>

**Evidence:**
- <bullet grounded in specific digest items>
- ...

**Insight:** <one crisp interpretive sentence>

Strength rating rule (use the mention counts in the digest as the signal):
- **Strong** = supported by ~20+ mentions across the contributing items
- **Moderate** = ~10-19
- **Emerging** = ~5-9

After the patterns, add:

## Behavioral Evolution Over Time
A markdown table: | Period | Dominant Pattern | derived from the TOPIC TIMELINE section,
grouping adjacent months into coherent phases.

RULES:
- Ground every evidence bullet in the digest; do not invent specifics.
- Do NOT include a top-level H1 title or a "Generated:" line -- those are added separately.
- Order patterns strongest first."""


# --- Deterministic headers (script-owned, never LLM-generated) ------------------
def _source_summary(stats: dict) -> str:
    parts = [f"{n} {s.title()}" for s, n in sorted(stats["by_source"].items(), key=lambda kv: -kv[1])]
    return ", ".join(parts)


def profile_header(stats: dict, model: str) -> str:
    dr = stats["date_range"]
    today = datetime.now().strftime("%Y-%m-%d")
    return (
        f"# Biographical Profile: {stats['name_guess']}\n\n"
        f"> **Generated:** {today} | **Model:** {model} | "
        f"**Source:** {stats['total']} conversations ({_source_summary(stats)}) "
        f"spanning {dr['earliest']} to {dr['latest']}\n\n"
        f"---\n\n"
    )


def profile_footer() -> str:
    return "\n\n---\n\n*Key: [E] = explicit / well-supported, [I] = inferred. " \
           "Mention counts in parentheses where notable.*\n"


def patterns_header(stats: dict, model: str) -> str:
    dr = stats["date_range"]
    today = datetime.now().strftime("%Y-%m-%d")
    return (
        f"# Cross-Reference Patterns: {stats['name_guess']}\n\n"
        f"> **Generated:** {today} | **Model:** {model} | "
        f"**Source:** {stats['total']} conversations ({_source_summary(stats)}), "
        f"{dr['earliest']} to {dr['latest']}\n>\n"
        f"> Strength ratings: **Strong** = 20+ mentions | **Moderate** = 10-19 | "
        f"**Emerging** = 5-9\n\n"
        f"---\n\n"
    )


def patterns_footer() -> str:
    return "\n\n---\n\n*Patterns derived by correlating aggregated facts. " \
           "Strength reflects mention counts, not certainty.*\n"


# --- Orchestration --------------------------------------------------------------
def load_extractions(input_file: Path) -> list[dict]:
    if not input_file.exists():
        console.print(f"[red]Input file not found: {input_file}[/red]")
        console.print("[yellow]Run biography_extractor_v2.py first to produce extractions.json[/yellow]")
        sys.exit(1)
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        console.print(f"[red]Expected a JSON array of extractions, got {type(data).__name__}[/red]")
        sys.exit(1)
    return data


def synthesize(kind: str, digest: str, stats: dict, args) -> str | None:
    """Run one synthesis call. kind is 'profile' or 'patterns'. Returns full markdown."""
    if kind == "profile":
        system, user = PROFILE_SYSTEM, build_profile_user(digest)
        header, footer = profile_header(stats, args.model), profile_footer()
    else:
        system, user = PATTERNS_SYSTEM, build_patterns_user(digest)
        header, footer = patterns_header(stats, args.model), patterns_footer()

    console.print(f"[cyan]Synthesizing {kind} via {args.model}...[/cyan]")
    try:
        body, truncated = call_llm(
            args.base_url, args.api_key, args.model,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
    except (ConnectionError, RuntimeError) as e:
        console.print(f"[red]{kind} synthesis failed: {e}[/red]")
        return None
    if truncated:
        console.print(f"[yellow]Warning: {kind} output was truncated by the model's length limit.[/yellow]")
    return header + body.strip() + footer


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthesize narrative PROFILE.md and PATTERNS.md from extractions.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (override CLI defaults, shared with biography_extractor_v2.py):
  BIO_BASE_URL   API base URL   (default: http://127.0.0.1:8317/v1)
  BIO_API_KEY    API key
  BIO_MODEL      Model name     (default: gemini-3-flash-preview)
""",
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"extractions.json (default: {DEFAULT_INPUT})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"output dir (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--profile-only", action="store_true", help="Generate only PROFILE.md")
    parser.add_argument("--patterns-only", action="store_true", help="Generate only PATTERNS.md")
    parser.add_argument("--dry-run", action="store_true", help="Print digest + prompt sizes, make no API call")
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS_PER_FIELD,
                        help=f"Max items per field in the digest (default: {MAX_ITEMS_PER_FIELD})")
    parser.add_argument("--base-url", default=os.environ.get("BIO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("BIO_API_KEY", DEFAULT_API_KEY))
    parser.add_argument("--model", default=os.environ.get("BIO_MODEL", DEFAULT_MODEL))
    args = parser.parse_args()

    console.print(Panel.fit(
        "Profile Synthesizer\nAggregating extractions into a digest, then synthesizing narrative profile + patterns",
        title="profile_synthesizer",
    ))

    extractions = load_extractions(Path(args.input))
    console.print(f"[green]Loaded {len(extractions)} extractions[/green]")

    aggregated, timeline, stats = aggregate(extractions)
    digest = build_digest(aggregated, timeline, stats, args.max_items)
    console.print(
        f"[blue]Digest: {len(digest):,} chars | subject guess: {stats['name_guess']} | "
        f"endpoint: {args.base_url} | model: {args.model}[/blue]"
    )

    if args.dry_run:
        console.print("\n[bold]--- DIGEST PREVIEW (first 3000 chars) ---[/bold]")
        console.print(digest[:3000])
        console.print(
            f"\n[bold]Prompt sizes:[/bold] profile user ~{len(build_profile_user(digest)):,} chars, "
            f"patterns user ~{len(build_patterns_user(digest)):,} chars"
        )
        console.print("[yellow]Dry run -- no API calls made, no files written.[/yellow]")
        return

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    if not args.patterns_only:
        md = synthesize("profile", digest, stats, args)
        if md:
            (out_dir / "PROFILE.md").write_text(md, encoding="utf-8")
            written.append("PROFILE.md")

    if not args.profile_only:
        md = synthesize("patterns", digest, stats, args)
        if md:
            (out_dir / "PATTERNS.md").write_text(md, encoding="utf-8")
            written.append("PATTERNS.md")

    table = Table(title="Synthesis Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Input", str(args.input))
    table.add_row("Model", args.model)
    table.add_row("Conversations", str(stats["total"]))
    table.add_row("Date range", f"{stats['date_range']['earliest']} to {stats['date_range']['latest']}")
    table.add_row("Files written", ", ".join(written) if written else "(none)")
    console.print(table)

    if not written:
        console.print("[red]No files were written -- see errors above.[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
