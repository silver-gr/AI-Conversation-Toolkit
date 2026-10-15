#!/usr/bin/env python3
"""
Re-render already-imported conversations in place with the current extractors.

Incremental imports dedup by conversation id, so extractor fixes never reach
conversations imported earlier. This script re-renders them from the current
exports and overwrites their existing files (output/<source>/conversations/ or
output/_excluded/<source>/):

- Files with an `id:` frontmatter key are matched by id and rewritten directly.
  Older files are matched by frontmatter `date` (to the second, else to the
  minute: older renders stored minute precision); title breaks ties.
- Filename, frontmatter `title` and H1 are kept (Gemini titles may have been
  renamed by gemini_meta_indexer).
- Provenance guard for older files: every old message must still exist somewhere
  in the current raw export (any branch). Removals the fixes intend (off-branch
  turns, Claude thinking/placeholders) pass. ChatGPT/Claude messages the export no
  longer contains (e.g. DALL-E prompt and memory tool messages) are re-inserted
  before the message that followed them, tagged with RESTORED_MARK so later
  re-renders of id-matched files carry them forward; ones that sat on an abandoned
  branch are dropped like the rest of that branch. Gemini files with
  upstream-deleted activities are kept as they are.
- ChatGPT files that would shrink by more than half (and >1,000 chars), i.e. the
  richest content sat on a dropped branch, are kept as they are for review.

Dry-run by default; --apply writes. Writes a JSON plan with --plan PATH.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from simple_extractor import (parse_chatgpt_export, parse_claude_export, conversation_to_markdown,
                              strip_chatgpt_citations, _escape_yaml_string)
from gemini_extractor import parse_activity, group_into_conversations, activity_to_markdown

FOLDERS = {'chatgpt': 'chatgpt-full', 'claude': 'claude-full', 'gemini': 'gemini'}
_MSG_HEADER = re.compile(r'^### (USER|ASSISTANT|GEMINI)(?: \(Canvas\))?(?: \((\d{2}):(\d{2})\))?$', re.M)
_GEMINI_USER_NOISE = re.compile(r'^\*(?:Attachments: .*|\[Image attached\])\*$|^---$', re.M)
_PROBE_LEN = 200
RESTORED_MARK = '*[restored from an earlier export]*'


def norm(text: str) -> str:
    return ' '.join(text.split())


def read_existing(output_dir: Path, folder: str) -> tuple[dict, dict]:
    """Index existing files of one source: (by frontmatter id, by date string / 'min:' minute)."""
    by_id, by_date = {}, defaultdict(list)
    for d in (output_dir / folder / 'conversations', output_dir / '_excluded' / folder):
        for fp in sorted(d.glob('*.md')) if d.exists() else []:
            text = fp.read_text(encoding='utf-8')
            end = text.find('\n---', 3)
            if not text.startswith('---') or end == -1:
                continue
            try:
                fm = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                continue
            item = {'path': fp, 'fm': fm, 'body': text[end + 4:]}
            if fm.get('id'):
                by_id[str(fm['id'])] = item
                continue
            date = fm.get('date')
            key = date.strftime('%Y-%m-%dT%H:%M:%S') if isinstance(date, datetime) else str(date)
            by_date[key].append(item)
            by_date['min:' + key[:16]].append(item)
    return by_id, by_date


def old_messages(body: str) -> list[tuple[str, str, tuple | None]]:
    """Split a rendered body into (role, content, (hh, mm)) using the '### ROLE (HH:MM)' headers."""
    marks = list(_MSG_HEADER.finditer(body))
    out = []
    for i, m in enumerate(marks):
        chunk = body[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(body)]
        chunk = re.sub(r'\n---\s*$', '', chunk.strip()).strip()
        hhmm = (int(m.group(2)), int(m.group(3))) if m.group(2) else None
        out.append((m.group(1), chunk, hhmm))
    return out


def chatgpt_raw_texts(conv: dict) -> list[str]:
    """Every text the old extractor could have emitted for this conversation, on any branch."""
    texts = []
    for node in conv.get('mapping', {}).values():
        content = (node.get('message') or {}).get('content') or {}
        parts = content.get('parts') or []
        if content.get('content_type') == 'text':
            texts.append(strip_chatgpt_citations('\n'.join(str(p) for p in parts if p)))
    return [norm(t) for t in texts if t.strip()]


def claude_raw_texts(conv: dict) -> list[str]:
    texts = []
    for msg in conv.get('chat_messages') or []:
        texts.append(msg.get('text') or '')
        for c in msg.get('content') or []:
            if isinstance(c, dict) and c.get('text'):
                texts.append(c['text'])
    return [norm(t) for t in texts if t.strip()]


def lost_indices(old_msgs: list, raw_texts: list[str]) -> list[int]:
    """Indices of old messages whose text no longer appears anywhere in the raw export conversation."""
    haystack = '\n'.join(raw_texts)
    return [i for i, (_, content, _) in enumerate(old_msgs)
            if norm(content)[:_PROBE_LEN] and norm(content)[:_PROBE_LEN] not in haystack]


def restore_lost(messages: list[dict], old_msgs: list, lost: list[int], fallback_day) -> tuple[list, int]:
    """Re-insert lost old messages before the new message matching the next surviving old message.

    Surviving old messages are located in the new render with a forward-only search.
    A lost message whose nearest preceding surviving message is not in the new render
    sat on an abandoned branch and is dropped. Returns (messages, restored count).
    """
    new_norm = [norm(m['content']) for m in messages]
    lost_set = set(lost)
    located, cursor = {}, 0
    for j, (_, content, _) in enumerate(old_msgs):
        if j in lost_set:
            continue
        probe = norm(content)[:_PROBE_LEN]
        k = next((k for k in range(cursor, len(new_norm)) if probe and probe in new_norm[k]), None)
        if k is not None:
            located[j], cursor = k, k + 1

    before, tail, restored = defaultdict(list), [], 0
    for j in lost:
        prev = max((i for i in range(j) if i not in lost_set), default=None)
        if prev is not None and prev not in located:
            continue
        nxt = next((located[i] for i in range(j + 1, len(old_msgs)) if i in located), None)
        role, content, hhmm = old_msgs[j]
        ref = messages[nxt] if nxt is not None else (messages[-1] if messages else None)
        day = (ref or {}).get('timestamp') or fallback_day
        ts = day.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0) if day and hhmm else None
        if not content.startswith(RESTORED_MARK):
            content = f"{RESTORED_MARK}\n\n{content}"
        msg = {'role': role.lower(), 'content': content, 'timestamp': ts, 'model': ''}
        (tail if nxt is None else before[nxt]).append(msg)
        restored += 1
    merged = []
    for k, m in enumerate(messages):
        merged.extend(before[k])
        merged.append(m)
    return merged + tail, restored


def retitle(md: str, title: str) -> str:
    """Put the existing file's title back into the new render (frontmatter + H1), on one line."""
    title = ' '.join(title.split('\n')).strip()
    md = re.sub(r'^title: .*$', lambda _: f"title: {_escape_yaml_string(title)}", md, count=1, flags=re.M)
    return re.sub(r'^# .*$', lambda _: f"# {title}", md, count=1, flags=re.M)


