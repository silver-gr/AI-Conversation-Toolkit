# CLAUDE.md
This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
AI Conversation Export Toolkit - extracts, analyzes, and indexes conversations from ChatGPT, Claude, and Google Gemini into searchable markdown files with YAML frontmatter and optional Obsidian vault generation.

## Commands
```bash
# =============================================================================
# UNIFIED IMPORT CLI (RECOMMENDED)
# =============================================================================
# Main entry point for all imports - orchestrates extraction, logging, and post-processing

# Multi-account import (recommended — reads accounts.json)
python3 scripts/run_import.py --config accounts.json --incremental

# Full import from all sources (uses accounts.json if present, else default paths)
python3 scripts/run_import.py --all

# Incremental import (only new conversations, skip existing)
python3 scripts/run_import.py --all --incremental

# Import specific sources only (single-account, legacy mode)
python3 scripts/run_import.py --chatgpt ChatGPT/conversations.json
python3 scripts/run_import.py --claude Claude/conversations.json
python3 scripts/run_import.py --gemini "Google/path/to/MyActivity.json"

# Full import with post-processing
python3 scripts/run_import.py --all --research-index --memories

# Preview what would happen (no changes made)
python3 scripts/run_import.py --all --dry-run

# Add notes to import session
python3 scripts/run_import.py --all --notes "Weekly import"

# =============================================================================
# POST-PROCESSING
# =============================================================================

# Build unified research index (scans output/*/conversations/)
python3 scripts/research_index.py

# Extract Claude memories to markdown
python3 scripts/memories_to_md.py

# Migrate old inline metadata to YAML frontmatter (idempotent)
python3 scripts/migrate_to_yaml.py

# Identify and rename Gemini files with non-descriptive titles
python3 scripts/gemini_meta_indexer.py              # Dry-run: preview changes
python3 scripts/gemini_meta_indexer.py --rename      # Apply renames
python3 scripts/gemini_meta_indexer.py --rename --json  # Also save JSON manifest

# Quality filter: score conversations and move low-value ones to output/_excluded/
python3 scripts/quality_filter.py --dry-run          # Preview scoring (no moves)
python3 scripts/quality_filter.py                    # Apply filter (moves excluded files)
python3 scripts/quality_filter.py --verbose          # Show per-file scoring details
python3 scripts/quality_filter.py --threshold 50     # Custom score threshold (default: 40)

# Biography extraction: extract biographical facts from conversations using AI
uv run scripts/biography_extractor.py --all-sources            # All sources (Claude backend)
uv run scripts/biography_extractor.py --all-sources --sample 20 # Test with 20 files
uv run scripts/biography_extractor.py --all-sources --export output/profile/data/extractions.json

# Biography extraction v2: multi-model backend support
uv run scripts/biography_extractor_v2.py --all-sources --provider claude
uv run scripts/biography_extractor_v2.py --all-sources --provider gemini --model gemini-2.0-flash
uv run scripts/biography_extractor_v2.py --all-sources --provider codex

# Profile generator: aggregate biography extractions into organized markdown
python3 scripts/profile_generator.py                          # From default extractions.json
python3 scripts/profile_generator.py --input output/profile/data/extractions.json
python3 scripts/profile_generator.py --output-dir output/profile

# =============================================================================
# VAULT BUILDING
# =============================================================================

# Build Obsidian vault for any date range (flexible successor to build_december_vault.py)
python3 scripts/build_vault.py --all                          # All conversations
python3 scripts/build_vault.py --month 2026-01                # Specific month
python3 scripts/build_vault.py --from 2025-12-01 --to 2025-12-31
python3 scripts/build_vault.py --from 2026-01-01              # From date to now
python3 scripts/build_vault.py --name "my-vault"              # Custom vault name

# =============================================================================
# INDIVIDUAL EXTRACTORS (for manual/fine-grained control)
# =============================================================================

# Extract ChatGPT or Claude conversations (auto-detects format)
python3 scripts/simple_extractor.py ChatGPT/conversations.json -o output/chatgpt-full
python3 scripts/simple_extractor.py Claude/conversations.json -o output/claude-full

# Extract Gemini activity (groups into sessions by 30-min gaps)
python3 scripts/gemini_extractor.py "Google/Η δραστηριότητά μου/Εφαρμογές Gemini/MyActivity.json" -o output/gemini
python3 scripts/gemini_extractor.py path/to/MyActivity.json -o output/gemini --no-grouping

# AI-powered analysis with caching (requires Claude CLI)
uv run scripts/conversation_summarizer.py ChatGPT/conversations.json --output-dir output/chatgpt-full --max 50
uv run scripts/conversation_summarizer.py path/to/conversations.json --no-cache
uv run scripts/conversation_summarizer.py path/to/conversations.json --clean-cache 7

# Build December 2025 vault (legacy, hardcoded date range)
python3 scripts/build_december_vault.py

# =============================================================================
# IMPORT LOG MANAGEMENT
# =============================================================================

python3 scripts/import_logger.py status                          # Show current status
python3 scripts/import_logger.py start --notes "Initial import"  # Start new import session
python3 scripts/import_logger.py complete <import_id>            # Mark import complete
python3 scripts/import_logger.py regenerate                      # Rebuild markdown report
```

