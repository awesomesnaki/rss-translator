"""翻译瘦身 + 段落级缓存的离线测试：python3 test_segment_cache.py

不依赖网络，也不会调用 DeepSeek API（用一个保留标签结构、给每段文字加「译」字的假翻译器）。
"""
import os

os.environ.setdefault('DEEPSEEK_API_KEY', 'dummy-for-tests')
from bs4 import BeautifulSoup, Comment
import translate_rss as T

T.time.sleep = lambda s: None
CALLS = []


def fake_translate(text):
    CALLS.append(text)
    soup = BeautifulSoup(text, 'html.parser')
    for s in soup.find_all(string=True):
        if not isinstance(s, Comment) and s.strip():
            s.replace_with('译' + s)
    return str(soup)


T.translate_with_deepseek = fake_translate


def para(i, extra=''):
    return (f'<p class="body-text c{i}" data-pid="{i}">Paragraph {i} talks about the desk, the lamp and '
            f'the long walk to the studio every morning{extra}.</p>')


def article(n=12, figure_after=(), caption=False, wrapper_class='post-body', extra=None):
    parts = []
    for i in range(n):
        parts.append(para(i, (extra or {}).get(i, '')))
        if i in figure_after:
            cap = f'<figcaption>Photo {i} of the desk.</figcaption>' if caption else ''
            parts.append(f'<figure><img src="https://cdn.test/{i}.jpg" alt=""/>{cap}</figure>')
    return f'<div class="{wrapper_class}">' + ''.join(parts) + '</div>'


def run(html, cache):
    CALLS.clear()
    return T.translate_html_content(html, cache)


def sent_text():
    return ''.join(BeautifulSoup(c, 'html.parser').get_text() for c in CALLS)


def fresh():
    return T.TranslationCache({})


def test_existing_whole_article_cache_still_hits():
    # 上线时已在 feed 窗口里的文章：整篇缓存（原始 HTML 的 hash）照常命中，不会全量重翻
    cache = fresh()
    html = article()
    cache[T.get_hash(html.strip())] = '<p>旧译文</p>'
    assert run(html, cache) == '<p>旧译文</p>' and CALLS == []


def test_new_article_is_batched_and_fully_translated():
    cache = fresh()
    out = run(article(12), cache)
    assert 1 <= len(CALLS) <= 2, len(CALLS)
    assert out.count('<p>') == 12 and out.count('译Paragraph') == 12
    assert 'class=' not in ''.join(CALLS) and 'data-pid' not in ''.join(CALLS)   # 瘦身后才送翻


def test_markup_only_change_needs_no_api_call():
    """Substack / Squarespace 改模板：class、data-*、包裹层、注释变了，正文一字没改"""
    cache = fresh()
    run(article(12), cache)
    changed = article(12, wrapper_class='post-body v2 theme-dark').replace(
        'class="body-text', 'data-new="1" class="paragraph body-text').replace('</p>', '</p><!-- r42 -->')
    run(changed, cache)
    assert CALLS == []


def test_inserted_paragraph_only_sends_the_new_one():
    """边用边记的长文（basicappleguy iPhone Duo）：中间插一段，以前插入点之后整篇重翻"""
    cache = fresh()
    run(article(12), cache)
    html = article(12).replace(para(6), para(6) + '<p>Brand new thoughts on the hinge after a week.</p>')
    out = run(html, cache)
    assert len(CALLS) == 1 and 'Brand new thoughts' in sent_text() and 'Paragraph' not in sent_text()
    assert out.count('译Paragraph') == 12 and '译Brand new thoughts' in out


def test_typo_fix_only_sends_that_paragraph():
    """DaringFireball：Gruber 改了第 3 段的一个词"""
    cache = fresh()
    run(article(12), cache)
    run(article(12, extra={3: ' (updated)'}), cache)
    assert len(CALLS) == 1 and 'Paragraph 3' in sent_text() and 'Paragraph 4' not in sent_text()


def test_template_adds_figcaptions_only_figures_are_resent():
    """basicappleguy 10/15：Squarespace 给所有图片加了 figcaption，以前 5 篇整篇重翻"""
    cache = fresh()
    run(article(12, figure_after=(2, 5, 9)), cache)
    run(article(12, figure_after=(2, 5, 9), caption=True), cache)
    assert CALLS and 'Paragraph' not in sent_text() and sent_text().count('Photo') == 3


