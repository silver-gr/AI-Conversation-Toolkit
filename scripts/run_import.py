#!/usr/bin/env python3
"""
Unified Import CLI - Run complete AI conversation import workflow.

This is the main entry point for running imports from ChatGPT, Claude, and Gemini.
It orchestrates the entire workflow including extraction, logging, and post-processing.
Supports multiple accounts per platform via accounts.json config file.

Usage:
    # Full import from accounts config (recommended for multi-account)
    python3 scripts/run_import.py --config accounts.json --incremental

    # Full import (all sources, single-account fallback)
    python3 scripts/run_import.py --all

    # Incremental import (only new conversations)
    python3 scripts/run_import.py --all --incremental

    # Specific sources
    python3 scripts/run_import.py --chatgpt ChatGPT/conversations.json
    python3 scripts/run_import.py --claude Claude/conversations.json
    python3 scripts/run_import.py --gemini "Google/My Activity/Gemini Apps/MyActivity.json"

    # With post-processing
    python3 scripts/run_import.py --all --research-index --no-biography

    # Dry run (show what would be done)
    python3 scripts/run_import.py --all --dry-run
"""

import argparse
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add scripts directory to path for imports
SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Import existing modules
from simple_extractor import process_export as extract_chatgpt_claude
from gemini_extractor import process_gemini_export as extract_gemini
from import_logger import ImportLogger
from research_index import create_research_index
from parser import parse_timestamp

# Optional rich imports for prettier output
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

    console = Console()
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    console = None


# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Default paths for export files (relative to project root)
DEFAULT_PATHS = {
    'chatgpt': Path('ChatGPT/conversations.json'),
    'claude': Path('Claude/conversations.json'),
    'gemini': Path('Google/Η δραστηριότητά μου/Εφαρμογές Gemini/MyActivity.json'),
}

# Alternative Gemini paths to try (Greek and English variations)
GEMINI_ALT_PATHS = [
    # Greek filename variant
    Path('Google/Η δραστηριότητά μου/Εφαρμογές Gemini/Ηδραστηριότητάμου.json'),
    # English paths
    Path('Google/My Activity/Gemini Apps/MyActivity.json'),
    Path('Google/Gemini/MyActivity.json'),
]

# Default output directory
DEFAULT_OUTPUT_DIR = Path('output')

# Import log directory
DEFAULT_IMPORTS_DIR = Path('imports')

# Default accounts config file
DEFAULT_CONFIG = Path('accounts.json')