## Standard Import Workflow
```bash
# 1. Place fresh exports in their directories:
#    Per-account directories as defined in accounts.json
#    (e.g., ChatGPT/conversations.json, Claude-Pro/conversations.json, etc.)

# 2. Run incremental import (skips previously imported conversations)
#    Multi-account (recommended):
python3 scripts/run_import.py --config accounts.json --incremental
#    Single-account (legacy):
python3 scripts/run_import.py --all --incremental

# 3. Run post-processing
python3 scripts/research_index.py
python3 scripts/memories_to_md.py

# 4. Rename non-descriptive Gemini titles
python3 scripts/gemini_meta_indexer.py --rename

# 5. Migrate any old-format files to YAML frontmatter (idempotent, safe to re-run)
python3 scripts/migrate_to_yaml.py
```

## Architecture

**Data flow**: Raw exports (`ChatGPT/`, `Claude/`, `Google/`) → Parser scripts → YAML-frontmatter markdown (`output/`) → Optional vault generation

### Core Modules

**`parser.py`** - Shared parsing library:
- `Message` and `Conversation` dataclasses normalize all formats
- `parse_timestamp()` handles Unix epoch (ChatGPT) and ISO 8601 (Claude/Gemini)
- Streaming support via `ijson` for large files

**`simple_extractor.py`** - ChatGPT/Claude extraction:
- Auto-detects format from JSON structure (`mapping` dict = ChatGPT, `chat_messages` = Claude)
- ChatGPT tree traversal via parent-child `mapping` dict
- Claude linear `chat_messages` array with `sender` field ("human"/"assistant")
- Generates YAML frontmatter with type, title, date, source, messages, characters, topics

**`gemini_extractor.py`** - Google Gemini extraction:
- Parses flat activity logs (not threaded conversations)
- Queries in `title`, responses in `safeHtmlItem[].html`, Canvas artifacts in `subtitles[].name`
- HTML→Markdown conversion with `strip_html()`
- Groups activities into sessions based on 30-minute time gaps (configurable via `--no-grouping`)

**`research_index.py`** - Research detection and indexing:
- Uses **structural markers only** (no semantic/keyword matching) to detect actual Deep Research tool usage
- ChatGPT: `model: research` YAML field, PUA Unicode + source headers, source list patterns, long response (>30K chars)
- Claude: `web_search` tool name, `research task` phrase (Claude Research feature marker)
- Gemini: "i've completed your research" completion phrase, "έναρξη έρευνας" (Greek)
- Exclusion patterns filter out cancelled tasks, reminders, meta-questions (skipped for large files)
- Each detected file tagged with specific `detection_method` for transparency
- Output uses Obsidian wikilinks for easy vault navigation

