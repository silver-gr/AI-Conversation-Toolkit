#!/usr/bin/env python3
"""
Simple Conversation Extractor - No AI Analysis Required
Converts ChatGPT and Claude exports to readable markdown with FULL content.
"""

import json
import os
import re
import sys
import zipfile
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


# Private-use-area citation tokens in Deep Research reports:
# citeturn6view0turn8view1 (entity tokens carry display text, so they stay)
_PUA_CITATION_RE = re.compile('(?:cite|filecite|navlist)[^]*')


def _fenced(body: str, lang: str = '', char: str = '`') -> str:
    """Wrap body in a code fence longer than any run of the fence char inside it."""
    longest = max((len(run) for run in re.findall(re.escape(char) + '+', body)), default=0)
    fence = char * max(3, longest + 1)
    return f"{fence}{lang}\n{body}\n{fence}"


def _report_empty(empty: list[tuple[str, str]]) -> None:
    """Print one summary line for conversations skipped because they have no messages."""
    if empty:
        examples = ', '.join(f"{cid} '{title}'" for cid, title in empty[:5])
        print(f"  Skipped {len(empty)} conversations with no extractable messages (e.g. {examples})")


# =============================================================================
# CLAUDE PARSER
# =============================================================================

_CLAUDE_PLACEHOLDER_RE = re.compile(
    r'```\n(?:This block is not supported on your current device yet\.'
    r'|[^\n]*Analysis Tool[^\n]*supported on mobile\.)\n```\n*'
    r'|^[^\n]*Analysis Tool[^\n]*supported on mobile\.[^\n]*\n?',
    re.MULTILINE)


def _claude_current_branch(chat_messages: list[dict]) -> list[dict]:
    """Messages on the path from the latest leaf back to the root, in order."""
    if not any('parent_message_uuid' in m for m in chat_messages):
        return chat_messages
    by_uuid = {m.get('uuid'): m for m in chat_messages}
    parents = {m.get('parent_message_uuid') for m in chat_messages}
    leaves = [(m.get('created_at') or '', i) for i, m in enumerate(chat_messages)
              if m.get('uuid') not in parents]
    if not leaves:
        return chat_messages
    node = chat_messages[max(leaves)[1]]
    chain, seen = [], set()
    while node is not None and node.get('uuid') not in seen:
        seen.add(node.get('uuid'))
        chain.append(node)
        node = by_uuid.get(node.get('parent_message_uuid'))
    return chain[::-1]


def _claude_blocks_to_text(blocks: list, files: list[dict], tools: list[str]) -> str:
    """Render Claude content blocks; appends every tool_use name to `tools`."""
    pieces = []
    other_tools: dict[str, int] = {}
    file_names = {f.get('file_uuid'): f.get('file_name') for f in files}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind == 'text':
            if (block.get('text') or '').strip():
                pieces.append(block['text'])
        elif kind == 'tool_use':
            name = block.get('name') or ''
            inp = block.get('input') or {}
            tools.append(name)
            command = inp.get('command')
            if name == 'artifacts' and command in ('create', 'rewrite'):
                lang = inp.get('language') or (inp.get('type') or '').split('/')[-1].replace('vnd.ant.', '')
                pieces.append(f"#### Artifact: {inp.get('title') or inp.get('id')}\n\n"
                              + _fenced(inp.get('content') or '', lang))
            elif name == 'artifacts' and command == 'update':
                pieces.append(f"*[artifact {inp.get('id')} updated]*")
            elif name == 'create_file':
                pieces.append(f"#### File: {inp.get('path')}\n\n" + _fenced(inp.get('file_text') or ''))
            elif name == 'Write':
                pieces.append(f"#### File: {inp.get('file_path')}\n\n" + _fenced(inp.get('content') or ''))
            elif name == 'SendUserMessage':
                if (inp.get('message') or '').strip():
                    pieces.append(inp['message'])
            else:
                other_tools[name] = other_tools.get(name, 0) + 1
        elif kind == 'image':
            pieces.append('*[image]*')
        elif kind == 'document':
            name = block.get('file_name') or file_names.get(block.get('file_uuid')) or 'untitled'
            pieces.append(f"*[document: {name}]*")
    if other_tools:
        pieces.append('*[tools: ' + ', '.join(f"{n} x{c}" for n, c in other_tools.items()) + ']*')
    return '\n\n'.join(pieces)


