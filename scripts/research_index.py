#!/usr/bin/env python3
"""
Create an index of Research conversations from ChatGPT, Claude, and Gemini exports.
Detects Deep Research (ChatGPT, Gemini) and web_search tool (Claude) usage
using structural markers only — no semantic/keyword matching.
"""

import re
from pathlib import Path
from datetime import datetime


# Minimum character count for a file to be considered substantial research
MIN_RESEARCH_SIZE = 5000
# Claude web_search results can be shorter
MIN_CLAUDE_SIZE = 2000


# Patterns that indicate a conversation should be EXCLUDED from research index
EXCLUSION_PATTERNS = [
    # Cancelled/failed tasks
    r'task cancelled by user',
    r'η εργασία ακυρώθηκε',
    r'research cancelled',

    # Reminder/task management (not research)
    r'google tasks',
    r'θύμισε μου',
    r'remind me tomorrow',
    r'υπενθύμιση',
    r'θα σας υπενθυμίσω',

    # Generic list requests (not deep research)
    r'generate a comprehensive list of subreddits',
    r'list of subreddits',

    # Meta-questions about research tools (not actual research)
    r'how long would it take for me to manually do the research',
    r'how many sources did you check',
]

# Titles/filenames that are non-research content
EXCLUDED_TITLES = [
    'οδηγίες',  # Instructions/reminders
    'new-chat',  # Generic untitled chats that got misclassified
]


def is_excluded_content(filepath: Path) -> bool:
    """Check if file content matches exclusion patterns."""
    content = filepath.read_text(encoding='utf-8').lower()

    for pattern in EXCLUSION_PATTERNS:
        if re.search(pattern, content):
            return True

    # Check filename for excluded titles
    filename = filepath.stem.lower()
    for title in EXCLUDED_TITLES:
        if title in filename:
            return True

    return False


