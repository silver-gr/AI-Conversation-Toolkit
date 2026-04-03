#!/usr/bin/env python3
"""
Gemini Activity Extractor
Converts Google Gemini Apps Activity Takeout to readable markdown.

Gemini exports are activity logs (not threaded conversations like ChatGPT/Claude).
Each entry is a standalone query+response pair.
"""

import json
import re
import html
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Generator
from collections import defaultdict

# Support running from different directories
sys.path.insert(0, str(Path(__file__).parent))

from parser import slugify, parse_timestamp
from import_logger import ImportLogger


def strip_html(html_content: str) -> str:
    """Convert HTML to plain text/markdown."""
    if not html_content:
        return ''

    # Decode HTML entities
    text = html.unescape(html_content)

    # Convert common HTML to markdown
    text = re.sub(r'<h1[^>]*>(.*?)</h1>', r'# \1\n', text, flags=re.DOTALL)
    text = re.sub(r'<h2[^>]*>(.*?)</h2>', r'## \1\n', text, flags=re.DOTALL)
    text = re.sub(r'<h3[^>]*>(.*?)</h3>', r'### \1\n', text, flags=re.DOTALL)
    text = re.sub(r'<h4[^>]*>(.*?)</h4>', r'#### \1\n', text, flags=re.DOTALL)
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1\n', text, flags=re.DOTALL)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)

    # Remove remaining tags
    text = re.sub(r'<[^>]+>', '', text)

    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


def extract_query_from_title(title: str) -> str:
    """Extract user query from activity title."""
    # Greek: "Υποβλήθηκε το ερώτημα <query>"
    # English equivalent might be: "Submitted query <query>"
    prefix_patterns = [
        'Υποβλήθηκε το ερώτημα ',  # Greek
        'Submitted query ',         # English
        'Query submitted ',
    ]

    for prefix in prefix_patterns:
        if title.startswith(prefix):
            return title[len(prefix):].strip()

    return title


def extract_canvas_title(title: str) -> str:
    """Extract canvas title from activity title."""
    # Greek: "Δημιουργήθηκε Gemini Canvas με τίτλο <title>"
    prefix_patterns = [
        'Δημιουργήθηκε Gemini Canvas με τίτλο ',  # Greek
        'Created Gemini Canvas titled ',          # English
    ]

    for prefix in prefix_patterns:
        if title.startswith(prefix):
            return title[len(prefix):].strip()

    return title


def parse_activity(entry: dict) -> dict | None:
    """Parse a single activity entry."""
    title = entry.get('title', '')
    timestamp = parse_timestamp(entry.get('time'))

    # Determine activity type and extract content
    activity_type = None
    user_query = ''
    response = ''
    canvas_content = ''
    attachments = []

    # Check for query submission
    if 'Υποβλήθηκε το ερώτημα' in title or 'Submitted query' in title.lower():
        activity_type = 'query'
        user_query = extract_query_from_title(title)

        # Extract response from safeHtmlItem
        html_items = entry.get('safeHtmlItem', [])
        if html_items:
            response = strip_html(html_items[0].get('html', ''))

        # Check for attachments info in subtitles
        subtitles = entry.get('subtitles', [])
        for sub in subtitles:
            name = sub.get('name', '')
            if 'συνημμένα αρχεία' in name or 'attached files' in name.lower():
                attachments.append(name)

    # Check for Canvas creation
    elif 'Δημιουργήθηκε Gemini Canvas' in title or 'Created Gemini Canvas' in title:
        activity_type = 'canvas'
        user_query = extract_canvas_title(title)

        # Canvas content is in subtitles
        subtitles = entry.get('subtitles', [])
        if subtitles:
            canvas_content = subtitles[0].get('name', '')

    if not activity_type:
        return None

    return {
        'type': activity_type,
        'timestamp': timestamp,
        'query': user_query,
        'response': response,
        'canvas_content': canvas_content,
        'attachments': attachments,
        'has_image': bool(entry.get('imageFile')),
        'attached_files': entry.get('attachedFiles', [])
    }


def group_into_conversations(
    activities: list[dict],
    session_gap_minutes: int = 30
) -> list[dict]:
    """
    Group activities into conversation sessions.
    Activities within `session_gap_minutes` of each other are grouped together.
    """
    if not activities:
        return []

    # Sort by timestamp
    sorted_activities = sorted(
        [a for a in activities if a['timestamp']],
        key=lambda x: x['timestamp'],
        reverse=True  # Most recent first
    )

    conversations = []
    current_conv = None
    last_timestamp = None
    gap = timedelta(minutes=session_gap_minutes)

    for activity in sorted_activities:
        ts = activity['timestamp']

        # Start new conversation if gap is too large
        if last_timestamp is None or (last_timestamp - ts) > gap:
            if current_conv:
                conversations.append(current_conv)

            # Create new conversation
            current_conv = {
                'id': ts.isoformat() if ts else 'unknown',
                'title': activity['query'][:80] if activity['query'] else 'Gemini Session',
                'created_at': ts,
                'updated_at': ts,
                'activities': [],
                'source': 'gemini'
            }

        current_conv['activities'].append(activity)
        current_conv['updated_at'] = ts
        last_timestamp = ts

    if current_conv:
        conversations.append(current_conv)

    # Reverse activities within each conversation to be chronological
    for conv in conversations:
        conv['activities'] = list(reversed(conv['activities']))

    return conversations