def extract_claude_messages(conv: dict) -> list[dict]:
    """Extract current-branch messages from a Claude conversation.

    Each message carries 'tools': the tool_use names it called (with repeats).
    """
    messages = []
    for msg in _claude_current_branch(conv.get('chat_messages', [])):
        sender = msg.get('sender', '')
        role = 'user' if sender == 'human' else 'assistant'

        tools: list[str] = []
        blocks = msg.get('content')
        if isinstance(blocks, list) and blocks:
            content = _claude_blocks_to_text(blocks, msg.get('files') or [], tools)
        else:
            content = _CLAUDE_PLACEHOLDER_RE.sub('', msg.get('text') or '')

        pieces = [content]
        for att in msg.get('attachments') or []:
            if (att.get('extracted_content') or '').strip():
                pieces.append(f"**Attachment: {att.get('file_name') or 'pasted text'}**\n\n"
                              + _fenced(att['extracted_content'], char='~'))
        for f in msg.get('files') or []:
            if f.get('file_name'):
                pieces.append(f"*[file: {f['file_name']}]*")
        content = '\n\n'.join(p for p in pieces if p.strip())

        if content:
            messages.append({
                'role': role,
                'content': content,
                'timestamp': parse_timestamp(msg.get('created_at')),
                'tools': tools,
            })
    return messages


def parse_claude_export(filepath: Path) -> Generator[dict, None, None]:
    """Parse Claude conversations.json."""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    empty = []
    for conv in data:
        messages = extract_claude_messages(conv)
        if not messages:
            empty.append((conv.get('uuid', ''), conv.get('name', '') or 'Untitled'))
            continue

        calls = [name for m in messages for name in m['tools'] if name]
        if 'launch_extended_search_task' in calls:
            research_type = 'claude_research'
        elif calls.count('web_search') >= 3:
            research_type = 'claude_web_search'
        else:
            research_type = None

        yield {
            'id': conv.get('uuid', ''),
            'title': conv.get('name', '') or 'Untitled',
            'created_at': parse_timestamp(conv.get('created_at')),
            'updated_at': parse_timestamp(conv.get('updated_at')),
            'messages': messages,
            'source': 'claude',
            'summary': conv.get('summary', ''),
            'tools': sorted(set(calls)),
            'research_type': research_type,
        }
    _report_empty(empty)


# =============================================================================
# CHATGPT PARSER
# =============================================================================

def _chatgpt_node_message(node_id: str, node: dict) -> dict | None:
    """Convert one ChatGPT mapping node to a message dict, or None if it has no visible text."""
    msg_data = node.get('message')
    if not msg_data:
        return None

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
    elif content_type == 'multimodal_text':
        # Audio asset pointers are skipped: their words are in the audio_transcription part
        pieces = []
        for part in content_obj.get('parts', []):
            if isinstance(part, str):
                if part.strip():
                    pieces.append(part)
            elif isinstance(part, dict):
                if part.get('content_type') == 'audio_transcription':
                    if (part.get('text') or '').strip():
                        pieces.append(part['text'])
                elif part.get('content_type') == 'image_asset_pointer':
                    pieces.append('[image]')
        content = strip_chatgpt_citations('\n'.join(pieces))

    if content.strip() and role in ('user', 'assistant'):
        return {
            'id': msg_data.get('id') or node_id,
            'role': role,
            'content': content,
            'timestamp': parse_timestamp(msg_data.get('create_time')),
            'model': metadata.get('model_slug', '')
        }
    return None


def traverse_chatgpt_tree(mapping: dict, node_id: str, children_map: dict,
                          visited: set = None) -> list[dict]:
    """Traverse ChatGPT's tree structure (every branch, depth-first).

    `children_map` maps node_id -> list of child node_ids. It is reconstructed
    from `parent` pointers by the caller because newer ChatGPT exports (2026+)
    omit the per-node `children` array and keep only `parent`.
    """
    if visited is None:
        visited = set()

    if node_id in visited or node_id not in mapping:
        return []

    visited.add(node_id)
    messages = []

    msg = _chatgpt_node_message(node_id, mapping[node_id])
    if msg:
        messages.append(msg)

    # Descend into children (adjacency reconstructed from parent pointers)
    for child_id in children_map.get(node_id, []):
        messages.extend(traverse_chatgpt_tree(mapping, child_id, children_map, visited))

    return messages


