#!/usr/bin/env python3
"""Checks for gemini_extractor: HTML->Markdown conversion, multi-item responses,
attachments / generated images, and the frontmatter id line.

Run: /Users/silver/venv/bin/python3 scripts/tests/test_gemini_extractor.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_extractor import strip_html, parse_activity, group_into_conversations, activity_to_markdown


def test_escaped_code_survives():
    out = strip_html(
        '<h3>HTML</h3>\n<pre><code>&lt;div class=&quot;x&quot;&gt;\n\n\n\n'
        '  &lt;h2&gt;Αδυναμία&lt;/h2&gt;\n&lt;/div&gt;\n</code></pre>\n'
        '<p>Use <code>a &lt; b</code> and x &lt;y&gt; text.</p>'
    )
    assert out == (
        '### HTML\n\n```\n<div class="x">\n\n\n\n  <h2>Αδυναμία</h2>\n</div>\n```\n\n'
        'Use `a < b` and x <y> text.'
    ), out


def test_lists_quote_links():
    out = strip_html(
        '<ol>\n<li>One <strong>bold </strong>tail\n<ul>\n<li>sub a</li>\n'
        '<li>sub b<ol><li>deep</li></ol></li>\n</ul>\n</li>\n'
        '<li>Two<pre><code>x = 1\n</code></pre></li>\n</ol>\n'
        '<blockquote><p>quote <em>it</em></p><p>p2</p></blockquote><hr>'
        '<p>see <a href="http://e.com">link</a> <img src="x"> <span>kept</span></p>'
    )
    assert out == (
        '1. One **bold** tail\n'
        '   - sub a\n'
        '   - sub b\n'
        '     1. deep\n'
        '2. Two\n\n'
        '   ```\n   x = 1\n   ```\n\n'
        '> quote *it*\n>\n> p2\n\n'
        '---\n\n'
        'see [link](http://e.com) [image] kept'
    ), out


def test_table_and_greek():
    out = strip_html(
        '<h2>Πίνακας</h2><table><thead><tr><th>A|B</th><th>C</th></tr></thead>'
        '<tbody><tr><td><strong>x</strong><br>y</td><td>Ελληνικά&nbsp;κείμενο</td></tr>'
        '<tr><td>only</td></tr></tbody></table>'
    )
    assert out == (
        '## Πίνακας\n\n'
        '| A\\|B | C |\n| --- | --- |\n| **x** y | Ελληνικά\xa0κείμενο |\n| only |  |'
    ), out


def _entry(**kw):
    base = {'title': 'Prompted hello', 'time': '2026-09-14T11:05:14.598Z'}
    base.update(kw)
    return base


def test_multi_item_attachments_images_and_id():
    act = parse_activity(_entry(
        safeHtmlItem=[{'html': '<p></p>'}, {'html': '<p>report</p>'},
                      {'html': '<p>plan</p>'}, {'html': '<p>report</p>'}],
        subtitles=[{'name': '2 generated images.'}, {'name': 'Attached 1 file.'},
                   {'name': '-  image_7bd690.jpg'}],
        attachedFiles=['watermarked_img_1-abc.jpg', 'image_7bd690-def.jpg'],
    ))
    assert act['response'] == 'report\n\n---\n\nplan', act['response']
    assert act['attachments'] == ['Attached 1 file.']
    assert act['generated_images'] == 2

    greek = parse_activity(_entry(subtitles=[{'name': '1 δημιουργημένη εικόνα.'},
                                             {'name': '2 συνημμένα αρχεία.'}]))
    assert greek['generated_images'] == 1 and greek['attachments'] == ['2 συνημμένα αρχεία.']

    image_only = parse_activity(_entry(time='2026-09-14T11:00:00Z', safeHtmlItem=[{'html': '<p></p>'}],
                                       subtitles=[{'name': '1 generated image.'}]))
    conv = group_into_conversations([act, image_only])[0]
    md = activity_to_markdown(conv)
    assert 'source: gemini\nid: "2026-09-14T11:05:14.598000"\nmodel: null' in md, md
    assert '*Attachments: image_7bd690-def.jpg*' in md
    assert 'watermarked_img_' not in md
    assert 'report\n\n---\n\nplan\n\n*[2 generated images]*' in md
    assert '### GEMINI\n\n*[1 generated image]*' in md



def test_pre_in_list_item_and_attachment_only_prompt():
    assert strip_html('<ul><li><pre><code>a\nb</code></pre></li></ul>') == '- ```\n  a\n  b\n  ```'
    act = parse_activity(_entry(title='Prompted ', attachedFiles=['brief.pdf'], safeHtmlItem=None))
    md = activity_to_markdown(group_into_conversations([act])[0])
    assert '### USER' in md and '*Attachments: brief.pdf*' in md

if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('all gemini_extractor checks passed')