def _escape_yaml_string(s: str) -> str:
    """Escape a string for safe YAML output (double-quoted scalar)."""
    if not s:
        return '""'
    s = s.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{s}"'


def _detect_has_code_gemini(activities: list[dict]) -> bool:
    """Check if any activity contains code fences."""
    for act in activities:
        if '```' in act.get('query', '') or '```' in act.get('response', '') or '```' in act.get('canvas_content', ''):
            return True
    return False


def _extract_topics_gemini(activities: list[dict]) -> list[str]:
    """Extract simple topic keywords from Gemini activities."""
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

    # Get text from first few activities
    text = ' '.join(
        (a['query'][:500] + ' ' + a['response'][:500])
        for a in activities[:5]
    )

    words = re.findall(r'\b[a-zA-Z]{4,15}\b', text.lower())

    word_counts = defaultdict(int)
    for word in words:
        if word not in stop_words:
            word_counts[word] += 1

    sorted_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)
    return [word for word, count in sorted_words[:10] if count >= 1]


def activity_to_markdown(conv: dict, account: str | None = None) -> str:
    """Convert a conversation/activity group to markdown."""
    lines = []

    title = conv['title']
    created = conv['created_at']
    activities = conv['activities']

    # Calculate metadata values
    total_chars = sum(
        len(a['query']) + len(a['response']) + len(a['canvas_content'])
        for a in activities
    )
    has_code = _detect_has_code_gemini(activities)
    topics = _extract_topics_gemini(activities)

    # YAML frontmatter
    lines.append("---")
    lines.append("type: conversation")
    lines.append(f"title: {_escape_yaml_string(title)}")
    if created:
        lines.append(f"date: {created.strftime('%Y-%m-%dT%H:%M:%S')}")
    else:
        lines.append("date: null")
    lines.append("source: gemini")
    if account:
        lines.append(f"account: {account}")
    lines.append("model: null")
    lines.append(f"messages: {len(activities)}")
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

    # Content
    lines.append("## Conversation")
    lines.append("")

    for i, activity in enumerate(activities, 1):
        # User query
        if activity['query']:
            timestamp = activity['timestamp']
            time_str = f" ({timestamp.strftime('%H:%M')})" if timestamp else ""
            lines.append(f"### USER{time_str}")
            lines.append("")
            lines.append(activity['query'])
            lines.append("")

            if activity['attachments']:
                lines.append(f"*Attachments: {', '.join(activity['attachments'])}*")
                lines.append("")

            if activity['has_image']:
                lines.append("*[Image attached]*")
                lines.append("")

        lines.append("---")
        lines.append("")

        # Response or Canvas content
        if activity['type'] == 'canvas' and activity['canvas_content']:
            lines.append("### GEMINI (Canvas)")
            lines.append("")
            lines.append(activity['canvas_content'])
        elif activity['response']:
            lines.append("### GEMINI")
            lines.append("")
            lines.append(activity['response'])

        lines.append("")
        lines.append("---")
        lines.append("")

    return '\n'.join(lines)


def parse_gemini_export(filepath: Path) -> Generator[dict, None, None]:
    """Parse Gemini activity export."""
    print(f"  Loading {filepath}...")

    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"  Found {len(data)} activity entries")

    # Parse all activities
    activities = []
    for entry in data:
        parsed = parse_activity(entry)
        if parsed:
            activities.append(parsed)

    print(f"  Parsed {len(activities)} valid activities")

    # Group into conversations
    conversations = group_into_conversations(activities)
    print(f"  Grouped into {len(conversations)} conversation sessions")

    for conv in conversations:
        yield conv