def chatgpt_current_branch(mapping: dict, current_node: str) -> list[dict]:
    """Messages on the path from the root to `current_node` (the branch shown in the UI)."""
    path, seen = [], set()
    node_id = current_node
    while node_id in mapping and node_id not in seen:
        seen.add(node_id)
        path.append(node_id)
        node_id = mapping[node_id].get('parent')
    messages = []
    for node_id in reversed(path):
        msg = _chatgpt_node_message(node_id, mapping[node_id])
        if msg:
            messages.append(msg)
    return messages


def _library_local_time(entry: dict) -> datetime | None:
    """Library ISO-UTC timestamp as naive local time, matching ChatGPT message timestamps."""
    try:
        return parse_timestamp(datetime.fromisoformat(entry['created_at']).timestamp())
    except (KeyError, TypeError, ValueError):
        return None


def load_chatgpt_library(export_dir: Path) -> dict[str, list[dict]]:
    """Load Deep Research reports and assistant-generated text files from the export library.

    Reads library_files.json and the <file_id>.dat bodies from the export zip(s) in
    `export_dir`, in memory only. Returns {thread_id: [item, ...]} ordered by created_at;
    each item has 'kind' ('report' | 'file'), 'origin' (origination_message_id),
    'title', 'created_at' and the ready-to-insert assistant 'message'.
    """
    lib_path = export_dir / 'library_files.json'
    if not lib_path.exists():
        print(f"  Note: no library_files.json in {export_dir}; library reports/files not injected")
        return {}
    archives = []
    for zip_path in sorted(export_dir.glob('*.zip')):
        try:
            archives.append(zipfile.ZipFile(zip_path))
        except zipfile.BadZipFile:
            pass
    if not archives:
        print(f"  Note: no export zip in {export_dir}; library reports/files not injected")
        return {}

    with open(lib_path, 'r', encoding='utf-8') as f:
        entries = json.load(f)
    zip_index = {name: z for z in archives for name in z.namelist()}

    by_thread: dict[str, list[dict]] = {}
    missing = 0
    for entry in sorted(entries, key=lambda e: e.get('created_at') or ''):
        if entry.get('deleted_at') or entry.get('trashed_at') or not entry.get('file_id'):
            continue
        is_report = entry.get('library_artifact_type') == 'deep_research_report'
        is_text_file = (entry.get('mime_type') in ('text/markdown', 'text/plain')
                        and entry.get('origination_message_id')
                        and entry.get('origination_thread_id'))
        if not (is_report or is_text_file):
            continue
        dat_name = entry['file_id'] + '.dat'
        if dat_name not in zip_index:
            missing += 1
            continue
        raw = zip_index[dat_name].read(dat_name)
        created = _library_local_time(entry)

        if is_report:
            try:
                dat = json.loads(raw)
                report_msg = dat['widget_state']['report_message']
                parts = report_msg['content']['parts']
            except (ValueError, KeyError, TypeError):
                continue
            text = '\n'.join(p for p in parts if isinstance(p, str))
            if not text.strip():
                continue
            text = strip_chatgpt_citations(_PUA_CITATION_RE.sub('', text))
            title = dat.get('title') or entry.get('file_name') or 'Untitled'
            item = {
                'kind': 'report',
                'title': title,
                'message': {
                    'id': report_msg.get('id'),
                    'role': 'assistant',
                    'content': f"#### Deep Research report: {title}\n\n{text}",
                    'timestamp': parse_timestamp(report_msg.get('create_time')) or created,
                    'model': '',
                },
            }
            thread_id = entry.get('origination_thread_id') or entry['file_id']
        else:
            body = raw.decode('utf-8', errors='replace')
            if not body.strip():
                continue
            title = entry.get('file_name') or entry['file_id']
            item = {
                'kind': 'file',
                'digest': hashlib.sha256(raw).hexdigest(),
                'title': title,
                'message': {
                    'id': None,
                    'role': 'assistant',
                    # Tilde fence: prose deliverables must not trip has_code (backtick-only check).
                    'content': f"#### File: {title}\n\n" + _fenced(body, char='~'),
                    'timestamp': created,
                    'model': '',
                },
            }
            thread_id = entry['origination_thread_id']
        item['origin'] = entry.get('origination_message_id')
        item['created_at'] = created
        by_thread.setdefault(thread_id, []).append(item)

    for z in archives:
        z.close()
    if missing:
        print(f"  Note: {missing} library file bodies missing from the export zip; skipped")
    return by_thread