def test_image_only_segments_are_not_sent():
    cache = fresh()
    html = '<div><figure><img src="https://cdn.test/a.jpg"/></figure><hr/><p><img src="https://cdn.test/b.jpg"/></p></div>'
    out = run(html, cache)
    assert CALLS == [] and 'a.jpg' in out and 'b.jpg' in out


def test_misaligned_translation_falls_back_to_whole_batch():
    """模型把几段合并成一段：对不上就整包原样用，不按段缓存，也不多调用"""
    cache = fresh()
    original = T.translate_with_deepseek
    T.translate_with_deepseek = lambda text: (CALLS.append(text) or
                                              '<p>' + BeautifulSoup(text, 'html.parser').get_text() + '</p>')
    try:
        out = run(article(4), cache)
    finally:
        T.translate_with_deepseek = original
    assert len(CALLS) == 1 and out.count('<p>') == 1 and 'Paragraph 3' in out
    assert not any(k.startswith(T.SEGMENT_CACHE_PREFIX) for k in cache._store)
    assert T.get_hash(article(4).strip()) in cache       # 整篇缓存照常写，下次直接命中


def test_failed_batch_is_retried_alone_next_run():
    cache = fresh()
    original = T.translate_with_deepseek
    T.translate_with_deepseek = lambda text: ((CALLS.append(text) or None) if 'Paragraph 11 ' in text
                                              else fake_translate(text))
    try:
        out = run(article(40), cache)                      # 40 段约 5000 字，分成两包
    finally:
        T.translate_with_deepseek = original
    assert len(CALLS) == 2
    assert 'Paragraph 11 talks' in out and '译Paragraph 39' in out   # 失败的那包用原文顶上，另一包正常
    assert T.get_hash(article(40).strip()) not in cache    # 半中半英不进整篇缓存
    run(article(40), cache)                                # 下次只重试失败的那包
    assert len(CALLS) == 1 and 'Paragraph 11 ' in sent_text() and 'Paragraph 39' not in sent_text()


def test_inline_text_without_paragraph_tags_stays_one_segment():
    # 有的源正文没有 <p>，是裸文字 + <br> + <a>：不能被拆成碎片分开翻（会破坏句子）
    segs = T.split_segments('Hello <a href="https://x.test">world</a>,<br/>second line.<p>Block</p>tail')
    assert [s[0] for s in segs] == ['#inline', 'p', '#inline']
    assert segs[0][1] == 'Hello <a href="https://x.test">world</a>,<br/>second line.'


def test_slim_html_keeps_meaning_drops_noise():
    html = ('<div class="x" style="color:red"><script>track()</script><style>.a{}</style>'
            '<p class="c" id="p1" data-attrs="{&quot;a&quot;:1}"><span class="s">Hi</span> '
            '<a href="https://example.test/x" target="_blank" rel="noopener">link</a></p>'
            '<picture><source srcset="https://cdn.test/a.webp 1x"/><img src="https://cdn.test/a.jpg" alt="A"/></picture>'
            '<a href="https://substackcdn.com/image/fetch/big.png"><img data-src="https://cdn.test/lazy.png" src="data:," srcset="x 2x"/></a>'
            '<a href="https://apps.apple.com/app/id1"><img src="https://cdn.test/icon.png"/></a>'
            '<!-- comment --><table><tr><td colspan="2">cell</td></tr></table></div>')
    out = T.slim_html(html)
    for gone in ('class=', 'style=', 'track()', '.a{}', 'data-attrs', 'id="p1"', 'target=', 'rel=',
                 '<source', 'srcset', 'comment', 'substackcdn.com/image', '<span', '<div'):
        assert gone not in out, (gone, out)
    for kept in ('<p>Hi <a href="https://example.test/x">link</a></p>', '<img alt="A" src="https://cdn.test/a.jpg"/>',
                 'data-src="https://cdn.test/lazy.png"', 'href="https://apps.apple.com/app/id1"', 'colspan="2"'):
        assert kept in out, (kept, out)


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} 项全部通过 ✅")