def _pick(candidates: list[dict], title: str) -> dict | None:
    if len(candidates) == 1:
        return candidates[0]
    same = [c for c in candidates if str(c['fm'].get('title', '')) == title]
    return same[0] if len(same) == 1 else None


def assign_matches(convs: list[dict], by_id: dict, by_date: dict, probes_of) -> list[tuple]:
    """Match each conversation to at most one file, and each file to at most one conversation.

    Pass 1: by id. Pass 2: exact-second date (title breaks ties). Pass 3: minute-level date
    (older renders stored minute precision). When several conversations start in the same
    minute, a date match also requires the old body to contain one of the conversation's
    message probes, since a minute-precision date cannot tell them apart.
    Returns [(match | None, how)] aligned with convs; how is 'id' | 'date' | 'unmatched' | 'ambiguous'.
    """
    result, claimed = [None] * len(convs), set()
    keys = [c['created_at'].strftime('%Y-%m-%dT%H:%M:%S') for c in convs]
    per_minute = defaultdict(int)
    for k in keys:
        per_minute[k[:16]] += 1

    def candidates(i, key):
        cands = [c for c in by_date.get(key, []) if c['path'] not in claimed]
        if per_minute[keys[i][:16]] > 1:
            probes = [p for p in probes_of(convs[i]) if p]
            cands = [c for c in cands if any(p in c.setdefault('nbody', norm(c['body'])) for p in probes)]
        return cands

    for i, conv in enumerate(convs):
        if conv['id'] in by_id:
            result[i] = (by_id[conv['id']], 'id')
            claimed.add(by_id[conv['id']]['path'])
    for key_of in (lambda k: k, lambda k: 'min:' + k[:16]):
        for i, conv in enumerate(convs):
            if result[i] is None:
                cands = candidates(i, key_of(keys[i]))
                m = _pick(cands, conv['title']) if cands else None
                if m:
                    result[i] = (m, 'date')
                    claimed.add(m['path'])
    for i in range(len(convs)):
        if result[i] is None:
            seen = by_date.get(keys[i]) or by_date.get('min:' + keys[i][:16])
            result[i] = (None, 'ambiguous' if seen else 'unmatched')
    return result


