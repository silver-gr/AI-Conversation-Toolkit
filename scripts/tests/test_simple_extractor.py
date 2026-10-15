#!/usr/bin/env python3
"""Checks for simple_extractor: ChatGPT current branch, multimodal parts, library
injection (Deep Research reports / text files), Claude branch + block rendering,
attachments, research_type/tools and the frontmatter id line.

Run: /Users/silver/venv/bin/python3 scripts/tests/test_simple_extractor.py
"""

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simple_extractor import (
    _fenced, parse_chatgpt_export, parse_claude_export, extract_claude_messages,
    conversation_to_markdown,
)


def _node(nid, parent, role, content, model=None):
    return {'id': nid, 'parent': parent, 'message': {
        'id': nid, 'author': {'role': role}, 'create_time': 1790000000,
        'content': content, 'metadata': {'model_slug': model} if model else {}}}


def _text(s):
    return {'content_type': 'text', 'parts': [s]}


def _chatgpt_conv(cid, mapping, current, **extra):
    return {'id': cid, 'title': cid, 'create_time': 1790000000, 'update_time': 1790000000,
            'mapping': mapping, 'current_node': current, **extra}


def _write_chatgpt_export(tmp, convs, library=None, dats=None):
    (tmp / 'conversations.json').write_text(json.dumps(convs))
    if library is not None:
        (tmp / 'library_files.json').write_text(json.dumps(library))
        with zipfile.ZipFile(tmp / 'export.zip', 'w') as z:
            for name, body in (dats or {}).items():
                z.writestr(name, body)
    return tmp / 'conversations.json'


def _lib(file_id, thread, origin, mime, name, kind=None, created='2026-09-01T10:00:00+00:00'):
    return {'file_id': file_id, 'origination_thread_id': thread, 'origination_message_id': origin,
            'mime_type': mime, 'file_name': name, 'library_artifact_type': kind,
            'created_at': created, 'deleted_at': None, 'trashed_at': None}


def test_fence_longer_than_body_backticks():
    assert _fenced('a ``` b ```` c') == '`````\na ``` b ```` c\n`````'
    assert _fenced('plain', 'py') == '```py\nplain\n```'