def load_accounts_config(config_path: Path, base_dir: Path) -> list[dict] | None:
    """Load multi-account configuration from JSON file.

    Args:
        config_path: Path to accounts.json
        base_dir: Project root for resolving relative paths

    Returns:
        List of account dicts with keys: name, source, path.
        None if config file doesn't exist.

    Raises:
        ValueError: If config is malformed.
    """
    if not config_path.exists():
        return None

    with open(config_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    accounts = data.get('accounts', [])
    if not accounts:
        raise ValueError(f"No accounts defined in {config_path}")

    valid_sources = {'chatgpt', 'claude', 'gemini'}
    resolved = []
    for entry in accounts:
        name = entry.get('name', '').strip()
        source = entry.get('source', '').strip().lower()
        export_path = entry.get('export_path', '').strip()

        if not all([name, source, export_path]):
            raise ValueError(
                f"Account entry missing required fields (name, source, export_path): {entry}"
            )
        if source not in valid_sources:
            raise ValueError(
                f"Invalid source '{source}' for account '{name}'. "
                f"Must be one of: {', '.join(sorted(valid_sources))}"
            )

        path = Path(export_path)
        if not path.is_absolute():
            path = base_dir / path

        resolved.append({
            'name': name,
            'source': source,
            'path': path,
        })

    return resolved


# ==============================================================================
# OUTPUT HELPERS
# ==============================================================================

def print_header(title: str):
    """Print a styled header."""
    if RICH_AVAILABLE:
        console.print(Panel(f"[bold cyan]{title}[/bold cyan]", expand=False))
    else:
        print("=" * 60)
        print(title)
        print("=" * 60)


def print_info(message: str):
    """Print an info message."""
    if RICH_AVAILABLE:
        console.print(f"[blue]{message}[/blue]")
    else:
        print(f"INFO: {message}")


def print_success(message: str):
    """Print a success message."""
    if RICH_AVAILABLE:
        console.print(f"[green]{message}[/green]")
    else:
        print(f"SUCCESS: {message}")


def print_warning(message: str):
    """Print a warning message."""
    if RICH_AVAILABLE:
        console.print(f"[yellow]{message}[/yellow]")
    else:
        print(f"WARNING: {message}")


def print_error(message: str):
    """Print an error message."""
    if RICH_AVAILABLE:
        console.print(f"[red]{message}[/red]")
    else:
        print(f"ERROR: {message}")


def print_table(title: str, headers: list, rows: list):
    """Print a table with data."""
    if RICH_AVAILABLE:
        table = Table(title=title)
        for header in headers:
            table.add_column(header)
        for row in rows:
            table.add_row(*[str(cell) for cell in row])
        console.print(table)
    else:
        print(f"\n{title}")
        print("-" * 60)
        print(" | ".join(headers))
        print("-" * 60)
        for row in rows:
            print(" | ".join(str(cell) for cell in row))
        print()


# ==============================================================================
# PATH RESOLUTION
# ==============================================================================

def find_gemini_path(base_dir: Path) -> Optional[Path]:
    """Try to find the Gemini export file in various locations."""
    # Try default path first
    default_path = base_dir / DEFAULT_PATHS['gemini']
    if default_path.exists():
        return default_path

    # Try alternative paths
    for alt_path in GEMINI_ALT_PATHS:
        full_path = base_dir / alt_path
        if full_path.exists():
            return full_path

    return None


def resolve_paths(
    base_dir: Path,
    chatgpt_path: Optional[str],
    claude_path: Optional[str],
    gemini_path: Optional[str],
    use_all: bool
) -> dict:
    """Resolve paths for each source."""
    paths = {}

    if use_all:
        # Use default paths for all sources
        if chatgpt_path:
            paths['chatgpt'] = Path(chatgpt_path)
        else:
            default = base_dir / DEFAULT_PATHS['chatgpt']
            # Include ChatGPT if the merged file exists OR split exports are
            # present (they get merged into `default` before extraction).
            if default.exists() or list(default.parent.glob('conversations-*.json')):
                paths['chatgpt'] = default

        if claude_path:
            paths['claude'] = Path(claude_path)
        else:
            default = base_dir / DEFAULT_PATHS['claude']
            if default.exists():
                paths['claude'] = default

        if gemini_path:
            paths['gemini'] = Path(gemini_path)
        else:
            found = find_gemini_path(base_dir)
            if found:
                paths['gemini'] = found
    else:
        # Only use explicitly specified paths
        if chatgpt_path:
            paths['chatgpt'] = Path(chatgpt_path)
        if claude_path:
            paths['claude'] = Path(claude_path)
        if gemini_path:
            paths['gemini'] = Path(gemini_path)

    return paths


def merge_chatgpt_splits(target: Path) -> Path:
    """Merge split ChatGPT exports (conversations-NNN.json) into a single file.

    Since ~March 2026, ChatGPT Takeout ships conversations as
    conversations-000.json ... conversations-NNN.json (100 convs each) with no
    combined conversations.json. This merges them, in filename order, into
    `target`. The split files are the source of truth, so the merged file is
    rewritten whenever any split is newer than the existing merged file.

    Returns the path the extractor should read (always `target`).
    """
    split_files = sorted(target.parent.glob('conversations-*.json'))
    if not split_files:
        return target  # single-file export (or nothing) - use target as-is

    # Skip re-merging if the combined file is already up to date.
    if target.exists():
        target_mtime = target.stat().st_mtime
        if all(sf.stat().st_mtime <= target_mtime for sf in split_files):
            return target

    merged = []
    for sf in split_files:
        with open(sf, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            merged.extend(data)
        else:
            merged.append(data)

    with open(target, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False)

    print_info(
        f"  Merged {len(split_files)} ChatGPT split files into "
        f"{target.name} ({len(merged):,} conversations)"
    )
    return target


# ==============================================================================
# EXTRACTION WRAPPERS
# ==============================================================================

def extract_source(
    source: str,
    input_path: Path,
    output_dir: Path,
    incremental: bool = False,
    imported_ids: set = None,
    account: str | None = None,
) -> dict:
    """
    Extract conversations from a source.

    Args:
        source: Base source type ('chatgpt', 'claude', 'gemini')
        input_path: Path to the export file
        output_dir: Base output directory
        incremental: Skip already imported conversations
        imported_ids: Set of previously imported conversation IDs
        account: Optional account name for multi-account setups

    Returns dict with:
        - file: source filename
        - total_in_export: total conversations in file
        - new_imported: newly imported count
        - skipped_existing: skipped count
        - conversation_ids: list of imported conversation IDs
    """
    stats = {
        'file': input_path.name,
        'total_in_export': 0,
        'new_imported': 0,
        'skipped_existing': 0,
        'conversation_ids': [],
    }

    if not input_path.exists():
        print_error(f"File not found: {input_path}")
        return stats

    label = f"{source.upper()} ({account})" if account else source.upper()
    print_info(f"Processing {label}: {input_path}")

    try:
        if source == 'gemini':
            result = extract_gemini(
                input_path,
                output_dir / 'gemini',
                incremental=incremental,
                imported_ids=imported_ids if imported_ids else set(),
                account=account,
            )
            stats['total_in_export'] = result.get('total_activities', result.get('total', 0))
            stats['new_imported'] = result.get('processed', 0)
            stats['skipped_existing'] = result.get('skipped', 0)
            stats['conversation_ids'] = result.get('new_ids', [])
        else:
            # ChatGPT or Claude
            result = extract_chatgpt_claude(
                input_path,
                output_dir / f'{source}-full',
                incremental=incremental,
                imported_ids=imported_ids,
                account=account,
            )
            stats['total_in_export'] = result.get('total', 0)
            stats['new_imported'] = result.get('processed', 0)
            stats['skipped_existing'] = result.get('skipped', 0)
            stats['conversation_ids'] = result.get('new_ids', [])

        print_success(
            f"  Imported {stats['new_imported']} conversations from {label}"
        )

    except Exception as e:
        print_error(f"Failed to extract {label}: {e}")
        raise

    return stats


# ==============================================================================
# POST-PROCESSING
# ==============================================================================

def run_research_index(output_dir: Path) -> bool:
    """Run the research index generator."""
    print_info("Generating research index...")

    try:
        create_research_index(
            claude_dir=output_dir / 'claude-full',
            chatgpt_dir=output_dir / 'chatgpt-full',
            gemini_dir=output_dir / 'gemini',
            output_path=output_dir / 'RESEARCH_INDEX.md'
        )
        print_success("Research index created")
        return True
    except Exception as e:
        print_error(f"Failed to create research index: {e}")
        return False


def run_memories_extraction(base_dir: Path, output_dir: Path) -> bool:
    """Run the Claude memories extraction."""
    print_info("Extracting Claude memories...")

    try:
        from memories_to_md import process_memories

        input_path = base_dir / 'Claude' / 'memories.json'
        memories_output = output_dir / 'memories'

        if not input_path.exists():
            print_warning(f"Memories file not found: {input_path}")
            return False

        memories_output.mkdir(parents=True, exist_ok=True)
        stats = process_memories(input_path, memories_output)
        print_success(f"Extracted {stats['processed']} memory files")
        return True

    except Exception as e:
        print_error(f"Failed to extract memories: {e}")
        return False


def run_biography_extractor(all_sources: bool = True) -> bool:
    """Run the biography extractor."""
    print_info("Running biography extractor...")

    try:
        import asyncio
        from biography_extractor import BiographyExtractor, CONVERSATION_DIRS

        extractor = BiographyExtractor()
        conversations = extractor.discover_conversations(CONVERSATION_DIRS)

        if not conversations:
            print_warning("No conversations found for biography extraction")
            return False

        # Run async extraction
        extractions = asyncio.run(
            extractor.process_batch_async(conversations, batch_size=5)
        )

        print_success(f"Processed {len(extractions)} conversations for biography")
        return True

    except ImportError:
        print_warning("Biography extractor requires 'rich' package")
        return False
    except Exception as e:
        print_error(f"Failed to run biography extractor: {e}")
        return False


# ==============================================================================
# MAIN WORKFLOW
# ==============================================================================

def run_import(
    chatgpt_path: Optional[str] = None,
    claude_path: Optional[str] = None,
    gemini_path: Optional[str] = None,
    use_all: bool = False,
    incremental: bool = False,
    output_dir: Optional[str] = None,
    imports_dir: Optional[str] = None,
    run_research: bool = False,
    run_biography: bool = False,
    run_memories: bool = False,
    notes: str = "",
    dry_run: bool = False,
    config_path: Optional[str] = None,
) -> int:
    """
    Run the complete import workflow.

    Returns:
        0 on success, 1 on failure
    """
    # Resolve base directory (project root)
    base_dir = SCRIPT_DIR.parent

    # Resolve output directories
    output_path = Path(output_dir) if output_dir else base_dir / DEFAULT_OUTPUT_DIR
    imports_path = Path(imports_dir) if imports_dir else base_dir / DEFAULT_IMPORTS_DIR

    # Build list of accounts to process: [{name, source, path}, ...]
    accounts = None

    # Try config file first
    cfg_file = Path(config_path) if config_path else base_dir / DEFAULT_CONFIG
    if config_path or (use_all and cfg_file.exists()):
        try:
            accounts = load_accounts_config(cfg_file, base_dir)
            if accounts:
                print_info(f"Loaded {len(accounts)} accounts from {cfg_file}")
        except (ValueError, json.JSONDecodeError) as e:
            if config_path:
                # Explicit --config: fail hard
                print_error(f"Config error: {e}")
                return 1
            else:
                # Implicit auto-detect: warn and fall back
                print_warning(f"Config error in {cfg_file}: {e}")
                print_info("Falling back to default paths")

    # Fall back to legacy path resolution
    if not accounts:
        source_paths = resolve_paths(
            base_dir, chatgpt_path, claude_path, gemini_path, use_all
        )
        if not source_paths:
            print_error("No source files found or specified")
            print_info(
                "Use --config accounts.json, --all, or specify paths explicitly"
            )
            return 1
        # Convert to account format (unnamed accounts)
        accounts = [
            {'name': None, 'source': source, 'path': path}
            for source, path in source_paths.items()
        ]

    # Print configuration
    print_header("AI Conversation Import")
    print_info(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print_info(f"Output: {output_path}")
    print_info(f"Mode: {'Incremental' if incremental else 'Full'}")

    if dry_run:
        print_warning("DRY RUN - No changes will be made")

    print()
    print_info("Accounts to process:")
    for acct in accounts:
        label = (
            f"{acct['source'].upper()} ({acct['name']})"
            if acct['name'] else acct['source'].upper()
        )
        exists = acct['path'].exists()
        n_splits = 0
        if acct['source'] == 'chatgpt' and not exists:
            n_splits = len(list(acct['path'].parent.glob('conversations-*.json')))
        if exists:
            status, color = "[exists]", "green"
        elif n_splits:
            status, color = f"[{n_splits} split files -> will merge]", "yellow"
        else:
            status, color = "[not found]", "red"
        if RICH_AVAILABLE:
            console.print(f"  {label}: {acct['path']} [{color}]{status}[/{color}]")
        else:
            print(f"  {label}: {acct['path']} {status}")

    print()

    # Early exit for dry run
    if dry_run:
        print_info("Post-processing steps:")
        print_info(f"  Research Index: {'Yes' if run_research else 'No'}")
        print_info(f"  Biography Extractor: {'Yes' if run_biography else 'No'}")
        print_info(f"  Memories Extraction: {'Yes' if run_memories else 'No'}")
        return 0

    # Initialize import logger
    logger = ImportLogger(imports_path)
    import_record = logger.start_import(notes=notes)
    import_id = import_record['id']

    print_success(f"Started import session: {import_id}")
    print()

    # Track results
    results = {}
    post_processing_steps = []
    success = True

    try:
        # Process each account
        for acct in accounts:
            source = acct['source']
            path = acct['path']
            account_name = acct['name']

            # Auto-merge split ChatGPT exports (conversations-NNN.json) into a
            # single conversations.json before extraction.
            if source == 'chatgpt':
                path = merge_chatgpt_splits(path)
                acct['path'] = path

            if not path.exists():
                label = (
                    f"{source} ({account_name})"
                    if account_name else source
                )
                print_warning(f"Skipping {label}: file not found")
                continue

            # Composite key for import log: "chatgpt:personal" or "chatgpt"
            log_key = (
                f"{source}:{account_name}" if account_name else source
            )

            try:
                # Load previously imported IDs for incremental mode
                # (pools across all accounts of the same source type)
                imported_ids = None
                if incremental:
                    imported_ids = logger.get_imported_ids(source)
                    if imported_ids:
                        print_info(
                            f"  Found {len(imported_ids)} previously "
                            f"imported {source} conversations"
                        )

                stats = extract_source(
                    source=source,
                    input_path=path,
                    output_dir=output_path,
                    incremental=incremental,
                    imported_ids=imported_ids,
                    account=account_name,
                )

                results[log_key] = stats

                # Record in import log with composite key
                logger.record_source(import_id, log_key, stats)

            except Exception as e:
                print_error(f"Failed to process {log_key}: {e}")
                results[log_key] = {
                    'file': path.name,
                    'total_in_export': 0,
                    'new_imported': 0,
                    'skipped_existing': 0,
                    'conversation_ids': [],
                    'error': str(e),
                }
                success = False

        print()

        # Run post-processing steps
        if run_research:
            if run_research_index(output_path):
                post_processing_steps.append('research_index')

        if run_memories:
            if run_memories_extraction(base_dir, output_path):
                post_processing_steps.append('memories')

        if run_biography:
            if run_biography_extractor():
                post_processing_steps.append('biography')

        # Complete the import
        logger.complete_import(import_id, post_processing_steps)

    except Exception as e:
        print_error(f"Import failed: {e}")
        logger.cancel_import(import_id, str(e))
        return 1

    # Print summary
    print()
    print_header("Import Summary")

    # Results table
    headers = ["Source", "Account", "Total", "Imported", "Skipped"]
    rows = []

    total_imported = 0
    total_in_export = 0

    for log_key, stats in results.items():
        base = log_key.split(':')[0]
        account = log_key.split(':')[1] if ':' in log_key else '-'
        rows.append([
            base.upper(),
            account,
            stats['total_in_export'],
            stats['new_imported'],
            stats['skipped_existing'],
        ])
        total_imported += stats['new_imported']
        total_in_export += stats['total_in_export']

    if rows:
        print_table("Extraction Results", headers, rows)

    print_info(f"Total conversations imported: {total_imported}")
    print_info(f"Import ID: {import_id}")

    if post_processing_steps:
        print_info(f"Post-processing: {', '.join(post_processing_steps)}")

    print_info(f"Import log: {imports_path / 'IMPORT_LOG.md'}")

    return 0 if success else 1


# ==============================================================================
# CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Unified AI Conversation Import CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Multi-account import (recommended)
  python3 scripts/run_import.py --config accounts.json --incremental

  # Process all sources (uses accounts.json if present)
  python3 scripts/run_import.py --all

  # Process only ChatGPT with custom path
  python3 scripts/run_import.py --chatgpt exports/chatgpt.json

  # Full import with research index generation
  python3 scripts/run_import.py --all --research-index

  # Incremental import (skip existing)
  python3 scripts/run_import.py --all --incremental

  # Preview what would happen
  python3 scripts/run_import.py --all --dry-run
        """
    )

    # Source options
    source_group = parser.add_argument_group('Source Options')
    source_group.add_argument(
        '--all', '-a',
        action='store_true',
        dest='use_all',
        help='Process all sources (uses accounts.json if present, else default paths)'
    )
    source_group.add_argument(
        '--config', '-c',
        metavar='FILE',
        help='Path to accounts.json config file for multi-account imports'
    )
    source_group.add_argument(
        '--chatgpt',
        metavar='PATH',
        help='Path to ChatGPT conversations.json'
    )
    source_group.add_argument(
        '--claude',
        metavar='PATH',
        help='Path to Claude conversations.json'
    )
    source_group.add_argument(
        '--gemini',
        metavar='PATH',
        help='Path to Gemini MyActivity.json'
    )

    # Import mode
    mode_group = parser.add_argument_group('Import Mode')
    mode_group.add_argument(
        '--incremental', '-i',
        action='store_true',
        help='Only import new conversations (skip existing)'
    )
    mode_group.add_argument(
        '--full',
        action='store_true',
        help='Import all conversations (default)'
    )

    # Post-processing options
    post_group = parser.add_argument_group('Post-Processing')
    post_group.add_argument(
        '--research-index', '-r',
        action='store_true',
        help='Generate research conversations index'
    )
    post_group.add_argument(
        '--biography',
        action='store_true',
        help='Run biography extractor (requires rich package)'
    )
    post_group.add_argument(
        '--no-biography',
        action='store_true',
        help='Skip biography extractor (default)'
    )
    post_group.add_argument(
        '--memories', '-m',
        action='store_true',
        help='Extract Claude memories to markdown'
    )

    # Output options
    output_group = parser.add_argument_group('Output Options')
    output_group.add_argument(
        '--output-dir', '-o',
        metavar='DIR',
        help='Base output directory (default: output/)'
    )
    output_group.add_argument(
        '--imports-dir',
        metavar='DIR',
        help='Import logs directory (default: imports/)'
    )
    output_group.add_argument(
        '--notes', '-n',
        default='',
        help='Notes to attach to this import session'
    )
    output_group.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )

    args = parser.parse_args()

    # Validate arguments
    has_source = any([args.chatgpt, args.claude, args.gemini])
    if not args.use_all and not args.config and not has_source:
        parser.print_help()
        print()
        print_error(
            "No sources specified. Use --config, --all, or specify paths."
        )
        return 1

    # Run import
    return run_import(
        chatgpt_path=args.chatgpt,
        claude_path=args.claude,
        gemini_path=args.gemini,
        use_all=args.use_all or bool(args.config),
        incremental=args.incremental,
        output_dir=args.output_dir,
        imports_dir=args.imports_dir,
        run_research=args.research_index,
        run_biography=args.biography and not args.no_biography,
        run_memories=args.memories,
        notes=args.notes,
        dry_run=args.dry_run,
        config_path=args.config,
    )


if __name__ == '__main__':
    sys.exit(main())