def inject_library_items(messages: list[dict], mapping: dict, items: list[dict],
                         seen_digests: set | None = None) -> list[dict]:
    """Insert library items after their origination message, else by timestamp.

    Text files are only injected when their origination message is an assistant
    message in this thread, deduplicated by body digest via `seen_digests`;
    reports are always injected.
    """
    if not items:
        return messages
    if seen_digests is None:
        seen_digests = set()
    assistant_ids = {
        (node['message'].get('id') or node_id)
        for node_id, node in mapping.items()
        if node.get('message') and node['message'].get('author', {}).get('role') == 'assistant'
    }
    emitted = {m.get('id') for m in messages if m.get('id')}
    after: dict[str, list[dict]] = defaultdict(list)
    loose = []
    for item in items:
        if item['kind'] == 'file':
            if item['origin'] not in assistant_ids or item['digest'] in seen_digests:
                continue
            seen_digests.add(item['digest'])
        if item['origin'] in emitted:
            after[item['origin']].append(item['message'])
        else:
            loose.append(item['message'])
    result = []
    for msg in messages:
        result.append(msg)
        result.extend(after.get(msg.get('id'), []))
    # Reports carry their own id (never in the mapping): place them chronologically,
    # before any follow-up turns that came after the report finished.
    for item_msg in loose:
        ts = item_msg.get('timestamp')
        pos = next((i for i, m in enumerate(result)
                    if ts and m.get('timestamp') and m['timestamp'] > ts), len(result))
        result.insert(pos, item_msg)
    return result


def parse_chatgpt_export(filepath: Path) -> Generator[dict, None, None]:
    """Parse ChatGPT conversations.json, injecting library reports/files from the same directory."""
    print(f"  Loading {filepath}...")
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print(f"  Found {len(data)} conversations")
    library = load_chatgpt_library(Path(filepath).parent)

    empty = []
    seen_ids = set()
    seen_file_digests = set()
    for conv in data:
        conv_id = conv.get('id', conv.get('conversation_id', ''))
        title = conv.get('title', '') or 'Untitled'
        seen_ids.add(conv_id)
        mapping = conv.get('mapping', {})

        current = conv.get('current_node')
        if current in mapping:
            messages = chatgpt_current_branch(mapping, current)
        else:
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

        items = library.get(conv_id, [])
        messages = inject_library_items(messages, mapping, items, seen_file_digests)

        if not messages:
            empty.append((conv_id, title))
            continue

        yield {
            'id': conv_id,
            'title': title,
            'created_at': parse_timestamp(conv.get('create_time')),
            'updated_at': parse_timestamp(conv.get('update_time')),
            'messages': messages,
            'source': 'chatgpt',
            'gizmo_id': conv.get('gizmo_id'),
            'default_model_slug': conv.get('default_model_slug'),
            'research_type': ('chatgpt_deep_research'
                              if any(i['kind'] == 'report' for i in items) else None),
        }
    _report_empty(empty)

    # Deep Research reports whose originating thread is missing from conversations.json
    for thread_id, items in library.items():
        reports = [i for i in items if i['kind'] == 'report']
        if thread_id in seen_ids or not reports:
            continue
        yield {
            'id': thread_id,
            'title': reports[0]['title'],
            'created_at': reports[0]['created_at'],
            'updated_at': reports[-1]['created_at'],
            'messages': [i['message'] for i in reports],
            'source': 'chatgpt',
            'gizmo_id': None,
            'default_model_slug': None,
            'research_type': 'chatgpt_deep_research',
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
    default = conv.get('default_model_slug')
    return default if default and default != 'auto' else None


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
    if conv.get('id'):
        lines.append(f"id: {_escape_yaml_string(conv['id'])}")
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
    lines.append(f"research_type: {conv.get('research_type') or 'null'}")
    if conv.get('tools'):
        lines.append("tools:")
        for tool in conv['tools']:
            lines.append(f"  - {tool}")
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