**`run_import.py`** - Unified import orchestrator (main entry point):
- Single CLI to process all sources with consistent workflow
- Multi-account support via `accounts.json` config file (`--config`)
- `--all` auto-detects `accounts.json` if present, else falls back to `DEFAULT_PATHS`
- Integrates ImportLogger for session tracking and deduplication
- Post-processing: research index, memories extraction, biography extraction
- Dry-run mode for previewing operations
- Rich terminal output (with fallback to plain text)

**`accounts.json`** - Multi-account configuration (gitignored, see `accounts.example.json`):
- Defines multiple accounts per platform (e.g., 3 Claude accounts, 2 ChatGPT accounts)
- Each entry: `{"name": "personal", "source": "chatgpt", "export_path": "ChatGPT/conversations.json"}`
- Paths can be relative (to project root) or absolute
- Used by `run_import.py --config accounts.json`

**`import_logger.py`** - Import tracking system:
- `ImportLogger` class tracks import sessions across sources (ChatGPT, Claude, Gemini)
- Supports account-qualified source keys (e.g., `chatgpt:personal`, `claude:pro`)
- `get_imported_ids()` pools IDs across all accounts of the same platform for deduplication
- JSON log (`imports/import_log.json`) stores per-import stats and conversation IDs
- Auto-generates timestamped markdown reports (`imports/IMPORT_LOG_*.md`)
- `get_imported_ids(source)` returns all previously imported IDs for deduplication
- CLI interface: `python3 scripts/import_logger.py status`

**`migrate_to_yaml.py`** - YAML frontmatter migration:
- Converts old inline `## Metadata` sections to YAML frontmatter
- Idempotent: skips files that already have `---` frontmatter
- Extracts topics from conversation content
- Scans `output/{chatgpt-full,claude-full,gemini}/conversations/`

**`gemini_meta_indexer.py`** - Gemini title renaming:
- Identifies files with non-descriptive titles (e.g., "start-research", "translate")
- Extracts descriptive topics from first user message using keyword extraction
- Renames files and updates YAML frontmatter title + H1 heading
- Handles filename collisions with `_N` suffixes
- Dry-run by default; use `--rename` to apply

**`conversation_summarizer.py`** - AI-powered analysis:
- Uses `uv run` with inline script dependencies (rich, pandas, openpyxl)
- SQLite cache with SHA256 content hashing for idempotency
- Requires Claude CLI for LLM analysis

**`quality_filter.py`** - UltraRAG curation filter:
- Scores each conversation (base 60) using character count, message count, title patterns, and content signals
- Force-keeps research conversations (any score) and very long ones (>40k chars, 3+ messages)
- Hard-excludes near-empty files (<300 chars) and single-message stubs
- Moves excluded files to `output/_excluded/{source}/` (reversible)
- Outputs `output/QUALITY_FILTER_REPORT.md` with scoring breakdown
- Default threshold: score ≥ 40; research and long content always kept regardless

**`biography_extractor.py`** / **`biography_extractor_v2.py`** - Biographical fact extraction:
- Uses AI (Claude CLI) to extract structured biographical facts from conversation content
- SQLite cache keyed by SHA256 to avoid re-processing unchanged files
- v2 adds multi-model support: `--provider claude|gemini|codex`
- Exports structured JSON to `output/profile/data/extractions.json`

**`profile_generator.py`** - Profile aggregation:
- Reads `output/profile/data/extractions.json` from biography extractor
- Aggregates facts by life domain category and generates organized markdown files
- Outputs to `output/profile/` directory

**`build_vault.py`** - Flexible Obsidian vault builder:
- Successor to `build_december_vault.py`; accepts `--all`, `--month YYYY-MM`, `--from`/`--to` date range
- Auto-classifies conversations into topic categories (AI & LLMs, Health, Product Dev, etc.)
- Generates daily notes, topic MOCs, and a dashboard

### Export Format Differences

