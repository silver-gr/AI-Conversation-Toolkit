# Repository Guidelines

## Project Structure & Data Layout
- `scripts/` holds the Python tools: `conversation_summarizer.py` (uv-run CLI with SQLite caching), `simple_extractor.py` (fast markdown export without LLM calls), `gemini_extractor.py` (Google Gemini Apps Activity → markdown with session grouping), `memories_to_md.py` (Claude memories to markdown), `research_index.py` (aggregates research conversations), and `parser.py` (shared parsing helpers with type hints/dataclasses).
- `ChatGPT/`, `Claude/`, and `Google/` store raw exports and attachments; treat them as read-only inputs. `cache/`, `conversation_cache.db*`, and other `*.db` files are LLM caches; keep them untracked.
- `output/` contains generated markdown (`*-full`, `*-test`, `memories`, `gemini`, `RESEARCH_INDEX.md`). Regenerate instead of editing by hand.

## Build, Test, and Development Commands
- `uv run scripts/conversation_summarizer.py ChatGPT/conversations.json --output-dir output/chatgpt-full --max 50` — analyze a ChatGPT export with caching to `conversation_cache.db`.
- `python scripts/simple_extractor.py ChatGPT/conversations.json --output output/chatgpt-test` — quick export; add `--summary` to truncate long messages.
- `python scripts/gemini_extractor.py Google/<Gemini_takeout_dir>/MyActivity.json --output output/gemini` — convert Gemini Apps Activity; add `--no-grouping` to keep entries independent.
- `python scripts/memories_to_md.py` — convert `Claude/memories.json` into `output/memories/`.
- `python scripts/research_index.py` — rebuild `output/RESEARCH_INDEX.md` from processed outputs.
- Use Python 3.11+; if you skip `uv`, ensure `rich`, `pandas`, `openpyxl`, `ijson`, and core stdlib deps are installed in your venv.

## Coding Style & Naming Conventions
- Python with 4-space indentation and PEP 8 defaults; keep type hints and dataclasses as seen in `parser.py`.
- Prefer pure helpers and reuse existing parsers instead of duplicating traversal logic; keep functions small and side-effect aware.
- Keep filenames slugified (see `slugify` helpers) and avoid spaces in generated paths.
- When adding dependencies, prefer the `# /// script dependencies` block for `uv run`, or document install steps at the script entrypoint.

## Testing Guidelines
- No automated suite yet; validate changes by running the commands above against sample exports in `ChatGPT/`, `Claude/`, and `Google/`.
- Check counts and outputs: expected files under `output/`, sensible message ordering/session grouping, and reasonable metadata in generated markdown (indexes plus per-conversation files).
- For new parsing logic, add a small `tests/` fixture set and a `pytest` smoke test covering ChatGPT, Claude, and Gemini flows (query + Canvas entries).

## Commit & Pull Request Guidelines
- No history to mirror; use short, imperative commits (e.g., `feat: improve claude parsing`).
- In PRs/patches, note which exports you used, commands run, and where outputs landed. Link issues when applicable; screenshots are only needed if output formatting changes.
- Avoid committing raw exports, cache databases, or regenerated markdown unless illustrating a specific before/after change.

## Data & Security
- Exports contain personal conversations and Google activity; redact sensitive content from examples and issue descriptions.
- Treat cache DBs and generated markdown as disposable artifacts—regenerate instead of editing them manually.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ai-conversation-toolkit** (1760 symbols, 3800 relationships, 153 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
