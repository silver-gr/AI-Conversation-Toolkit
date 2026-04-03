#!/usr/bin/env python3
"""
Import Logger Module - Tracks AI conversation imports over time.

Provides a reusable ImportLogger class that maintains a JSON log of all imports
with per-source statistics, and generates human-readable markdown reports.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ImportLogger:
    """
    Manages import logging for AI conversation exports.

    Tracks import sessions across multiple sources (ChatGPT, Claude, Gemini),
    stores detailed statistics, and generates markdown reports.

    Attributes:
        imports_dir: Directory containing import logs
        log_file: Path to the JSON log file
        markdown_file: Path to the generated markdown report
    """

    SUPPORTED_SOURCES = frozenset({'chatgpt', 'claude', 'gemini'})

    def __init__(self, imports_dir: Path | str):
        """
        Initialize the ImportLogger.

        Args:
            imports_dir: Directory to store import logs. Will be created if it doesn't exist.
        """
        self.imports_dir = Path(imports_dir)
        self.log_file = self.imports_dir / "import_log.json"
        # Markdown file will be timestamped in _generate_markdown()
        self.markdown_file = None  # Set dynamically

        # Ensure directory exists
        self.imports_dir.mkdir(parents=True, exist_ok=True)

        # Initialize log file if it doesn't exist
        if not self.log_file.exists():
            self._write_log(self._create_empty_log())

    def _create_empty_log(self) -> dict:
        """Create an empty log structure."""
        return {
            "imports": [],
            "metadata": {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_imports": 0,
                "schema_version": "1.1.0"
            }
        }

    def _read_log(self) -> dict:
        """
        Read the current log file.

        Returns:
            The parsed JSON log data.

        Raises:
            json.JSONDecodeError: If the log file contains invalid JSON.
            IOError: If the file cannot be read.
        """
        try:
            with open(self.log_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return self._create_empty_log()
        except json.JSONDecodeError as e:
            # Create backup of corrupted file
            backup_path = self.log_file.with_suffix('.json.bak')
            if self.log_file.exists():
                self.log_file.rename(backup_path)
            raise ValueError(
                f"Corrupted log file. Backup saved to {backup_path}. "
                f"Original error: {e}"
            ) from e

    def _write_log(self, data: dict) -> None:
        """
        Write log data to file with atomic write pattern.

        Args:
            data: The log data to write.
        """
        # Update metadata
        data["metadata"]["last_updated"] = datetime.now(timezone.utc).isoformat()
        data["metadata"]["total_imports"] = len(data["imports"])

        # Write to temp file first, then rename (atomic on POSIX)
        temp_file = self.log_file.with_suffix('.json.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        temp_file.replace(self.log_file)

    def _generate_import_id(self) -> str:
        """Generate a unique import ID based on current timestamp with microseconds."""
        # Include microseconds to handle rapid successive calls
        return f"import_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    def start_import(self, import_id: str | None = None, notes: str = "") -> dict:
        """
        Start a new import session.

        Creates a new import record with pending status. The import_id is
        auto-generated if not provided.

        Args:
            import_id: Optional custom import ID. Auto-generated if None.
            notes: Optional notes for this import session.

        Returns:
            The created import record dictionary.

        Raises:
            ValueError: If an import with the same ID already exists.
        """
        log_data = self._read_log()

        # Generate ID if not provided
        if import_id is None:
            import_id = self._generate_import_id()

        # Check for duplicate ID
        existing_ids = {imp["id"] for imp in log_data["imports"]}
        if import_id in existing_ids:
            raise ValueError(f"Import with ID '{import_id}' already exists")

        # Create new import record
        import_record = {
            "id": import_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "status": "in_progress",
            "sources": {},
            "post_processing": [],
            "notes": notes
        }

        log_data["imports"].append(import_record)
        self._write_log(log_data)

        return import_record.copy()

    @staticmethod
    def _base_source(source_key: str) -> str:
        """Extract base source from a possibly account-qualified key.

        'chatgpt:personal' -> 'chatgpt', 'claude' -> 'claude'
        """
        return source_key.split(':')[0]

    def record_source(
        self,
        import_id: str,
        source: str,
        stats: dict[str, Any]
    ) -> None:
        """
        Record statistics for a source within an import session.

        Args:
            import_id: The import session ID.
            source: Source name, optionally account-qualified.
                Plain: 'chatgpt', 'claude', 'gemini'
                With account: 'chatgpt:personal', 'claude:pro'
            stats: Dictionary containing source statistics. Expected keys:
                - file: Source file name
                - total_in_export: Total conversations in the export file
                - new_imported: Number of newly imported conversations
                - skipped_existing: Number of skipped (already imported) conversations
                - conversation_ids: List of imported conversation IDs

        Raises:
            ValueError: If the import_id doesn't exist or source is invalid.
            KeyError: If required stats keys are missing.
        """
        # Validate base source (before the colon)
        source = source.lower()
        base = self._base_source(source)
        if base not in self.SUPPORTED_SOURCES:
            raise ValueError(
                f"Invalid source '{source}'. Base must be one of: "
                f"{', '.join(sorted(self.SUPPORTED_SOURCES))}"
            )

        # Validate required stats keys
        required_keys = {'file', 'total_in_export', 'new_imported',
                        'skipped_existing', 'conversation_ids'}
        missing_keys = required_keys - set(stats.keys())
        if missing_keys:
            raise KeyError(f"Missing required stats keys: {missing_keys}")

        log_data = self._read_log()

        # Find the import record
        import_record = None
        for imp in log_data["imports"]:
            if imp["id"] == import_id:
                import_record = imp
                break

        if import_record is None:
            raise ValueError(f"Import '{import_id}' not found")

        if import_record["status"] == "completed":
            raise ValueError(
                f"Cannot modify completed import '{import_id}'. "
                "Start a new import session instead."
            )

        # Record source stats
        import_record["sources"][source] = {
            "file": stats["file"],
            "total_in_export": int(stats["total_in_export"]),
            "new_imported": int(stats["new_imported"]),
            "skipped_existing": int(stats["skipped_existing"]),
            "conversation_ids": list(stats["conversation_ids"])
        }

        self._write_log(log_data)

    def complete_import(
        self,
        import_id: str,
        post_processing: list[str] | None = None
    ) -> dict:
        """
        Mark an import session as complete.

        Finalizes the import record, sets completion timestamp, and
        regenerates the markdown report.

        Args:
            import_id: The import session ID.
            post_processing: List of post-processing steps performed
                (e.g., ['research_index', 'biography_extractor']).

        Returns:
            The completed import record.

        Raises:
            ValueError: If the import_id doesn't exist or is already completed.
        """
        log_data = self._read_log()

        # Find the import record
        import_record = None
        for imp in log_data["imports"]:
            if imp["id"] == import_id:
                import_record = imp
                break

        if import_record is None:
            raise ValueError(f"Import '{import_id}' not found")

        if import_record["status"] == "completed":
            raise ValueError(f"Import '{import_id}' is already completed")

        # Update record
        import_record["completed_at"] = datetime.now(timezone.utc).isoformat()
        import_record["status"] = "completed"
        import_record["post_processing"] = list(post_processing or [])

        self._write_log(log_data)

        # Regenerate markdown
        self._generate_markdown(log_data)

        return import_record.copy()

    def cancel_import(self, import_id: str, reason: str = "") -> None:
        """
        Cancel an in-progress import session.

        Args:
            import_id: The import session ID.
            reason: Optional reason for cancellation.

        Raises:
            ValueError: If the import_id doesn't exist or is already completed.
        """
        log_data = self._read_log()

        # Find the import record
        import_record = None
        for imp in log_data["imports"]:
            if imp["id"] == import_id:
                import_record = imp
                break

        if import_record is None:
            raise ValueError(f"Import '{import_id}' not found")

        if import_record["status"] == "completed":
            raise ValueError(f"Cannot cancel completed import '{import_id}'")

        # Update record
        import_record["completed_at"] = datetime.now(timezone.utc).isoformat()
        import_record["status"] = "cancelled"
        if reason:
            import_record["notes"] = (
                f"{import_record.get('notes', '')} [Cancelled: {reason}]".strip()
            )

        self._write_log(log_data)
        self._generate_markdown(log_data)

    def get_last_import(self) -> dict | None:
        """
        Get the most recent import record.

        Returns:
            The most recent import record, or None if no imports exist.
        """
        log_data = self._read_log()

        if not log_data["imports"]:
            return None

        # Imports are stored chronologically, last one is most recent
        return log_data["imports"][-1].copy()

    def get_import(self, import_id: str) -> dict | None:
        """
        Get a specific import record by ID.

        Args:
            import_id: The import session ID.

        Returns:
            The import record, or None if not found.
        """
        log_data = self._read_log()

        for imp in log_data["imports"]:
            if imp["id"] == import_id:
                return imp.copy()

        return None

    def get_imported_ids(self, source: str) -> set[str]:
        """
        Get all conversation IDs that have been imported for a source.

        Scans all completed imports and aggregates conversation IDs.
        Matches by base source prefix so 'chatgpt' collects IDs from
        'chatgpt', 'chatgpt:personal', 'chatgpt:work', etc.

        Args:
            source: Source name or account-qualified key.
                'chatgpt' matches all ChatGPT accounts.
                'chatgpt:personal' also works (matches by base source).

        Returns:
            Set of all imported conversation IDs for the source.

        Raises:
            ValueError: If base source is invalid.
        """
        source = source.lower()
        base = self._base_source(source)
        if base not in self.SUPPORTED_SOURCES:
            raise ValueError(
                f"Invalid source '{source}'. Base must be one of: "
                f"{', '.join(sorted(self.SUPPORTED_SOURCES))}"
            )

        log_data = self._read_log()
        imported_ids: set[str] = set()

        for imp in log_data["imports"]:
            # Only include completed imports
            if imp["status"] != "completed":
                continue

            for src_key, src_data in imp.get("sources", {}).items():
                if self._base_source(src_key) == base:
                    imported_ids.update(src_data.get("conversation_ids", []))

        return imported_ids

    def get_all_imports(self) -> list[dict]:
        """
        Get all import records.

        Returns:
            List of all import records (copies).
        """
        log_data = self._read_log()
        return [imp.copy() for imp in log_data["imports"]]

    def get_statistics(self) -> dict:
        """
        Get aggregate statistics across all imports.

        Returns:
            Dictionary with aggregate statistics:
            - total_imports: Number of completed imports
            - by_source: Per-source totals
            - first_import: Timestamp of first import
            - last_import: Timestamp of last import
        """
        log_data = self._read_log()

        stats = {
            "total_imports": 0,
            "by_source": {
                "chatgpt": {"conversations": 0, "imports": 0},
                "claude": {"conversations": 0, "imports": 0},
                "gemini": {"conversations": 0, "imports": 0}
            },
            "first_import": None,
            "last_import": None
        }

        completed_imports = [
            imp for imp in log_data["imports"]
            if imp["status"] == "completed"
        ]

        if not completed_imports:
            return stats

        stats["total_imports"] = len(completed_imports)
        stats["first_import"] = completed_imports[0].get("started_at")
        stats["last_import"] = completed_imports[-1].get("completed_at")

        for imp in completed_imports:
            for source_key, source_data in imp.get("sources", {}).items():
                base = self._base_source(source_key)
                if base in stats["by_source"]:
                    stats["by_source"][base]["conversations"] += (
                        source_data.get("new_imported", 0)
                    )
                    stats["by_source"][base]["imports"] += 1

        return stats

    def _generate_markdown(self, log_data: dict | None = None) -> None:
        """
        Generate the markdown report from log data.

        Args:
            log_data: Optional pre-loaded log data. Will read from file if None.
        """
        if log_data is None:
            log_data = self._read_log()

        lines = []

        # Header
        lines.append("# Import Log")
        lines.append("")
        lines.append(f"*Last updated: {log_data['metadata']['last_updated']}*")
        lines.append("")

        # Statistics
        stats = self.get_statistics()
        lines.append("## Summary Statistics")
        lines.append("")
        lines.append(f"- **Total Imports**: {stats['total_imports']}")

        if stats['first_import']:
            first_date = stats['first_import'][:10]
            lines.append(f"- **First Import**: {first_date}")

        if stats['last_import']:
            last_date = stats['last_import'][:10]
            lines.append(f"- **Last Import**: {last_date}")

        lines.append("")
        lines.append("### By Source")
        lines.append("")
        lines.append("| Source | Total Conversations | Import Sessions |")
        lines.append("|--------|---------------------|-----------------|")

        for source in ['chatgpt', 'claude', 'gemini']:
            source_stats = stats['by_source'][source]
            lines.append(
                f"| {source.upper()} | "
                f"{source_stats['conversations']:,} | "
                f"{source_stats['imports']} |"
            )

        lines.append("")
        lines.append("---")
        lines.append("")

        # Import History Table
        lines.append("## Import History")
        lines.append("")

        completed_imports = [
            imp for imp in log_data["imports"]
            if imp["status"] == "completed"
        ]

        if not completed_imports:
            lines.append("*No completed imports yet.*")
        else:
            lines.append(
                "| Date | ID | ChatGPT | Claude | Gemini | "
                "Post-Processing |"
            )
            lines.append(
                "|------|----|---------:|--------:|--------:|"
                "-----------------|"
            )

            # Show most recent first
            for imp in reversed(completed_imports):
                date = imp.get("completed_at", imp.get("started_at", ""))[:10]
                import_id = imp["id"]

                # Aggregate counts by base source (handles account-qualified keys)
                source_counts: dict[str, dict] = {}
                for src_key, src_data in imp.get("sources", {}).items():
                    base = self._base_source(src_key)
                    if base not in source_counts:
                        source_counts[base] = {
                            "new_imported": 0, "skipped_existing": 0
                        }
                    source_counts[base]["new_imported"] += (
                        src_data.get("new_imported", 0)
                    )
                    source_counts[base]["skipped_existing"] += (
                        src_data.get("skipped_existing", 0)
                    )

                # Format counts with skipped info
                chatgpt_str = self._format_count(
                    source_counts.get("chatgpt", {})
                )
                claude_str = self._format_count(
                    source_counts.get("claude", {})
                )
                gemini_str = self._format_count(
                    source_counts.get("gemini", {})
                )

                post = ", ".join(imp.get("post_processing", [])) or "-"

                lines.append(
                    f"| {date} | {import_id} | {chatgpt_str} | "
                    f"{claude_str} | {gemini_str} | {post} |"
                )

        lines.append("")
        lines.append("---")
        lines.append("")

        # Detailed Import Records
        lines.append("## Import Details")
        lines.append("")

        for imp in reversed(log_data["imports"]):
            self._append_import_details(lines, imp)

        # Write file with timestamp (preserves history)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.markdown_file = self.imports_dir / f'IMPORT_LOG_{timestamp}.md'
        self.markdown_file.write_text('\n'.join(lines), encoding='utf-8')

    def _format_count(self, source_data: dict) -> str:
        """Format count string with optional skipped indicator."""
        if not source_data:
            return "-"

        new = source_data.get("new_imported", 0)
        skipped = source_data.get("skipped_existing", 0)

        if skipped > 0:
            return f"{new:,} (+{skipped} skip)"
        return f"{new:,}" if new > 0 else "-"

    def _append_import_details(self, lines: list[str], imp: dict) -> None:
        """Append detailed import information to markdown lines."""
        import_id = imp["id"]
        status = imp["status"]
        status_emoji = {
            "completed": "completed",
            "in_progress": "in progress",
            "cancelled": "cancelled"
        }.get(status, status)

        lines.append(f"### {import_id}")
        lines.append("")
        lines.append(f"- **Status**: {status_emoji}")
        lines.append(f"- **Started**: {imp.get('started_at', 'N/A')}")

        if imp.get("completed_at"):
            lines.append(f"- **Completed**: {imp['completed_at']}")

        if imp.get("notes"):
            lines.append(f"- **Notes**: {imp['notes']}")

        lines.append("")

        # Source details
        sources = imp.get("sources", {})
        if sources:
            lines.append("**Sources:**")
            lines.append("")

            for source_key, source_data in sources.items():
                # Display account-qualified keys nicely
                base = self._base_source(source_key)
                account = source_key.split(':', 1)[1] if ':' in source_key else None
                label = f"{base.upper()} ({account})" if account else base.upper()
                lines.append(f"*{label}*:")
                lines.append(f"- File: `{source_data.get('file', 'N/A')}`")
                lines.append(
                    f"- Total in export: {source_data.get('total_in_export', 0):,}"
                )
                lines.append(
                    f"- New imported: {source_data.get('new_imported', 0):,}"
                )
                lines.append(
                    f"- Skipped (existing): "
                    f"{source_data.get('skipped_existing', 0):,}"
                )
                lines.append("")

        # Post-processing
        post = imp.get("post_processing", [])
        if post:
            lines.append(f"**Post-processing**: {', '.join(post)}")
            lines.append("")

        lines.append("---")
        lines.append("")

    def regenerate_markdown(self) -> None:
        """
        Force regeneration of the markdown report.

        Useful after manual edits to the JSON log or to fix formatting.
        """
        self._generate_markdown()


def main():
    """CLI for testing the ImportLogger."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Import Logger - Track AI conversation imports'
    )
    parser.add_argument(
        '--imports-dir', '-d',
        default='imports',
        help='Directory for import logs (default: imports)'
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Start command
    start_parser = subparsers.add_parser('start', help='Start a new import')
    start_parser.add_argument('--id', help='Custom import ID')
    start_parser.add_argument('--notes', default='', help='Import notes')

    # Record command
    record_parser = subparsers.add_parser('record', help='Record source stats')
    record_parser.add_argument('import_id', help='Import session ID')
    record_parser.add_argument('source', choices=['chatgpt', 'claude', 'gemini'])
    record_parser.add_argument('--file', required=True, help='Source file name')
    record_parser.add_argument('--total', type=int, required=True,
                              help='Total in export')
    record_parser.add_argument('--new', type=int, required=True,
                              help='New imported')
    record_parser.add_argument('--skipped', type=int, default=0,
                              help='Skipped existing')

    # Complete command
    complete_parser = subparsers.add_parser('complete', help='Complete an import')
    complete_parser.add_argument('import_id', help='Import session ID')
    complete_parser.add_argument('--post', nargs='*', default=[],
                                help='Post-processing steps')

    # Status command
    subparsers.add_parser('status', help='Show current status')

    # Regenerate command
    subparsers.add_parser('regenerate', help='Regenerate markdown report')

    args = parser.parse_args()

    logger = ImportLogger(Path(args.imports_dir))

    if args.command == 'start':
        record = logger.start_import(import_id=args.id, notes=args.notes)
        print(f"Started import: {record['id']}")

    elif args.command == 'record':
        stats = {
            'file': args.file,
            'total_in_export': args.total,
            'new_imported': args.new,
            'skipped_existing': args.skipped,
            'conversation_ids': []  # Would be populated by actual extraction
        }
        logger.record_source(args.import_id, args.source, stats)
        print(f"Recorded {args.source} stats for {args.import_id}")

    elif args.command == 'complete':
        record = logger.complete_import(args.import_id, args.post)
        print(f"Completed import: {record['id']}")
        print(f"Markdown generated: {logger.markdown_file}")

    elif args.command == 'status':
        last = logger.get_last_import()
        if last:
            print(f"Last import: {last['id']}")
            print(f"Status: {last['status']}")
            if last['sources']:
                print("Sources:")
                for src, data in last['sources'].items():
                    print(f"  {src}: {data.get('new_imported', 0)} new")
        else:
            print("No imports recorded yet")

        stats = logger.get_statistics()
        print(f"\nTotal completed imports: {stats['total_imports']}")

    elif args.command == 'regenerate':
        logger.regenerate_markdown()
        print(f"Regenerated: {logger.markdown_file}")

    else:
        parser.print_help()
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
