#!/usr/bin/env python3
"""
Simple Conversation Extractor - No AI Analysis Required
Converts ChatGPT and Claude exports to readable markdown with FULL content.
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Generator
from collections import defaultdict
import hashlib

# Support running from different directories
sys.path.insert(0, str(Path(__file__).parent))

from parser import slugify, parse_timestamp
from import_logger import ImportLogger


# ChatGPT citation marker pattern: 【...】 brackets with any content inside
# Matches web search refs (【turn0search2】), doc refs (【21†source】),
# titled refs (【14†Book Title†L80-L90】), numeric refs (【662637628283421†L75-L93】)
_CITATION_RE = re.compile(r'【[^】]*】')


def strip_chatgpt_citations(text: str) -> str:
    """Remove ChatGPT citation markers like 【turn0search2】 from text."""
    return _CITATION_RE.sub('', text)


# =============================================================================
# CLAUDE PARSER
# =============================================================================

def extract_claude_messages(conv: dict) -> list[dict]:
    """Extract messages from Claude conversation."""
    messages = []
    for msg in conv.get('chat_messages', []):
        sender = msg.get('sender', '')
        role = 'user' if sender == 'human' else 'assistant'

        content = msg.get('text', '')
        if not content and 'content' in msg:
            content_arr = msg.get('content', [])
            if isinstance(content_arr, list):
                content = ' '.join(
                    c.get('text', '') for c in content_arr
                    if isinstance(c, dict) and c.get('text')
                )

        if content and content.strip():
            messages.append({
                'role': role,
                'content': content,
                'timestamp': parse_timestamp(msg.get('created_at'))
            })
    return messages


def parse_claude_export(filepath: Path) -> Generator[dict, None, None]:
    """Parse Claude conversations.json."""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    for conv in data:
        messages = extract_claude_messages(conv)
        if not messages:
            continue

        yield {
            'id': conv.get('uuid', ''),
            'title': conv.get('name', '') or 'Untitled',
            'created_at': parse_timestamp(conv.get('created_at')),
            'updated_at': parse_timestamp(conv.get('updated_at')),
            'messages': messages,
            'source': 'claude',
            'summary': conv.get('summary', '')
        }


# =============================================================================
# CHATGPT PARSER
# =============================================================================

def traverse_chatgpt_tree(mapping: dict, node_id: str, children_map: dict,
                          visited: set = None) -> list[dict]:
    """Traverse ChatGPT's tree structure.

    `children_map` maps node_id -> list of child node_ids. It is reconstructed
    from `parent` pointers by the caller because newer ChatGPT exports (2026+)
    omit the per-node `children` array and keep only `parent`.
    """
    if visited is None:
        visited = set()

    if node_id in visited or node_id not in mapping:
        return []

    visited.add(node_id)
    node = mapping[node_id]
    messages = []

    msg_data = node.get('message')
    if msg_data:
        author = msg_data.get('author', {})
        role = author.get('role', 'unknown')

        # Skip hidden system messages
        metadata = msg_data.get('metadata', {})
        if metadata.get('is_visually_hidden_from_conversation'):
            role = 'system_hidden'

        content_obj = msg_data.get('content', {})
        content_type = content_obj.get('content_type', '')
        content = ''

        if content_type == 'text':
            parts = content_obj.get('parts', [])
            content = '\n'.join(str(p) for p in parts if p)
            content = strip_chatgpt_citations(content)

        if content.strip() and role in ('user', 'assistant'):
            messages.append({
                'role': role,
                'content': content,
                'timestamp': parse_timestamp(msg_data.get('create_time')),
                'model': metadata.get('model_slug', '')
            })

    # Descend into children (adjacency reconstructed from parent pointers)
    for child_id in children_map.get(node_id, []):
        messages.extend(traverse_chatgpt_tree(mapping, child_id, children_map, visited))

    return messages


def parse_chatgpt_export(filepath: Path) -> Generator[dict, None, None]:
    """Parse ChatGPT conversations.json."""
    print(f"  Loading {filepath}...")
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"  Found {len(data)} conversations")

    for conv in data:
        mapping = conv.get('mapping', {})

        # Reconstruct child adjacency from parent pointers and find the root.
        # Newer ChatGPT exports (2026+) drop the 'children' array and keep only
        # 'parent'; deriving children from 'parent' works for older exports too.
        children_map: dict[str, list[str]] = {}
        root_id = None
        for node_id, node in mapping.items():
            parent = node.get('parent')
            if parent is None:
                root_id = node_id
            else:
                children_map.setdefault(parent, []).append(node_id)

        messages = []
        if root_id:
            messages = traverse_chatgpt_tree(mapping, root_id, children_map)

        if not messages:
            continue

        yield {
            'id': conv.get('id', conv.get('conversation_id', '')),
            'title': conv.get('title', '') or 'Untitled',
            'created_at': parse_timestamp(conv.get('create_time')),
            'updated_at': parse_timestamp(conv.get('update_time')),
            'messages': messages,
            'source': 'chatgpt',
            'gizmo_id': conv.get('gizmo_id'),
        }


# =============================================================================
# MARKDOWN GENERATOR
# =============================================================================

def _escape_yaml_string(s: str) -> str:
    """Escape a string for safe YAML output (double-quoted scalar)."""
    if not s:
        return '""'
    # Escape backslashes first, then double quotes
    s = s.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{s}"'


def _detect_has_code(messages: list[dict]) -> bool:
    """Check if any message contains code fences."""
    for msg in messages:
        if '```' in msg.get('content', ''):
            return True
    return False


def _extract_model(conv: dict) -> str | None:
    """Extract model from ChatGPT metadata (first assistant message's model_slug)."""
    if conv.get('source') != 'chatgpt':
        return None
    for msg in conv.get('messages', []):
        if msg.get('role') == 'assistant' and msg.get('model'):
            return msg['model']
    return None


def conversation_to_markdown(conv: dict, include_full_content: bool = True,
                             account: str | None = None) -> str:
    """Convert a conversation to markdown with FULL content."""
    lines = []

    title = conv['title']
    created = conv['created_at']
    messages = conv['messages']

    # Calculate metadata values
    total_chars = sum(len(m['content']) for m in messages)
    has_code = _detect_has_code(messages)
    topics = extract_topics(messages)
    model = _extract_model(conv)
    source = conv['source'].lower()

    # YAML frontmatter
    lines.append("---")
    lines.append("type: conversation")
    lines.append(f"title: {_escape_yaml_string(title)}")
    if created:
        lines.append(f"date: {created.strftime('%Y-%m-%dT%H:%M:%S')}")
    else:
        lines.append("date: null")
    lines.append(f"source: {source}")
    if account:
        lines.append(f"account: {account}")
    if model:
        lines.append(f"model: {model}")
    else:
        lines.append("model: null")
    lines.append(f"messages: {len(messages)}")
    lines.append(f"characters: {total_chars}")
    lines.append(f"has_code: {'true' if has_code else 'false'}")
    if topics:
        lines.append("topics:")
        for topic in topics[:10]:
            lines.append(f"  - {topic}")
    else:
        lines.append("topics: []")
    lines.append("research_type: null")
    lines.append("---")
    lines.append("")

    # Header
    lines.append(f"# {title}")
    lines.append("")

    # Full Conversation
    lines.append("## Conversation")
    lines.append("")

    for i, msg in enumerate(messages, 1):
        role = msg['role'].upper()
        content = msg['content']

        # Message header
        timestamp = msg.get('timestamp')
        if timestamp:
            lines.append(f"### {role} ({timestamp.strftime('%H:%M')})")
        else:
            lines.append(f"### {role}")
        lines.append("")

        if include_full_content:
            # Include full content
            lines.append(content)
        else:
            # Truncate for summary view
            if len(content) > 1000:
                lines.append(content[:1000] + "\n\n*[... truncated ...]*")
            else:
                lines.append(content)

        lines.append("")
        lines.append("---")
        lines.append("")

    return '\n'.join(lines)


def extract_topics(messages: list[dict]) -> list[str]:
    """Extract simple topic keywords from messages."""
    stop_words = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
        'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
        'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
        'could', 'should', 'may', 'might', 'must', 'shall', 'can', 'need',
        'this', 'that', 'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we',
        'they', 'what', 'which', 'who', 'when', 'where', 'why', 'how', 'all',
        'each', 'every', 'both', 'few', 'more', 'most', 'other', 'some', 'such',
        'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very',
        'just', 'also', 'now', 'here', 'there', 'then', 'if', 'my', 'your',
        'me', 'like', 'want', 'think', 'know', 'make', 'get', 'go', 'see',
        'use', 'using', 'used', 'let', 'please', 'thanks', 'thank', 'help',
        'about', 'into', 'over', 'after', 'before', 'between', 'under', 'again',
    }

    # Get text from first few messages
    text = ' '.join(m['content'][:500] for m in messages[:5])

    # Extract words
    words = re.findall(r'\b[a-zA-Z]{4,15}\b', text.lower())

    # Count and filter
    word_counts = defaultdict(int)
    for word in words:
        if word not in stop_words:
            word_counts[word] += 1

    # Return top topics
    sorted_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)
    return [word for word, count in sorted_words[:10] if count >= 1]