def _parse_yaml_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from markdown content."""
    fm = {}
    if not content.startswith('---'):
        return fm
    end = content.find('\n---', 3)
    if end == -1:
        return fm
    block = content[4:end]
    for line in block.split('\n'):
        if ':' in line and not line.startswith(' '):
            key, _, val = line.partition(':')
            val = val.strip().strip('"')
            fm[key.strip()] = val
    return fm


def extract_metadata_from_md(filepath: Path, source_hint: str = '') -> dict:
    """Extract metadata and first user query from markdown file."""
    content = filepath.read_text(encoding='utf-8')

    metadata = {
        'file': filepath.name,
        'path': str(filepath),
        'title': '',
        'date': '',
        'source': source_hint.upper() if source_hint else '',
        'messages': 0,
        'activities': 0,  # For Gemini
        'chars': 0,
        'first_query': '',
        'detection_method': '',
    }

    # Try YAML frontmatter first
    fm = _parse_yaml_frontmatter(content)
    if fm:
        if fm.get('title'):
            metadata['title'] = fm['title']
        date_val = fm.get('date', '')
        if date_val:
            metadata['date'] = date_val[:10]
        source_val = fm.get('source', '')
        if source_val:
            metadata['source'] = source_val.upper()
        try:
            metadata['messages'] = int(fm.get('messages', 0))
        except (ValueError, TypeError):
            pass
        try:
            metadata['chars'] = int(fm.get('characters', 0))
        except (ValueError, TypeError):
            pass

    # Fallback to inline metadata patterns
    if not metadata['title']:
        title_match = re.search(r'^# (.+)$', content, re.MULTILINE)
        if title_match:
            metadata['title'] = title_match.group(1)

    if not metadata['date']:
        date_match = re.search(r'\*\*Date\*\*: (\d{4}-\d{2}-\d{2})', content)
        if date_match:
            metadata['date'] = date_match.group(1)

    if metadata['source'] == (source_hint.upper() if source_hint else ''):
        source_match = re.search(r'\*\*Source\*\*: (\w+)', content)
        if source_match:
            metadata['source'] = source_match.group(1)

    if not metadata['messages']:
        msg_match = re.search(r'\*\*Messages\*\*: (\d+)', content)
        if msg_match:
            metadata['messages'] = int(msg_match.group(1))

    if not metadata['chars']:
        char_match = re.search(r'\*\*Total Characters\*\*: ([\d,]+)', content)
        if char_match:
            metadata['chars'] = int(char_match.group(1).replace(',', ''))

    # Extract activities count (Gemini)
    act_match = re.search(r'\*\*Activities\*\*: (\d+)', content)
    if act_match:
        metadata['activities'] = int(act_match.group(1))

    # Extract first user message
    user_match = re.search(r'### USER.*?\n\n(.+?)(?=\n\n---|\n\n###)', content, re.DOTALL)
    if user_match:
        first_query = user_match.group(1).strip()
        metadata['first_query'] = first_query[:300] + '...' if len(first_query) > 300 else first_query

    return metadata


# ---------------------------------------------------------------------------
# Per-source detection functions
# ---------------------------------------------------------------------------

def detect_chatgpt_deep_research(content: str, fm: dict) -> str | None:
    """Detect ChatGPT Deep Research using structural markers.

    Tier 0: research_type: chatgpt_deep_research in YAML frontmatter (set by extractor)
    Tier 1: model: research in YAML frontmatter
    Tier 2: PUA Unicode chars + source/reference headers in assistant text
    Tier 3: Source list pattern (em-dash + italic) in last assistant response
    Tier 4: Very long single assistant response (>30K chars) — Browse can't produce this
    """
    if fm.get('research_type') == 'chatgpt_deep_research':
        return 'deep_research_report'

    # Tier 1: model field in frontmatter
    if fm.get('model', '').lower() == 'research':
        return 'model:research'

    # Extract assistant blocks (used by multiple tiers)
    parts = re.split(r'### ASSISTANT', content)
    assistant_blocks = parts[1:] if len(parts) > 1 else []
    longest_asst = max((len(b) for b in assistant_blocks), default=0)

    # Tier 2: PUA Unicode + source headers in assistant text
    has_pua = bool(re.search(r'[\ue200\ue202]', content))
    if has_pua:
        assistant_text = '\n'.join(assistant_blocks)
        has_source_header = bool(re.search(
            r'(?i)^##+ \**(?:References|Sources Consulted|Key sources|Appendix:?\s*Sources)',
            assistant_text, re.MULTILINE
        ))
        if has_source_header:
            return 'source_header'

    # Tier 3: Source list pattern in last assistant response
    if assistant_blocks:
        tail = assistant_blocks[-1][-4000:]
        # Pattern: bullet + source name + em-dash (en-dash or em-dash) + italicized description
        source_lines = re.findall(r'\n[-*]\s+.+?\s+[–—]\s+\*.+?\*', tail)
        if len(source_lines) >= 3:
            return 'source_list_pattern'

    # Tier 4: Very long single assistant response — Browse maxes out around 30K
    if longest_asst > 30000:
        return 'long_response'

    return None


def detect_claude_research(content: str, fm: dict) -> str | None:
    """Detect Claude Research tool usage.

    New-format files (frontmatter has 'id'): research_type set by the extractor is authoritative.
    Legacy files:
    Tier 1: Literal 'web_search' tool name in content
    Tier 2: 'research task' phrase — Claude describes using its research feature
    """
    research_type = fm.get('research_type', '')
    if research_type == 'claude_research':
        return 'claude_research_tool'
    if research_type == 'claude_web_search':
        return 'web_search_tool'
    # New renders contain '*[tools: web_search x1]*' lines, so body heuristics no longer apply
    if 'id' in fm:
        return None
    if len(content) < MIN_CLAUDE_SIZE:
        return None
    if 'web_search' in content:
        return 'web_search_tool'
    if 'research task' in content.lower():
        return 'research_task'
    return None


def detect_gemini_deep_research(content: str) -> str | None:
    """Detect Gemini Deep Research using structural markers.

    Tier 1: "i've completed your research" or Greek equivalent
    Tier 2: "here's a research plan" + chars > 10000
    Tier 3: Greek "έναρξη έρευνας" + chars > 8000
    """
    content_lower = content.lower()
    chars = len(content)

    # Tier 1: Completed research phrase (definitive)
    if ("i've completed your research" in content_lower
            or "i\u2019ve completed your research" in content_lower
            or "ολοκλήρωσα την έρευνά" in content_lower):
        return 'completed_your_research'

    # Tier 2: Research plan phrase + substantial content
    if (("here's a research plan" in content_lower
            or "here\u2019s a research plan" in content_lower)
            and chars > 10000):
        return 'research_plan'

    # Tier 3: Greek research start + substantial content
    if "έναρξη έρευνας" in content_lower and chars > 8000:
        return 'greek_research_start'

    return None


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan_source(source_dir: Path, source_name: str, detect_fn) -> list[dict]:
    """Scan a source directory for research conversations using the given detector."""
    conv_dir = source_dir / 'conversations'
    if not conv_dir.exists():
        return []

    min_size = MIN_CLAUDE_SIZE if source_name == 'claude' else MIN_RESEARCH_SIZE
    results = []

    for md_file in conv_dir.glob('*.md'):
        content = md_file.read_text(encoding='utf-8')
        fm = _parse_yaml_frontmatter(content)

        # Call the source-specific detection function
        if source_name in ('chatgpt', 'claude'):
            detection = detect_fn(content, fm)
        else:
            detection = detect_fn(content)

        if detection is None:
            continue

        # Check exclusion patterns (skip for large files where meta-questions
        # are incidental, not the primary content)
        char_count = len(content)
        try:
            char_count = int(fm.get('characters', char_count))
        except (ValueError, TypeError):
            pass
        if char_count < 20000 and is_excluded_content(md_file):
            continue

        # Size filter (except Claude which has its own threshold in detect_fn)
        if source_name != 'claude':
            has_file_link = '{{file:' in content or 'artifacts created' in content
            if char_count < min_size and not has_file_link:
                continue

        metadata = extract_metadata_from_md(md_file, source_name)
        metadata['detection_method'] = detection
        results.append(metadata)

    return results


def create_research_index(
    claude_dir: Path,
    chatgpt_dir: Path,
    gemini_dir: Path,
    output_path: Path
):
    """Create a unified research index markdown file."""

    print("Scanning for research conversations...")

    # Find research conversations from all sources
    chatgpt_research = scan_source(chatgpt_dir, 'chatgpt', detect_chatgpt_deep_research)
    claude_research = scan_source(claude_dir, 'claude', detect_claude_research)
    gemini_research = scan_source(gemini_dir, 'gemini', detect_gemini_deep_research)

    print(f"  ChatGPT: {len(chatgpt_research)} research conversations")
    print(f"  Claude: {len(claude_research)} research conversations")
    print(f"  Gemini: {len(gemini_research)} research conversations")

    # Sort by date (newest first)
    chatgpt_research.sort(key=lambda x: x['date'], reverse=True)
    claude_research.sort(key=lambda x: x['date'], reverse=True)
    gemini_research.sort(key=lambda x: x['date'], reverse=True)

    total_count = len(chatgpt_research) + len(claude_research) + len(gemini_research)

    # Generate markdown
    lines = []
    lines.append("# Research Conversations Index")
    lines.append("")
    lines.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Total Research Conversations**: {total_count}")
    lines.append(f"- **ChatGPT Deep Research**: {len(chatgpt_research)}")
    lines.append(f"- **Claude Research**: {len(claude_research)}")
    lines.append(f"- **Gemini Deep Research**: {len(gemini_research)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ChatGPT Deep Research section
    lines.append("## ChatGPT Deep Research")
    lines.append("")
    if chatgpt_research:
        lines.append("| Date | Title | Detection | Messages | Size |")
        lines.append("|------|-------|-----------|----------|------|")
        for conv in chatgpt_research:
            title = conv['title'][:50] + '...' if len(conv['title']) > 50 else conv['title']
            rel_path = f"chatgpt-full/conversations/{conv['file']}"
            lines.append(f"| {conv['date']} | [[{rel_path}\\|{title}]] | {conv['detection_method']} | {conv['messages']} | {conv['chars']:,} |")
        lines.append("")
    else:
        lines.append("*No ChatGPT Deep Research conversations found.*")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Claude Research section
    lines.append("## Claude Research")
    lines.append("")
    if claude_research:
        lines.append("| Date | Title | Detection | Messages | Size |")
        lines.append("|------|-------|-----------|----------|------|")
        for conv in claude_research:
            title = conv['title'][:50] + '...' if len(conv['title']) > 50 else conv['title']
            rel_path = f"claude-full/conversations/{conv['file']}"
            lines.append(f"| {conv['date']} | [[{rel_path}\\|{title}]] | {conv['detection_method']} | {conv['messages']} | {conv['chars']:,} |")
        lines.append("")
    else:
        lines.append("*No Claude Research conversations found.*")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Gemini Deep Research section
    lines.append("## Gemini Deep Research")
    lines.append("")
    if gemini_research:
        lines.append("| Date | Title | Detection | Activities | Size |")
        lines.append("|------|-------|-----------|------------|------|")
        for conv in gemini_research:
            title = conv['title'][:50] + '...' if len(conv['title']) > 50 else conv['title']
            rel_path = f"gemini/conversations/{conv['file']}"
            # Use activities count for Gemini, fallback to messages
            count = conv['activities'] if conv['activities'] > 0 else conv['messages']
            lines.append(f"| {conv['date']} | [[{rel_path}\\|{title}]] | {conv['detection_method']} | {count} | {conv['chars']:,} |")
        lines.append("")
    else:
        lines.append("*No Gemini Deep Research conversations found.*")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Discovery Methods documentation
    lines.append("## Discovery Methods")
    lines.append("")
    lines.append("Structural markers used to detect research conversations per platform:")
    lines.append("")
    lines.append("### ChatGPT")
    lines.append("")
    lines.append("| Method | Signal | Confidence |")
    lines.append("|--------|--------|------------|")
    lines.append("| `deep_research_report` | `research_type: chatgpt_deep_research` in YAML frontmatter (set by extractor) | Definitive |")
    lines.append("| `model:research` | `model: research` in YAML frontmatter | Definitive |")
    lines.append("| `source_header` | PUA Unicode chars + `## References`/`### Sources Consulted` header in assistant text | High |")
    lines.append("| `source_list_pattern` | 3+ source citation lines with em-dash + italics in last assistant response | High |")
    lines.append("| `long_response` | Single assistant response > 30,000 chars (exceeds Browse output limits) | High |")
    lines.append("")
    lines.append("### Claude")
    lines.append("")
    lines.append("| Method | Signal | Confidence |")
    lines.append("|--------|--------|------------|")
    lines.append("| `claude_research_tool` | `research_type: claude_research` in YAML frontmatter (set by extractor) | Definitive |")
    lines.append("| `web_search_tool` | `research_type: claude_web_search` in YAML frontmatter; legacy files (no `id`): literal string `web_search` in content | Definitive |")
    lines.append('| `research_task` | Legacy files only (no `id`): "research task" phrase in content (Claude Research feature marker) | High |')
    lines.append("")
    lines.append("### Gemini")
    lines.append("")
    lines.append("| Method | Signal | Confidence |")
    lines.append("|--------|--------|------------|")
    lines.append('| `completed_your_research` | "i\'ve completed your research" or Greek equivalent | Definitive |')
    lines.append('| `research_plan` | "here\'s a research plan" + content > 10,000 chars | High |')
    lines.append('| `greek_research_start` | "έναρξη έρευνας" (Greek) + content > 8,000 chars | High |')
    lines.append("")

    # Write output
    output_path.write_text('\n'.join(lines), encoding='utf-8')
    print(f"\nCreated: {output_path}")
    print(f"Total research conversations indexed: {total_count}")


def main():
    base_dir = Path(__file__).parent.parent

    claude_dir = base_dir / 'output' / 'claude-full'
    chatgpt_dir = base_dir / 'output' / 'chatgpt-full'
    gemini_dir = base_dir / 'output' / 'gemini'
    output_path = base_dir / 'output' / 'RESEARCH_INDEX.md'

    create_research_index(claude_dir, chatgpt_dir, gemini_dir, output_path)


if __name__ == '__main__':
    main()
