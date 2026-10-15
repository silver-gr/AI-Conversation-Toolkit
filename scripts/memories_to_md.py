#!/usr/bin/env python3
"""
Phase 0: Convert Claude memories exports to organized markdown files.
Reads Claude/memories/*.json (or legacy Claude/memories.json) and Claude/reflections/*.json.
"""

import json
import os
import re
import shutil
import unicodedata
from pathlib import Path
from datetime import datetime


def slugify(text: str) -> str:
    """Convert text to a valid filename slug."""
    # Remove special characters and convert to lowercase
    slug = re.sub(r'[^\w\s-]', '', text.lower())
    # Replace spaces with hyphens
    slug = re.sub(r'[-\s]+', '-', slug).strip('-')
    return slug[:50]  # Limit length


def extract_project_name(memory_content: str) -> str:
    """Extract a project name from the memory content."""
    # Look for **Purpose & context** section and extract key terms
    lines = memory_content.split('\n')

    # Try to find identifiable project names
    project_keywords = {
        'x3lixi': 'x3lixi-platform',
        'x3lixi-os': 'x3lixi-app',
        'wordpress': 'wordpress-platform',
        'dj': 'djing-music',
        'psytrance': 'djing-music',
        'adhd': 'adhd-course',
        'meditation': 'meditation-course',
        'bdsm': 'bdsm-education',
        'journaling': 'journaling-system',
        'obsidian': 'knowledge-management',
        'cognito': 'cognito-ai-system',
        'flutter': 'flutter-development',
        'prompt': 'prompt-engineering',
        'course': 'course-development',
        'translation': 'translation-work',
        'research': 'research-methodology',
    }

    content_lower = memory_content.lower()

    for keyword, project_name in project_keywords.items():
        if keyword in content_lower:
            return project_name

    # Fallback: extract first meaningful words from purpose section
    for line in lines[:10]:
        if line.strip() and not line.startswith('**'):
            words = line.strip().split()[:3]
            if words:
                return slugify(' '.join(words))

    return 'misc'


def find_memory_inputs(claude_dir: Path) -> list:
    """Return memories export files: memories/*.json, else legacy memories.json."""
    inputs = sorted(claude_dir.glob('memories/*.json'))
    if inputs:
        return inputs
    legacy = claude_dir / 'memories.json'
    return [legacy] if legacy.exists() else []


def resolve_project_names(uuids, memory_files: list, claude_dir: Path) -> dict:
    """Map project uuid -> real project name from memory_files index.md, then Takeout projects/*.json."""
    names = {}
    for entry in memory_files:
        parts = (entry.get('path') or '').strip('/').split('/')
        if len(parts) == 3 and parts[0] == 'projects' and parts[2] == 'index.md':
            m = re.search(r'^name:\s*"?(.*?)"?\s*$', entry.get('content') or '', re.M)
            if m and m.group(1):
                names[parts[1]] = m.group(1)

    for uuid in uuids:
        if uuid in names:
            continue
        candidates = [claude_dir / 'projects' / f'{uuid}.json']
        candidates += sorted(claude_dir.glob(f'Takeout*/projects/{uuid}.json'))
        for candidate in candidates:
            try:
                name = json.loads(candidate.read_text(encoding='utf-8')).get('name')
            except (OSError, ValueError, AttributeError):
                continue
            if name:
                names[uuid] = name
                break
    return names


def claim_path(rel: str, used: set, suffix: str) -> str:
    """Reserve a relative output path, adding a deterministic suffix on in-run collision."""
    if rel in used:
        stem, dot, ext = rel.rpartition('.')
        if not dot:
            stem, ext = rel, ''
        candidate, n = f"{stem}-{suffix}", 2
        while candidate + dot + ext in used:
            candidate, n = f"{stem}-{suffix}-{n}", n + 1
        rel = candidate + dot + ext
    used.add(rel)
    return rel