# =============================================================================
# MAIN PROCESSOR
# =============================================================================

def process_export(
    input_path: Path,
    output_dir: Path,
    max_conversations: int = None,
    full_content: bool = True,
    incremental: bool = False,
    imported_ids: set[str] = None,
    account: str | None = None,
) -> dict:
    """Process an export file and create markdown files.

    Args:
        input_path: Path to the conversations.json file
        output_dir: Directory to write markdown files
        max_conversations: Maximum number of conversations to process
        full_content: Include full content or truncate
        incremental: Skip already imported conversations
        imported_ids: Set of conversation IDs to skip (from previous imports)
        account: Optional account name for multi-account setups

    Returns:
        dict with stats including:
        - processed: number of conversations written
        - skipped: number of conversations skipped (incremental mode)
        - total: total conversations in export
        - new_ids: list of newly imported conversation IDs
        - source: detected source ('chatgpt' or 'claude')
    """

    # Detect format
    with open(input_path, 'r', encoding='utf-8') as f:
        sample = f.read(5000)  # Increased from 2000 to catch chat_messages in Claude exports

    if '"chat_messages"' in sample:
        source = 'claude'
        parser = parse_claude_export
    else:
        source = 'chatgpt'
        parser = parse_chatgpt_export

    print(f"Detected format: {source.upper()}")

    # Initialize imported_ids set
    if imported_ids is None:
        imported_ids = set()

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    conv_dir = output_dir / 'conversations'
    conv_dir.mkdir(exist_ok=True)

    # Process conversations
    stats = {
        'total': 0,
        'processed': 0,
        'skipped': 0,
        'total_messages': 0,
        'total_chars': 0,
        'new_ids': [],
        'source': source,
    }

    summaries = []

    for conv in parser(input_path):
        stats['total'] += 1
        
        # Get conversation ID (both parsers store it in 'id' key)
        conv_id = conv.get('id', '')
        
        # Skip if already imported (incremental mode)
        if incremental and conv_id in imported_ids:
            stats['skipped'] += 1
            continue

        if max_conversations and stats['processed'] >= max_conversations:
            break

        # Generate filename
        created = conv['created_at']
        date_str = created.strftime('%Y%m%d') if created else '00000000'
        title_slug = slugify(conv['title'], 40)
        filename = f"{date_str}_{title_slug}.md"

        # Avoid duplicates
        filepath = conv_dir / filename
        counter = 1
        while filepath.exists():
            filepath = conv_dir / f"{date_str}_{title_slug}_{counter}.md"
            counter += 1

        # Generate markdown
        md_content = conversation_to_markdown(conv, include_full_content=full_content,
                                              account=account)
        filepath.write_text(md_content, encoding='utf-8')

        # Update stats
        stats['processed'] += 1
        msg_count = len(conv['messages'])
        char_count = sum(len(m['content']) for m in conv['messages'])
        stats['total_messages'] += msg_count
        stats['total_chars'] += char_count
        stats['new_ids'].append(conv_id)

        # Store summary
        summaries.append({
            'title': conv['title'],
            'date': created.isoformat() if created else None,
            'messages': msg_count,
            'chars': char_count,
            'topics': extract_topics(conv['messages']),
            'file': filepath.name
        })

        if stats['processed'] % 50 == 0:
            print(f"  Processed {stats['processed']} conversations...")

    # Create index file with timestamp (preserves history)
    index_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    index_path = output_dir / f'INDEX_{index_timestamp}.md'
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(f"# {source.upper()} Conversation Export\n\n")
        f.write(f"*Extracted: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n")
        f.write("## Statistics\n\n")
        f.write(f"- **Total Conversations**: {stats['processed']}\n")
        f.write(f"- **Total Messages**: {stats['total_messages']:,}\n")
        f.write(f"- **Total Characters**: {stats['total_chars']:,}\n")
        f.write(f"- **Avg Messages/Conv**: {stats['total_messages'] // max(stats['processed'], 1)}\n")
        if incremental and stats['skipped'] > 0:
            f.write(f"- **Skipped (existing)**: {stats['skipped']}\n")
        f.write("\n---\n\n")

        f.write("## Conversations\n\n")
        f.write("| Date | Title | Messages | Size |\n")
        f.write("|------|-------|----------|------|\n")

        # Sort by date descending
        summaries.sort(key=lambda x: x['date'] or '', reverse=True)

        for s in summaries:
            date = s['date'][:10] if s['date'] else 'Unknown'
            title = s['title'][:50]
            f.write(f"| {date} | [{title}](conversations/{s['file']}) | {s['messages']} | {s['chars']:,} chars |\n")

    print(f"\nComplete!")
    print(f"  Processed: {stats['processed']} conversations")
    if incremental:
        print(f"  Skipped: {stats['skipped']} existing conversations")
    print(f"  Messages: {stats['total_messages']:,}")
    print(f"  Characters: {stats['total_chars']:,}")
    print(f"  Output: {output_dir}")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Extract AI conversations to markdown')
    parser.add_argument('input', help='Path to conversations.json')
    parser.add_argument('--output', '-o', default='output', help='Output directory')
    parser.add_argument('--max', '-m', type=int, help='Maximum conversations to process')
    parser.add_argument('--summary', action='store_true', help='Truncate long messages')
    parser.add_argument('--incremental', '-i', action='store_true',
                       help='Skip conversations that were already imported')
    parser.add_argument('--imports-dir', default='imports',
                       help='Directory containing import logs (default: imports)')

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}")
        return 1

    output_dir = Path(args.output)
    
    # Load previously imported IDs if incremental mode
    imported_ids = set()
    if args.incremental:
        logger = ImportLogger(Path(args.imports_dir))
        
        # Detect source to get correct imported IDs
        with open(input_path, 'r', encoding='utf-8') as f:
            sample = f.read(5000)  # Increased from 2000 to catch chat_messages in Claude exports
        source = 'claude' if '"chat_messages"' in sample else 'chatgpt'
        
        imported_ids = logger.get_imported_ids(source)
        if imported_ids:
            print(f"Incremental mode: {len(imported_ids)} previously imported {source} conversations will be skipped")

    print("=" * 60)
    print("Simple Conversation Extractor")
    print("=" * 60)
    print(f"Input: {input_path}")
    print(f"Output: {output_dir}")
    if args.max:
        print(f"Max conversations: {args.max}")
    if args.incremental:
        print(f"Mode: Incremental (skip existing)")
    print()

    stats = process_export(
        input_path,
        output_dir,
        max_conversations=args.max,
        full_content=not args.summary,
        incremental=args.incremental,
        imported_ids=imported_ids
    )

    return 0


if __name__ == '__main__':
    exit(main())
