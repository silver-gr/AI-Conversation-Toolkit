#!/usr/bin/env python3
"""Plain-assert checks for backfill_rerender restore placement and retitle."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backfill_rerender import restore_lost, retitle, assign_matches, norm

T = datetime(2026, 1, 2, 10, 0)


def _m(role, content):
    return {'role': role, 'content': content, 'timestamp': T, 'model': ''}


def test_lost_goes_before_next_surviving_message():
    # Old file lacked the (multimodal) user request; DALL-E prompt preceded the result.
    old = [('ASSISTANT', '{"prompt": "a cat"}', (10, 1)), ('ASSISTANT', 'Here is your cat', (10, 2))]
    new = [_m('user', '[image]\ndraw a cat'), _m('assistant', 'Here is your cat')]
    merged, n = restore_lost(new, old, [0], T)
    assert n == 1
    mark = '*[restored from an earlier export]*\n\n'
    assert [m['content'] for m in merged] == ['[image]\ndraw a cat', mark + '{"prompt": "a cat"}', 'Here is your cat']
    assert merged[1]['timestamp'] == T.replace(minute=1)
    # Re-render of the same file (id-matched): marked message carried forward, not duplicated or re-marked
    again = [(m['role'].upper(), m['content'], (10, 1)) for m in merged]
    merged2, n2 = restore_lost(new, again, [1], T)
    assert n2 == 1 and [m['content'] for m in merged2] == [m['content'] for m in merged]


def test_lost_after_abandoned_branch_is_dropped_and_trailing_kept():
    old = [('USER', 'q old branch', (9, 0)), ('ASSISTANT', 'memory note A', (9, 1)),
           ('USER', 'q current', (9, 5)), ('ASSISTANT', 'answer', (9, 6)), ('ASSISTANT', 'memory note B', (9, 7))]
    new = [_m('user', 'q current'), _m('assistant', 'answer')]
    merged, n = restore_lost(new, old, [1, 4], T)
    assert n == 1
    assert [m['content'] for m in merged] == ['q current', 'answer', '*[restored from an earlier export]*\n\nmemory note B']


def test_retitle_single_line():
    md = 'title: "x"\n---\n\n# x\n\nbody'
    out = retitle(md, 'line one\nline two')
    assert 'title: "line one line two"' in out and '# line one line two\n' in out



def test_same_minute_same_title_claims_file_once_by_content():
    a = {'id': 'A', 'title': 'Advice', 'created_at': datetime(2025, 7, 10, 15, 15, 0), 'messages': [_m('user', 'first ask')]}
    b = {'id': 'B', 'title': 'Advice', 'created_at': datetime(2025, 7, 10, 15, 15, 49), 'messages': [_m('user', 'second ask')]}
    f = {'path': 'x.md', 'fm': {'title': 'Advice'}, 'body': '### USER (15:15)\n\nsecond ask\n'}
    by_date = {'2025-07-10T15:15:00': [f], 'min:2025-07-10T15:15': [f]}
    res = assign_matches([a, b], {}, by_date, lambda c: [norm(m['content'])[:80] for m in c['messages']])
    # exact-second pass would give the file to A; content does not match, so A must not take it
    assert res[1][0] is f and [r[0] for r in res].count(f) == 1, res

if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