def _chars(messages: list[dict]) -> int:
    return sum(len(m['content']) for m in messages)


def plan_source(source: str, base: Path, output_dir: Path, prev_ids: set[str]) -> list[dict]:
    by_id, by_date = read_existing(output_dir, FOLDERS[source])
    plan = []

    if source in ('chatgpt', 'claude'):
        export = base / ('ChatGPT' if source == 'chatgpt' else 'Claude') / 'conversations.json'
        raw = {c.get('id') or c.get('uuid') or c.get('conversation_id'): c
               for c in json.load(open(export, encoding='utf-8'))}
        parser = parse_chatgpt_export if source == 'chatgpt' else parse_claude_export
        raw_texts = chatgpt_raw_texts if source == 'chatgpt' else claude_raw_texts
        convs = [c for c in parser(export) if c['id'] in prev_ids and c['created_at']]
        matches = assign_matches(convs, by_id, by_date,
                                 lambda c: [norm(m['content'])[:80] for m in c['messages'][:10]])
        for conv, (match, how) in zip(convs, matches):
            key = conv['created_at'].strftime('%Y-%m-%dT%H:%M:%S')
            entry = {'id': conv['id'], 'title': conv['title'], 'date': key}
            if not match:
                entry['action'] = how
                plan.append(entry)
                continue
            entry['path'] = str(match['path'])
            old_chars = int(match['fm'].get('characters') or 0)
            action = 'rewrite'
            if how == 'id':
                olds = old_messages(match['body'])
                kept = [j for j, (_, c, _) in enumerate(olds) if c.startswith(RESTORED_MARK)]
                if kept:
                    merged, restored = restore_lost(conv['messages'], olds, kept, conv['created_at'])
                    conv = dict(conv, messages=merged)
                    entry['restored_count'] = restored
                    action = 'rewrite_restored'
            if how == 'date':
                rc = raw.get(conv['id'])
                if not rc:
                    entry.update(action='keep_upstream_loss', missing=['raw conversation not found'])
                    plan.append(entry)
                    continue
                olds = old_messages(match['body'])
                lost = lost_indices(olds, raw_texts(rc))
                if lost:
                    merged, restored = restore_lost(conv['messages'], olds, lost, conv['created_at'])
                    conv = dict(conv, messages=merged)
                    entry.update(restored_count=restored, dropped_offbranch=len(lost) - restored,
                                 restored=[f"{olds[i][0]}: {norm(olds[i][1])[:80]}" for i in lost[:5]])
                    action = 'rewrite_restored' if restored else 'rewrite'
                new_chars = _chars(conv['messages'])
                if source == 'chatgpt' and new_chars < old_chars / 2 and old_chars - new_chars > 1000:
                    entry.update(action='keep_shrunk', old_chars=old_chars, new_chars=new_chars)
                    plan.append(entry)
                    continue
            md = retitle(conversation_to_markdown(conv), str(match['fm'].get('title') or conv['title']))
            entry.update(action=action, md=md, matched_by=how,
                         old_messages=match['fm'].get('messages'), new_messages=len(conv['messages']),
                         old_chars=old_chars, new_chars=_chars(conv['messages']))
            plan.append(entry)
        return plan

    export = base / 'Google' / 'My Activity' / 'Gemini Apps' / 'MyActivity.json'
    activities = [a for a in (parse_activity(e) for e in json.load(open(export, encoding='utf-8')))
                  if a and a['type'] != 'imported_query']
    convs = []
    for conv in group_into_conversations(activities):
        act_ids = [a['timestamp'].isoformat() for a in conv['activities'] if a.get('timestamp')]
        if conv['id'] in prev_ids or (act_ids and all(a in prev_ids for a in act_ids)):
            convs.append(conv)
    matches = assign_matches(convs, by_id, by_date,
                             lambda c: [norm(a['query'])[:60] for a in c['activities'][:10]])
    for conv, (match, how) in zip(convs, matches):
        key = conv['created_at'].strftime('%Y-%m-%dT%H:%M:%S')
        entry = {'id': conv['id'], 'title': conv['title'], 'date': key}
        if not match:
            entry['action'] = how
            plan.append(entry)
            continue
        entry['path'] = str(match['path'])
        old_count = int(match['fm'].get('messages') or 0)
        if how == 'date':
            queries = '\n'.join(norm(a['query']) for a in conv['activities'])
            old_users = [norm(_GEMINI_USER_NOISE.sub('', c))[:60]
                         for r, c, _ in old_messages(match['body']) if r == 'USER']
            missing = [f"USER: {u}" for u in old_users if u and u not in queries]
            if len(conv['activities']) < old_count:
                missing.append(f"activities {old_count} -> {len(conv['activities'])}")
            if missing:
                entry.update(action='keep_upstream_loss', missing=missing[:5], missing_count=len(missing))
                plan.append(entry)
                continue
        md = retitle(activity_to_markdown(conv), str(match['fm'].get('title') or conv['title']))
        entry.update(action='rewrite', md=md, matched_by=how, old_messages=old_count,
                     new_messages=len(conv['activities']), old_chars=match['fm'].get('characters'),
                     new_chars=sum(len(a['query']) + len(a['response']) + len(a['canvas_content'])
                                   for a in conv['activities']))
        plan.append(entry)
    return plan