def test_chatgpt_branch_multimodal_and_model_fallback():
    mapping = {
        'root': {'id': 'root', 'parent': None, 'message': None},
        'u1': _node('u1', 'root', 'user', {'content_type': 'multimodal_text', 'parts': [
            {'content_type': 'image_asset_pointer'}, 'look at this', ' ',
            {'content_type': 'audio_transcription', 'text': 'spoken words'},
            {'content_type': 'audio_asset_pointer'},
            {'content_type': 'real_time_user_audio_video_asset_pointer'}]}),
        'a_old': _node('a_old', 'u1', 'assistant', _text('abandoned answer'), 'm1'),
        'a_new': _node('a_new', 'u1', 'assistant', {'content_type': 'thoughts', 'thoughts': []}),
    }
    convs = [
        _chatgpt_conv('c1', mapping, 'a_new', default_model_slug='gpt-x'),
        _chatgpt_conv('c2', {'r': {'id': 'r', 'parent': None, 'message': None}}, 'r'),
        _chatgpt_conv('c3', {'r': {'id': 'r', 'parent': None, 'message': None},
                             'u': _node('u', 'r', 'user', _text('hi')),
                             'a': _node('a', 'u', 'assistant', _text('dfs'))}, None,
                      default_model_slug='auto'),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out = list(parse_chatgpt_export(_write_chatgpt_export(Path(tmp), convs)))
    assert [c['id'] for c in out] == ['c1', 'c3']  # c2 empty -> skipped
    c1 = out[0]
    assert [m['content'] for m in c1['messages']] == ['[image]\nlook at this\nspoken words']
    md = conversation_to_markdown(c1)
    assert 'source: chatgpt\nid: "c1"\nmodel: gpt-x\n' in md
    assert 'research_type: null\n---' in md and 'tools:' not in md
    assert [m['content'] for m in out[1]['messages']] == ['hi', 'dfs']  # no current_node -> DFS
    assert 'model: null' in conversation_to_markdown(out[1])  # 'auto' is not a model


def test_chatgpt_library_injection():
    mapping = {
        'root': {'id': 'root', 'parent': None, 'message': None},
        'u1': _node('u1', 'root', 'user', _text('make a file')),
        'a1': _node('a1', 'u1', 'assistant', _text('here it is'), 'm'),
        'u2': _node('u2', 'a1', 'user', _text('thanks')),
    }
    report = {'title': 'Report T', 'widget_state': {'status': 'completed', 'report_message': {
        'id': 'rep1', 'create_time': 1790000100, 'content': {'content_type': 'text', 'parts': [
            'Finding.citeturn1search0turn2view1 More entity["x","Name"].']}}}}
    library = [
        _lib('f_rep', 't1', 'rep1', 'application/json', 'Report T', 'deep_research_report'),
        _lib('f_md', 't1', 'a1', 'text/markdown', 'notes.md', created='2026-09-01T10:01:00+00:00'),
        _lib('f_dup', 't1', 'a1', 'text/markdown', 'notes(1).md', created='2026-09-01T10:02:00+00:00'),
        _lib('f_user', 't1', 'u1', 'text/markdown', 'from_user.md'),
        _lib('f_orphan', 'gone', 'x', 'application/json', 'Orphan', 'deep_research_report'),
        _lib('f_nodat', 't1', 'a1', 'text/plain', 'missing.txt'),
    ]
    dats = {'f_rep.dat': json.dumps(report), 'f_md.dat': 'body ``` x', 'f_dup.dat': 'body ``` x',
            'f_user.dat': 'user file', 'f_orphan.dat': json.dumps(report)}
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_chatgpt_export(Path(tmp), [_chatgpt_conv('t1', mapping, 'u2')], library, dats)
        out = list(parse_chatgpt_export(path))
    conv, orphan = out
    contents = [m['content'] for m in conv['messages']]
    assert contents[:2] == ['make a file', 'here it is']
    assert contents[2] == '#### File: notes.md\n\n~~~\nbody ``` x\n~~~'  # right after a1, dup dropped
    assert contents[3] == 'thanks'
    assert contents[4].startswith('#### Deep Research report: Report T\n\nFinding. More entity')
    assert len(contents) == 5  # user-origin file and missing .dat skipped
    assert conv['research_type'] == 'chatgpt_deep_research'
    assert 'research_type: chatgpt_deep_research' in conversation_to_markdown(conv)
    late = dict(mapping, u2=dict(mapping['u2'], message=dict(mapping['u2']['message'], create_time=1790000200)))
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_chatgpt_export(Path(tmp), [_chatgpt_conv('t1', late, 'u2')], library, dats)
        late_contents = [m['content'] for m in list(parse_chatgpt_export(path))[0]['messages']]
    assert late_contents[3].startswith('#### Deep Research report') and late_contents[4] == 'thanks'
    assert orphan['id'] == 'gone' and orphan['title'] == 'Report T'
    assert orphan['research_type'] == 'chatgpt_deep_research' and len(orphan['messages']) == 1


def _cmsg(uuid, parent, sender, created, content, text='', attachments=(), files=()):
    return {'uuid': uuid, 'parent_message_uuid': parent, 'sender': sender, 'created_at': created,
            'text': text, 'content': content, 'attachments': list(attachments), 'files': list(files)}


ROOT = '00000000-0000-4000-8000-000000000000'


def test_claude_branch_blocks_attachments():
    art_body = 'code with ``` inside'
    conv = {'uuid': 'cl1', 'name': 'T', 'created_at': '2026-09-01T10:00:00Z', 'chat_messages': [
        _cmsg('h1', ROOT, 'human', '2026-09-01T10:00:00Z', [{'type': 'text', 'text': ''}],
              attachments=[{'file_name': '', 'extracted_content': 'pasted brief'}],
              files=[{'file_uuid': 'f1', 'file_name': 'pic.png'}]),
        _cmsg('a_old', 'h1', 'assistant', '2026-09-01T10:01:00Z', [{'type': 'text', 'text': 'old regen'}]),
        _cmsg('a1', 'h1', 'assistant', '2026-09-01T10:02:00Z', [
            {'type': 'thinking', 'thinking': 'SECRET REASONING'},
            {'type': 'text', 'text': 'Answer.'},
            {'type': 'tool_use', 'name': 'web_search', 'input': {}},
            {'type': 'tool_result', 'content': [{'type': 'text', 'text': 'RESULT'}]},
            {'type': 'tool_use', 'name': 'web_search', 'input': {}},
            {'type': 'tool_use', 'name': 'launch_extended_search_task', 'input': {}},
            {'type': 'tool_use', 'name': 'artifacts', 'input': {
                'command': 'create', 'id': 'r1', 'title': 'Report', 'type': 'text/markdown', 'content': art_body}},
            {'type': 'tool_use', 'name': 'artifacts', 'input': {'command': 'update', 'id': 'r1'}},
            {'type': 'tool_use', 'name': 'create_file', 'input': {'path': '/o/a.md', 'file_text': 'A'}},
            {'type': 'tool_use', 'name': 'Write', 'input': {'file_path': 'b.py', 'content': 'B'}},
            {'type': 'tool_use', 'name': 'SendUserMessage', 'input': {'message': 'Status note'}},
            {'type': 'image', 'file_uuid': 'i1'},
            {'type': 'document', 'file_uuid': 'd1'},
        ], files=[{'file_uuid': 'd1', 'file_name': 'brief.pdf'}]),
        # assistant -> assistant continuation, later than a_old: this is the latest leaf
        _cmsg('a2', 'a1', 'assistant', '2026-09-01T10:03:00Z', [],
              text='Done.\n```\nThis block is not supported on your current device yet.\n```\n\n'
                   'Viewing artifacts created via the Analysis Tool web feature preview isn’t yet supported on mobile.\n'),
    ]}
    msgs = extract_claude_messages(conv)
    assert [m['role'] for m in msgs] == ['user', 'assistant', 'assistant']
    assert msgs[0]['content'] == '**Attachment: pasted text**\n\n~~~\npasted brief\n~~~\n\n*[file: pic.png]*'
    body = msgs[1]['content']
    assert 'SECRET' not in body and 'RESULT' not in body and 'old regen' not in body
    assert body == (
        'Answer.\n\n#### Artifact: Report\n\n````markdown\ncode with ``` inside\n````\n\n'
        '*[artifact r1 updated]*\n\n#### File: /o/a.md\n\n```\nA\n```\n\n#### File: b.py\n\n```\nB\n```\n\n'
        'Status note\n\n*[image]*\n\n*[document: brief.pdf]*\n\n'
        '*[tools: web_search x2, launch_extended_search_task x1]*\n\n*[file: brief.pdf]*'), body
    assert msgs[2]['content'] == 'Done.\n'

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'conversations.json'
        p.write_text(json.dumps([conv, {'uuid': 'empty', 'name': '', 'chat_messages': []}]))
        out = list(parse_claude_export(p))
    assert len(out) == 1
    c = out[0]
    assert c['research_type'] == 'claude_research'
    assert c['tools'] == ['SendUserMessage', 'Write', 'artifacts', 'create_file',
                          'launch_extended_search_task', 'web_search']
    md = conversation_to_markdown(c)
    assert 'source: claude\nid: "cl1"\nmodel: null\n' in md
    assert 'research_type: claude_research\ntools:\n  - SendUserMessage\n' in md


def test_claude_research_type_web_search_and_legacy_order():
    def conv(n):
        return {'uuid': 'w', 'name': 'w', 'chat_messages': [
            {'sender': 'human', 'text': 'q', 'content': [{'type': 'text', 'text': 'q'}]},
            {'sender': 'assistant', 'text': '', 'content':
                [{'type': 'tool_use', 'name': 'web_search', 'input': {}}] * n + [{'type': 'text', 'text': 'a'}]},
        ]}
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'conversations.json'
        p.write_text(json.dumps([conv(3), conv(2)]))
        three, two = list(parse_claude_export(p))
    assert three['research_type'] == 'claude_web_search' and two['research_type'] is None
    assert [m['content'] for m in three['messages']] == ['q', 'a\n\n*[tools: web_search x3]*']


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('all simple_extractor checks passed')