def process_gemini_export(
    input_path: Path,
    output_dir: Path,
    max_conversations: int = None,
    no_grouping: bool = False,
    incremental: bool = False,
    imported_ids: set[str] = None,
    account: str | None = None,
) -> dict:
    """Process Gemini export and create markdown files.

    Args:
        input_path: Path to the Gemini activity JSON file
        output_dir: Directory to write markdown files
        max_conversations: Maximum number of conversations to process
        no_grouping: Keep each activity separate (no session grouping)
        incremental: Skip already imported conversations
        imported_ids: Set of conversation IDs to skip (from previous imports)
        account: Optional account name for multi-account setups

    Returns:
        dict with stats including:
        - processed: number of conversations written
        - skipped: number of conversations skipped (incremental mode)
        - total: total conversations
        - new_ids: list of newly imported conversation IDs
        - source: 'gemini'
    """

    print(f"Processing Gemini export: {input_path}")

    # Initialize imported_ids set
    if imported_ids is None:
        imported_ids = set()

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    conv_dir = output_dir / 'conversations'
    conv_dir.mkdir(exist_ok=True)

    # Load and parse
    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"  Found {len(data)} activity entries")

    # Parse activities
    activities = []
    for entry in data:
        parsed = parse_activity(entry)
        if parsed:
            activities.append(parsed)

    print(f"  Parsed {len(activities)} valid activities")

    # Group or keep individual
    if no_grouping:
        # Each activity as its own "conversation"
        conversations = []
        for act in activities:
            if act['timestamp']:
                conversations.append({
                    'id': act['timestamp'].isoformat(),
                    'title': act['query'][:80] if act['query'] else 'Gemini Activity',
                    'created_at': act['timestamp'],
                    'updated_at': act['timestamp'],
                    'activities': [act],
                    'source': 'gemini'
                })
    else:
        conversations = group_into_conversations(activities)

    print(f"  Created {len(conversations)} conversation files")

    # Process stats
    stats = {
        'total': len(conversations),
        'processed': 0,
        'skipped': 0,
        'total_activities': len(activities),
        'total_chars': 0,
        'new_ids': [],
        'source': 'gemini',
    }

    summaries = []

    for conv in conversations:
        # Get conversation ID (timestamp-based)
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
        md_content = activity_to_markdown(conv, account=account)
        filepath.write_text(md_content, encoding='utf-8')

        # Calculate stats
        char_count = sum(
            len(a['query']) + len(a['response']) + len(a['canvas_content'])
            for a in conv['activities']
        )
        stats['processed'] += 1
        stats['total_chars'] += char_count
        stats['new_ids'].append(conv_id)

        summaries.append({
            'title': conv['title'],
            'date': created.isoformat() if created else None,
            'activities': len(conv['activities']),
            'chars': char_count,
            'file': filepath.name,
            'has_canvas': any(a['type'] == 'canvas' for a in conv['activities'])
        })

        if stats['processed'] % 50 == 0:
            print(f"  Processed {stats['processed']} conversations...")

    # Create index file with timestamp (preserves history)
    index_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    index_path = output_dir / f'INDEX_{index_timestamp}.md'
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write("# GEMINI Conversation Export\n\n")
        f.write(f"*Extracted: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n")
        f.write("## Statistics\n\n")
        f.write(f"- **Total Activities**: {stats['total_activities']}\n")
        f.write(f"- **Conversation Sessions**: {stats['processed']}\n")
        f.write(f"- **Total Characters**: {stats['total_chars']:,}\n")
        f.write(f"- **Canvas Creations**: {sum(1 for s in summaries if s['has_canvas'])}\n")
        if incremental and stats['skipped'] > 0:
            f.write(f"- **Skipped (existing)**: {stats['skipped']}\n")
        f.write("\n---\n\n")

        f.write("## Conversations\n\n")
        f.write("| Date | Title | Activities | Size | Canvas |\n")
        f.write("|------|-------|------------|------|--------|\n")

        # Sort by date descending
        summaries.sort(key=lambda x: x['date'] or '', reverse=True)

        for s in summaries:
            date = s['date'][:10] if s['date'] else 'Unknown'
            title = s['title'][:50]
            canvas = '' if s['has_canvas'] else ''
            f.write(f"| {date} | [{title}](conversations/{s['file']}) | {s['activities']} | {s['chars']:,} | {canvas} |\n")

    print(f"\nComplete!")
    print(f"  Conversation sessions: {stats['processed']}")
    if incremental:
        print(f"  Skipped: {stats['skipped']} existing conversations")
    print(f"  Total activities: {stats['total_activities']}")
    print(f"  Total characters: {stats['total_chars']:,}")
    print(f"  Output: {output_dir}")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Extract Gemini activity to markdown'
    )
    parser.add_argument('input', help='Path to Gemini activity JSON file')
    parser.add_argument('--output', '-o', default='output/gemini',
                       help='Output directory')
    parser.add_argument('--max', '-m', type=int,
                       help='Maximum conversations to process')
    parser.add_argument('--no-grouping', action='store_true',
                       help='Keep each activity separate (no session grouping)')
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
        imported_ids = logger.get_imported_ids('gemini')
        if imported_ids:
            print(f"Incremental mode: {len(imported_ids)} previously imported gemini conversations will be skipped")

    print("=" * 60)
    print("Gemini Activity Extractor")
    print("=" * 60)
    print(f"Input: {input_path}")
    print(f"Output: {output_dir}")
    if args.max:
        print(f"Max conversations: {args.max}")
    if args.incremental:
        print(f"Mode: Incremental (skip existing)")
    print()

    stats = process_gemini_export(
        input_path,
        output_dir,
        max_conversations=args.max,
        no_grouping=args.no_grouping,
        incremental=args.incremental,
        imported_ids=imported_ids
    )

    return 0


if __name__ == '__main__':
    exit(main())