def previous_ids(log_path: Path, source: str, exclude_import: str | None = None) -> set[str]:
    """Ids logged by completed imports (optionally excluding one import)."""
    log = json.load(open(log_path, encoding='utf-8'))
    ids = set()
    for imp in log.get('imports', []):
        if imp.get('id') == exclude_import or imp.get('status') != 'completed':
            continue
        for key, info in (imp.get('sources') or {}).items():
            if key.split(':')[0] == source:
                ids.update(info.get('conversation_ids') or [])
    return ids


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--sources', default='chatgpt,claude,gemini')
    ap.add_argument('--exclude-import', help='skip conversations logged by this import id')
    ap.add_argument('--apply', action='store_true', help='overwrite files (default: dry-run)')
    ap.add_argument('--plan', help='write the plan (without rendered markdown) as JSON here')
    args = ap.parse_args()

    base = Path(__file__).parent.parent
    output_dir = base / 'output'
    full = {}
    for source in args.sources.split(','):
        prev = previous_ids(base / 'imports' / 'import_log.json', source, args.exclude_import)
        plan = plan_source(source, base, output_dir, prev)
        full[source] = plan
        counts = defaultdict(int)
        for e in plan:
            counts[e['action']] += 1
        print(f"{source}: {dict(counts)}")
        if args.apply:
            written = 0
            for e in plan:
                if e['action'].startswith('rewrite'):
                    Path(e['path']).write_text(e['md'], encoding='utf-8')
                    written += 1
            print(f"  wrote {written} files")

    if args.plan:
        slim = {s: [{k: v for k, v in e.items() if k != 'md'} for e in p] for s, p in full.items()}
        Path(args.plan).write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f"plan: {args.plan}")
    if not args.apply:
        print("[dry-run] nothing written; re-run with --apply")
    return 0


if __name__ == '__main__':
    sys.exit(main())
