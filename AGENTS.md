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
# GitNexus MCP

This project is indexed by GitNexus as **AI-Conversations-Export-Toolkit** (778 symbols, 2039 relationships, 58 execution flows).

## Always Start Here

1. **Read `gitnexus://repo/{name}/context`** — codebase overview + check index freshness
2. **Match your task to a skill below** and **read that skill file**
3. **Follow the skill's workflow and checklist**

> If step 1 warns the index is stale, run `npx gitnexus analyze` in the terminal first.

## Skills

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