| Source | Structure | Timestamps | Messages |
|--------|-----------|------------|----------|
| ChatGPT | `mapping` dict with tree structure | Unix seconds | Parent-child traversal |
| Claude | `chat_messages` array | ISO 8601 | Linear array, `sender` field |
| Gemini | Flat activity log | ISO 8601 | `title` (query) + `safeHtmlItem` (response) |

## Output Format

All conversation files use YAML frontmatter:

```yaml
---
type: conversation
title: "Conversation Title"
date: 2025-07-02T14:30:00
source: chatgpt    # chatgpt | claude | gemini
account: personal  # optional, from accounts.json (omitted if unnamed)
model: null
messages: 28
characters: 38198
has_code: true
topics:
  - keyword1
  - keyword2
research_type: null
---
# Conversation Title

## Conversation

### USER (14:30)
...

### ASSISTANT (14:31)
...
```

## Output Structure

```
output/
├── chatgpt-full/
│   ├── INDEX.md
│   └── conversations/YYYYMMDD_title-slug.md
├── claude-full/
│   ├── INDEX.md
│   └── conversations/YYYYMMDD_title-slug.md
├── gemini/
│   ├── INDEX.md
│   └── conversations/YYYYMMDD_title-slug.md
├── memories/
│   └── *.md
├── profile/
│   ├── data/extractions.json      # Biography extractor output
│   └── *.md                       # Profile category files
├── _excluded/                     # Quality-filtered conversations (moved, not deleted)
│   ├── chatgpt-full/
│   ├── claude-full/
│   └── gemini/
├── RESEARCH_INDEX.md
├── QUALITY_FILTER_REPORT.md       # Quality filter scoring report
├── gemini_files_to_reindex.json   # Gemini meta-indexer manifest
└── december-2025-vault/           # Optional Obsidian vault (legacy)
    ├── Home.md
    ├── Daily/YYYY-MM-DD.md
    ├── Topics/*.md
    └── Conversations/{Claude,ChatGPT,Gemini}/*.md

imports/
├── import_log.json       # JSON log of all imports (schema below)
└── IMPORT_LOG_*.md       # Timestamped markdown reports
```

## Key Patterns

- All scripts use `slugify()` for safe filenames: lowercase, no special chars, max 50 chars
- Output files named `YYYYMMDD_title-slug.md` for chronological sorting
- YAML frontmatter with structured metadata (date, source, messages, characters, topics)
- Deduplication via conversation IDs stored in import log (`get_imported_ids()`)
- Cache databases (`*.db`) use SQLite WAL mode for concurrent access

## File Locations

- Raw exports: `ChatGPT/`, `Claude/`, `Google/` (gitignored, per-account dirs like `Claude-Pro/` also gitignored)
- Account config: `accounts.json` (gitignored, see `accounts.example.json` for template)
- Generated output: `output/` (gitignored)
- Import logs: `imports/` (tracked in git)
- Cache DBs: `*_cache.db` files (gitignored)
- Documentation: `docs/*.md`

## Import History

| Date | ChatGPT | Claude | Gemini | Notes |
|------|---------|--------|--------|-------|
| 2025-12-16 | 16 | 51 | 389 | Retroactive (pre-logger ghost run) |
| 2026-01-13 | 1,541 | 142 | — | First logged import |
| 2026-01-25 | — | — | 343 | First Gemini import |
| 2026-02-09 | 48 | 319 | 8 | Latest import |
| 2026-03-02 | 28 | 30 | — | Split-file export format (merged before import) |
| 2026-07-07 | 138 | 150 | 95 | Auto-merge built in; fixed `children`-less mapping (silent 0-import); Gemini activity-level dedup |

**Totals**: 1,771 ChatGPT + 692 Claude + 835 Gemini sessions

## Import Log Schema