def memory_to_markdown(memory_id: str, memory_content: str, title: str = None) -> str:
    """Convert a single memory entry to markdown format."""
    lines = []

    # Add header
    if title is None:
        title = extract_project_name(memory_content).replace('-', ' ').title()
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> Memory ID: `{memory_id}`")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Process the content - it's already in a nice format
    # Just clean up and add proper markdown structure
    content = memory_content

    # Convert **Section** to ## Section
    content = re.sub(r'\*\*([^*]+)\*\*\n', r'## \1\n', content)

    lines.append(content)

    return '\n'.join(lines)


def process_memories(input_path: Path, output_dir: Path, claude_dir: Path = None, used: set = None) -> dict:
    """Process one memories export (legacy list or current dict format) into markdown files.

    `used` holds output paths (relative to output_dir) already written in this run.
    """
    if claude_dir is None:
        claude_dir = input_path.parent if input_path.name == 'memories.json' else input_path.parent.parent
    if used is None:
        used = set()

    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    stats = {
        'total': 0,
        'processed': 0,
        'files_created': [],
    }

    # Handle the structure - it's a list with one object containing memories
    if isinstance(data, list) and len(data) > 0:
        memories_data = data[0]
    else:
        memories_data = data

    # Get project memories
    project_memories = memories_data.get('project_memories') or {}
    conversations_memory = memories_data.get('conversations_memory') or ''
    memory_files = memories_data.get('memory_files') or []
    account_suffix = (memories_data.get('account_uuid') or input_path.stem)[:8]

    def write(rel: str, content: str, suffix: str) -> Path:
        rel = claim_path(rel, used, suffix)
        output_file = output_dir / rel
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(content, encoding='utf-8')
        stats['processed'] += 1
        stats['files_created'].append(str(output_file))
        return output_file

    # Process main conversations memory
    if conversations_memory:
        stats['total'] += 1
        md_content = memory_to_markdown('main-context', conversations_memory)
        output_file = write('main-context.md', md_content, account_suffix)
        print(f"  Created: {output_file.name}")

    # Process project memories
    names = resolve_project_names(project_memories, memory_files, claude_dir)
    for memory_id in sorted(project_memories):
        memory_content = project_memories[memory_id] or ''
        stats['total'] += 1

        if memory_id in names:
            base_name = slugify(names[memory_id].replace('/', ' ')) or 'project'
            title = names[memory_id]
        else:
            base_name = extract_project_name(memory_content)
            title = None

        md_content = memory_to_markdown(memory_id, memory_content, title)
        output_file = write(f"{base_name}.md", md_content, memory_id[:8])
        print(f"  Created: {output_file.name}")

    # memory_files already carry their own frontmatter: write verbatim
    written = 0
    for entry in memory_files:
        stats['total'] += 1
        parts = (entry.get('path') or '').strip('/').split('/')
        if any(part in ('', '.', '..') for part in parts):
            print(f"  Skipped unsafe memory file path: {entry.get('path')!r}")
            continue
        write('/'.join(['files'] + parts), entry.get('content') or '', account_suffix)
        written += 1
    if memory_files:
        print(f"  Created: {written} files under files/")

    return stats


def reflection_to_markdown(reflection: dict) -> str:
    """Render one Claude reflection card as markdown with a small frontmatter."""
    content = reflection.get('content') or {}
    lines = ['---', 'type: reflection', f"period: {json.dumps(reflection.get('period', ''))}"]
    if reflection.get('created_at'):
        lines.append(f"created: {json.dumps(reflection['created_at'])}")
    if reflection.get('updated_at'):
        lines.append(f"updated: {json.dumps(reflection['updated_at'])}")
    lines += ['---', f"# {content.get('hero_title', 'Reflection')}", '']
    if content.get('period'):
        lines += [f"> {content['period']}", '']
    if content.get('hero_body'):
        lines += [content['hero_body'], '']

    if content.get('stats'):
        lines += ['## Stats', '']
        for st in content['stats']:
            sub = f" ({st['sublabel']})" if st.get('sublabel') else ''
            lines.append(f"- {st.get('label', '')}: {st.get('n', '')}{sub}")
        lines.append('')

    if content.get('topics'):
        lines += ['## Topics', '']
        for tp in content['topics']:
            pct = f" ({tp['percent']}%)" if tp.get('percent') is not None else ''
            desc = f" — {tp['description']}" if tp.get('description') else ''
            lines.append(f"- **{tp.get('title', '')}**{pct}{desc}")
        lines.append('')

    for key, heading in (('about_your_time', 'About your time'),
                         ('expanding_your_skills', 'Expanding your skills'),
                         ('worth_thinking_about', 'Worth thinking about')):
        if not content.get(key):
            continue
        lines += [f"## {heading}", '']
        for item in content[key]:
            skill = f" ({item['skill']})" if item.get('skill') else ''
            lines += [f"### {item.get('title', '')}{skill}", '', item.get('body') or '', '']

    return '\n'.join(lines).rstrip('\n') + '\n'


