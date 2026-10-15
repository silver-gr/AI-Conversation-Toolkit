#!/usr/bin/env python3
"""
Gemini Activity Extractor
Converts Google Gemini Apps Activity Takeout to readable markdown.

Gemini exports are activity logs (not threaded conversations like ChatGPT/Claude).
Each entry is a standalone query+response pair.
"""

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timedelta
from typing import Generator
from collections import defaultdict

# Support running from different directories
sys.path.insert(0, str(Path(__file__).parent))

from parser import slugify, parse_timestamp
from import_logger import ImportLogger


_HEADINGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_PARAGRAPH_TAGS = {'p', 'div', 'section'}
_INLINE_WRAP = {'strong': '**', 'b': '**', 'em': '*', 'i': '*'}
_INLINE_FRAMES = set(_INLINE_WRAP) | {'a', 'code'}


def _one_line(text: str) -> str:
    return re.sub(r'[ \t]*\n[ \t\n]*', ' ', text).strip(' ')


class _HTMLToMarkdown(HTMLParser):
    """Stack-based HTML -> Markdown converter for Gemini safeHtmlItem html.

    Elements whose output depends on their whole content (li, blockquote,
    headings, cells, links, emphasis, code) collect text in a frame and render
    it on close. <pre> bodies are stashed as placeholders so later whitespace
    cleanup never touches them; the placeholder's line prefix (list indent or
    '> ') is applied to every code line on restore.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.frames = [{'tag': 'root', 'buf': []}]
        self.lists = []
        self.tables = []
        self.pre_blocks = []
        self.pre_depth = 0

    def _emit(self, s: str):
        self.frames[-1]['buf'].append(s)

    def _at_line_start(self) -> bool:
        for frame in reversed(self.frames):
            for s in reversed(frame['buf']):
                if s:
                    return s.endswith('\n')
            if frame['tag'] not in _INLINE_FRAMES:
                return True
        return True

    def _block_break(self):
        self._emit('\n\n')

    def _newline(self):
        if not self._at_line_start():
            self._emit('\n')

    def _push(self, tag: str, **extra):
        self.frames.append({'tag': tag, 'buf': [], **extra})

    def handle_starttag(self, tag, attrs):
        if self.pre_depth:
            if tag == 'pre':
                self.pre_depth += 1
            elif tag == 'br':
                self._emit('\n')
            return
        if tag in _PARAGRAPH_TAGS:
            self._block_break()
        elif tag == 'br':
            self._emit('\n')
        elif tag == 'hr':
            self._block_break()
            self._emit('---')
            self._block_break()
        elif tag == 'img':
            self._emit('[image]')
        elif tag == 'a':
            self._push(tag, href=dict(attrs).get('href') or '')
        elif tag == 'pre':
            self.pre_depth = 1
            self._push(tag)
        elif tag in ('ul', 'ol'):
            if self.lists:
                self._newline()
            else:
                self._block_break()
            self.lists.append({'ordered': tag == 'ol', 'n': 0})
        elif tag == 'li':
            lst = self.lists[-1] if self.lists else {'ordered': False, 'n': 0}
            lst['n'] += 1
            self._push(tag, marker=f"{lst['n']}. " if lst['ordered'] else '- ')
        elif tag == 'table':
            self.tables.append({'rows': [], 'caption': ''})
            self._push(tag)
        elif tag == 'tr':
            if self.tables:
                self.tables[-1]['rows'].append([])
        elif (tag in _HEADINGS or tag in _INLINE_FRAMES
              or tag in ('blockquote', 'td', 'th', 'caption')):
            self._push(tag)

    def handle_endtag(self, tag):
        if self.pre_depth:
            if tag != 'pre':
                return
            self.pre_depth -= 1
            if self.pre_depth:
                return
        if tag in _PARAGRAPH_TAGS:
            self._block_break()
        elif tag in ('ul', 'ol'):
            if self.lists:
                self.lists.pop()
            if self.lists:
                self._newline()
            else:
                self._block_break()
        elif any(f['tag'] == tag for f in self.frames[1:]):
            while True:
                frame = self.frames.pop()
                self._close(frame)
                if frame['tag'] == tag:
                    break

    def handle_data(self, data):
        if self.pre_depth or self.frames[-1]['tag'] == 'code':
            self._emit(data)
            return
        if data.strip():
            # Gemini writes markdown pipe tables and line breaks as raw text inside <p>.
            text = re.sub(r'[ \t\r\f]*\n[ \t\r\n\f]*', '\n', data)
            text = re.sub(r'[ \t\r\f]+', ' ', text)
            # Literal tags Gemini displayed (&lt;thinking&gt;) must not become live HTML.
            text = re.sub(r'<(?=[A-Za-z/!])', r'\\<', text)
        else:
            text = re.sub(r'[ \t\r\n\f]+', ' ', data)
        if self._at_line_start():
            text = text.lstrip(' ')
        if text:
            self._emit(text)

    def _close(self, frame: dict):
        tag = frame['tag']
        text = ''.join(frame['buf'])

        if tag == 'pre':
            if text.startswith('\n'):
                text = text[1:]
            text = text.rstrip('\n')
            fence = '```'
            while fence in text:
                fence += '`'
            self.pre_blocks.append(f'{fence}\n{text}\n{fence}')
            self._block_break()
            self._emit(f'\x00PRE{len(self.pre_blocks) - 1}\x00')
            self._block_break()
        elif tag in _HEADINGS:
            text = _one_line(text)
            if text:
                self._block_break()
                self._emit('#' * int(tag[1]) + ' ' + text)
                self._block_break()
        elif tag == 'code':
            text = text.replace('\n', ' ')
            if text.strip():
                ticks = '`'
                while ticks in text:
                    ticks += '`'
                pad = ' ' if text.startswith('`') or text.endswith('`') else ''
                self._emit(f'{ticks}{pad}{text}{pad}{ticks}')
        elif tag in _INLINE_FRAMES:
            inner = text.strip(' ')
            lead = ' ' if text.startswith(' ') else ''
            trail = ' ' if text.endswith(' ') and inner else ''
            if tag == 'a':
                href = frame['href']
                if href:
                    inner = f'[{inner or href}]({href})'
            elif inner:
                mark = _INLINE_WRAP[tag]
                inner = f'{mark}{inner}{mark}'
            self._emit(lead + inner + trail)
        elif tag == 'li':
            text = re.sub(r'\n{3,}', '\n\n', text).strip(' \n')
            marker = frame['marker']
            indent = ' ' * len(marker)
            lines = text.split('\n')
            body = [marker + lines[0]] + [indent + l if l else '' for l in lines[1:]]
            self._newline()
            self._emit('\n'.join(body) + '\n')
        elif tag == 'blockquote':
            text = re.sub(r'\n{3,}', '\n\n', text).strip(' \n')
            if text:
                self._block_break()
                self._emit('\n'.join('> ' + l if l else '>' for l in text.split('\n')))
                self._block_break()
        elif tag in ('td', 'th'):
            cell = _one_line(text).replace('|', '\\|')
            if self.tables and self.tables[-1]['rows']:
                self.tables[-1]['rows'][-1].append(cell)
            else:
                self._emit(cell + ' ')
        elif tag == 'caption':
            if self.tables:
                self.tables[-1]['caption'] = _one_line(text)
            else:
                self._emit(_one_line(text))
        elif tag == 'table':
            table = self.tables.pop()
            rows = [r for r in table['rows'] if r]
            self._block_break()
            if table['caption']:
                self._emit(table['caption'])
                self._block_break()
            if rows:
                width = max(len(r) for r in rows)
                lines = ['| ' + ' | '.join(r + [''] * (width - len(r))) + ' |' for r in rows]
                lines.insert(1, '|' + ' --- |' * width)
                self._emit('\n'.join(lines))
                self._block_break()


def strip_html(html_content: str) -> str:
    """Convert HTML to markdown."""
    if not html_content:
        return ''

    converter = _HTMLToMarkdown()
    converter.feed(html_content)
    converter.close()
    while len(converter.frames) > 1:
        converter._close(converter.frames.pop())

    text = ''.join(converter.frames[0]['buf'])
    text = '\n'.join(line.rstrip(' \t') for line in text.split('\n'))
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    def restore_pre(m):
        prefix, marker = m.group(1), m.group(2) or ''
        lines = converter.pre_blocks[int(m.group(3))].split('\n')
        # A fence opening a list item keeps the item's continuation indent on later lines.
        cont = prefix + ' ' * len(marker)
        return '\n'.join([prefix + marker + lines[0]]
                         + [cont + l if l else cont.rstrip() for l in lines[1:]])

    text = re.sub(r'^([ >]*)((?:[-*+]|\d+\.) )?\x00PRE(\d+)\x00$', restore_pre, text, flags=re.M)
    text = re.sub(r'\x00PRE(\d+)\x00', lambda m: converter.pre_blocks[int(m.group(1))], text)
    return text


def extract_query_from_title(title: str) -> str:
    """Extract user query from activity title."""
    # Greek: "Υποβλήθηκε το ερώτημα <query>"
    # English equivalent might be: "Submitted query <query>"
    prefix_patterns = [
        'Υποβλήθηκε το ερώτημα ',  # Greek
        'Submitted query ',         # English
        'Query submitted ',
        'Prompted ',                # Current English format (2026+)
        'Imported prompt ',         # Bulk-imported prompts from other platforms
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
    generated_images = 0

    # Check for query submission
    if ('Υποβλήθηκε το ερώτημα' in title
            or 'Submitted query' in title.lower()
            or title.startswith('Prompted ')
            or title.startswith('Imported prompt ')):
        activity_type = 'query'
        if title.startswith('Imported prompt '):
            activity_type = 'imported_query'
        user_query = extract_query_from_title(title)

        # Extract response from every safeHtmlItem (Deep Research reports
        # and plans arrive as separate items)
        parts = []
        for item in entry.get('safeHtmlItem') or []:
            text = strip_html(item.get('html', ''))
            if text and text not in parts:
                parts.append(text)
        response = '\n\n---\n\n'.join(parts)

        # Check for attachments info in subtitles
        subtitles = entry.get('subtitles', [])
        for sub in subtitles:
            name = sub.get('name', '')
            if ('συνημμένα αρχεία' in name or 'attached files' in name.lower()
                    or re.match(r'attached \d+ files?\.', name, re.IGNORECASE)):
                attachments.append(name)
            m = re.match(
                r'(\d+) (?:generated images?|δημιουργημένη εικόνα|δημιουργημένες εικόνες)\.',
                name, re.IGNORECASE)
            if m:
                generated_images += int(m.group(1))

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
        'attached_files': entry.get('attachedFiles') or [],
        'generated_images': generated_images,
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
                'title': activity['query'].replace('\n', ' ')[:80].strip() if activity['query'] else 'Gemini Session',
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
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
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
    lines.append(f"id: {_escape_yaml_string(conv['id'])}")
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
        files = [
            f for f in activity.get('attached_files') or []
            if not f.startswith('watermarked_img_')
        ]
        if activity['query'] or files or activity['attachments'] or activity['has_image']:
            timestamp = activity['timestamp']
            time_str = f" ({timestamp.strftime('%H:%M')})" if timestamp else ""
            lines.append(f"### USER{time_str}")
            lines.append("")
            if activity['query']:
                lines.append(activity['query'])
                lines.append("")

            if files:
                lines.append(f"*Attachments: {', '.join(files)}*")
                lines.append("")
            elif activity['attachments']:
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
        elif activity['response'] or activity.get('generated_images'):
            lines.append("### GEMINI")
            lines.append("")
            if activity['response']:
                lines.append(activity['response'])
            n_images = activity.get('generated_images', 0)
            if n_images:
                if activity['response']:
                    lines.append("")
                lines.append(f"*[{n_images} generated image{'s' if n_images != 1 else ''}]*")

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
    skip_imported: bool = False,
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
        skip_imported: Exclude "Imported prompt" entries (bulk-imported from other platforms)

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

    # Filter out bulk-imported prompts
    if skip_imported:
        before = len(activities)
        activities = [a for a in activities if a['type'] != 'imported_query']
        skipped_imported = before - len(activities)
        if skipped_imported:
            print(f"  Skipped {skipped_imported} imported prompts (--skip-imported)")

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
        # Dedup keys: the session-boundary id (back-compat) PLUS every
        # activity's stable timestamp id. Session grouping can shift between
        # exports, so treat a session as already imported if its boundary id
        # was seen OR every one of its activities was already imported. The
        # activity-level test is grouping-independent (activity times never
        # move), so it survives boundary drift that the boundary id cannot.
        conv_id = conv.get('id', '')
        activity_ids = [
            a['timestamp'].isoformat()
            for a in conv['activities'] if a.get('timestamp')
        ]

        # Skip if already imported (incremental mode)
        already_imported = conv_id in imported_ids or (
            bool(activity_ids) and all(a in imported_ids for a in activity_ids)
        )
        if incremental and already_imported:
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
        # Record activity-level ids (grouping-independent) so future imports
        # can dedup even if session boundaries shift. The boundary id is one
        # of these, so session-level matching keeps working too.
        if activity_ids:
            stats['new_ids'].extend(activity_ids)
        else:
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
            title = s['title'].replace('\n', ' ').replace('|', '-')[:50]
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
    parser.add_argument('--skip-imported', action='store_true',
                       help='Exclude bulk-imported prompts from other platforms')
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
        imported_ids=imported_ids,
        skip_imported=args.skip_imported,
    )

    return 0


if __name__ == '__main__':
    exit(main())