```json
{
  "imports": [
    {
      "id": "import_20251216_000000_retroactive",
      "started_at": "2025-12-16T16:30:00+00:00",
      "completed_at": "2025-12-16T17:00:00+00:00",
      "status": "completed",
      "sources": {
        "chatgpt:personal": {
          "file": "conversations.json",
          "total_in_export": 1605,
          "new_imported": 16,
          "skipped_existing": 0,
          "conversation_ids": ["uuid1", "uuid2", "..."]
        },
        "claude:main": {
          "file": "conversations.json",
          "total_in_export": 512,
          "new_imported": 51,
          "skipped_existing": 0,
          "conversation_ids": ["uuid3", "uuid4", "..."]
        }
      },
      "post_processing": ["research_index"],
      "notes": "Retroactive entry for initial unlogged run"
    }
  ],
  "metadata": {
    "last_updated": "2026-02-09T...",
    "total_imports": 5,
    "schema_version": "1.1.0"
  }
}
```

## Known Issues & Notes

- **Gemini session grouping**: Activities within 30 minutes are grouped into one conversation. Use `--no-grouping` to disable.
- **YAML frontmatter required**: `research_index.py` and `migrate_to_yaml.py` both expect YAML frontmatter. Run migration if working with old-format files.
- **Gemini non-descriptive titles**: Many Gemini Deep Research sessions default to "start-research". Run `gemini_meta_indexer.py --rename` after imports.
- **Import ID tracking**: The import logger tracks conversation UUIDs (ChatGPT/Claude) and session timestamps (Gemini) for deduplication. All current exports have 100% ID coverage. Multi-account imports use composite source keys (`chatgpt:personal`) in the import log; `get_imported_ids()` pools across all accounts of the same platform.
- **Multi-account setup**: Copy `accounts.example.json` to `accounts.json` and edit paths. Conversations from all accounts merge into the same output directories (`chatgpt-full/`, `claude-full/`, `gemini/`). The `account:` YAML frontmatter field tracks provenance.
- **ChatGPT split exports (auto-merged)**: As of March 2026, ChatGPT exports conversations as multiple `conversations-000.json` ... `conversations-NNN.json` files (100 per file) instead of a single `conversations.json`. **`run_import.py` now merges these automatically** (`merge_chatgpt_splits()`): when split files are present it (re)builds `ChatGPT/conversations.json` before extraction, rewriting only when a split is newer than the merged file. Manual merge (if needed): `python3 -c "import json,glob; data=[c for f in sorted(glob.glob('ChatGPT/conversations-*.json')) for c in json.load(open(f))]; json.dump(data, open('ChatGPT/conversations.json','w'))"`.
- **ChatGPT `children`-less mapping (2026+)**: Newer ChatGPT exports drop the per-node `children` array in the `mapping` tree and keep only `parent`. `traverse_chatgpt_tree()` reconstructs child adjacency from `parent` pointers (works for older exports too). Symptom if this regresses: extraction reports "Found N conversations" but "Processed: 0" — a **silent zero-import**, no error.
- **Gemini dedup is activity-level**: Gemini has no per-conversation ID, so the extractor synthesizes one from a session-boundary timestamp. Session grouping (30-min gaps) can shift between exports, so `process_gemini_export()` now also records every activity's stable timestamp id and skips a session if its boundary id was seen **or all its activities were already imported**. Expect occasional single `_N` duplicate files on a boundary shift; the activity-level guard suppresses them on subsequent runs.
- **Quality filter is reversible**: Excluded files are moved to `output/_excluded/`, not deleted. Move them back to restore.
- **Biography extractors require `uv run`**: Both `biography_extractor.py` and `biography_extractor_v2.py` use inline script dependencies (`rich`) and must be run with `uv run`, not `python3`.
- **Profile generation is two-step**: Run `biography_extractor*.py` first to produce `extractions.json`, then `profile_generator.py` to build the markdown profile.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ai-conversation-toolkit** (1720 symbols, 3704 relationships, 147 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "master"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ai-conversation-toolkit/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ai-conversation-toolkit/clusters` | All functional areas |
| `gitnexus://repo/ai-conversation-toolkit/processes` | All execution flows |
| `gitnexus://repo/ai-conversation-toolkit/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