def process_reflections(input_path: Path, output_dir: Path, used: set) -> list:
    """Write each reflection in a Claude/reflections/*.json export to reflections/<period>.md."""
    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    account_suffix = (data.get('account_uuid') or input_path.stem)[:8]

    created = []
    for reflection in data.get('reflections') or []:
        period = str(reflection.get('period') or 'undated')
        # '/' would let an export-supplied period escape output_dir or create subdirectories
        rel = claim_path(f"reflections/{period.replace(':', '_').replace('/', '_')}.md", used, account_suffix)
        output_file = output_dir / rel
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(reflection_to_markdown(reflection), encoding='utf-8')
        created.append(str(output_file))
    return created


def backup_stale(output_dir: Path, used: set) -> int:
    """Move files in output_dir not produced this run to <parent>/_backup/memories-<ts>/."""
    # NFC on both sides: HFS+ returns decomposed names for the Greek filenames we wrote
    produced = {unicodedata.normalize('NFC', rel) for rel in used}
    stale = sorted(p for p in output_dir.rglob('*') if p.is_file() and
                   unicodedata.normalize('NFC', p.relative_to(output_dir).as_posix()) not in produced)
    if stale:
        backup_dir = output_dir.parent / '_backup' / f"memories-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        for p in stale:
            dest = backup_dir / p.relative_to(output_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
        for d in sorted((d for d in output_dir.rglob('*') if d.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        print(f"  Moved {len(stale)} stale file(s) to {backup_dir}")
    else:
        print("  Moved 0 stale files")
    return len(stale)


def convert_all(inputs: list, claude_dir: Path, output_dir: Path) -> dict:
    """Convert every memories input plus reflections, then back up files this run did not produce."""
    output_dir.mkdir(parents=True, exist_ok=True)
    used = set()
    stats = {'total': 0, 'processed': 0, 'files_created': []}

    for input_path in inputs:
        s = process_memories(input_path, output_dir, claude_dir, used)
        for key in ('total', 'processed', 'files_created'):
            stats[key] += s[key]

    for input_path in sorted(claude_dir.glob('reflections/*.json')):
        created = process_reflections(input_path, output_dir, used)
        print(f"  Created: {len(created)} reflections from {input_path.name}")
        stats['total'] += len(created)
        stats['processed'] += len(created)
        stats['files_created'] += created

    stats['backed_up'] = backup_stale(output_dir, used)
    return stats


def main():
    """Main entry point."""
    # Paths
    base_dir = Path(__file__).parent.parent
    claude_dir = base_dir / 'Claude'
    inputs = find_memory_inputs(claude_dir)
    output_dir = base_dir / 'output' / 'memories'

    print("=" * 60)
    print("Phase 0: Converting Claude Memories to Markdown")
    print("=" * 60)
    for input_path in inputs:
        print(f"\nInput:  {input_path}")
    print(f"Output: {output_dir}")
    print()

    if not inputs:
        print(f"ERROR: No memories export found: {claude_dir / 'memories'}/*.json or {claude_dir / 'memories.json'}")
        return 1

    print("Processing memories...")
    stats = convert_all(inputs, claude_dir, output_dir)

    print()
    print("=" * 60)
    print("COMPLETE")
    print("=" * 60)
    print(f"Total memories:    {stats['total']}")
    print(f"Files created:     {stats['processed']}")
    print(f"Output directory:  {output_dir}")
    print()
    print("Files created:")
    for f in stats['files_created']:
        print(f"  - {Path(f).relative_to(output_dir)}")

    return 0


if __name__ == '__main__':
    exit(main())
