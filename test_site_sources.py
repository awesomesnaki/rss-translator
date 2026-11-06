"""site_sources 的离线测试：python3 test_site_sources.py

不依赖网络（数据接口被 mock 掉），也不会调用 DeepSeek API。
"""
import os

os.environ.setdefault('DEEPSEEK_API_KEY', 'dummy-for-tests')
import site_sources as S
import translate_rss as T

T.time.sleep = lambda s: None


def block(*children, list_item=None, mark_defs=None):
    b = {'_type': 'block', 'style': 'normal', 'markDefs': mark_defs or [],
         'children': [{'_type': 'span', 'text': t, 'marks': m} for t, m in children]}
    if list_item:
        b['listItem'], b['level'] = list_item, 1
    return b


def photo(n, caption=None):
    p = {'_type': 'image', 'alt': f'alt {n}',
         'asset': {'_ref': f'image-{n:040x}-2000x1500-jpg', '_type': 'reference'}}
    if caption:
        p['caption'] = caption
    return p


SETUP = {
    'issueNumber': 551, 'publishedAt': '2026-10-31T13:00:00.000Z', 'slug': '551-jane-doe',
    'guestName': 'Jane Doe', 'guestTitle': 'Product Designer', 'guestLocation': 'London, UK',
    'guestBio': [block(('Jane builds ', []), ('Slate', ['l1']), ('.', []),
                       mark_defs=[{'_key': 'l1', '_type': 'link', 'href': 'https://slate.test'}])],
    'guestLinks': [{'platform': 'Website', 'url': 'https://jane.test/'}],
    'photos': [photo(1, 'Hero shot.'), photo(2, 'Close-up.'), photo(3)],
    'qa': [
        {'question': 'Q one?', 'answer': [block(('Answer ', []), ('one', ['strong']))]},
        {'question': 'Q two?', 'answer': [block(('item a', []), list_item='bullet'),
                                          block(('item b', []), list_item='bullet'),
                                          block(('after list', []))]},
    ],
    'gear': [{'category': 'Monitor', 'name': 'Studio Display', 'description': '5K',
              'affiliateUrl': 'https://amzn.test/x'},
             {'category': 'Chair', 'name': 'Aeron'}],
    'software': [{'name': 'Figma', 'description': 'Design', 'url': 'https://figma.test'}],
}


def with_api(result):
    original = S._get_json
    S._get_json = lambda url, headers, attempts=3: {'result': result}
    return original


def test_portable_text():
    html = S._pt_html(SETUP['guestBio'] + SETUP['qa'][1]['answer'])
    assert html == ('<p>Jane builds <a href="https://slate.test">Slate</a>.</p>'
                    '<ul><li>item a</li><li>item b</li></ul><p>after list</p>')
    assert S._pt_html(SETUP['qa'][0]['answer']) == '<p>Answer <strong>one</strong></p>'
    # 早期条目的 guestBio 是纯文本
    assert S._pt_html('First <para>.\n\nSecond.') == '<p>First &lt;para&gt;.</p><p>Second.</p>'


def test_image_url():
    url = S._workspaces_image_url('image-ab12-2000x1500-jpg')
    assert url == ('https://cdn.sanity.io/images/ui5qde1a/production/ab12-2000x1500.jpg'
                   '?w=1600&fit=max&auto=format')
    assert S._workspaces_image_url('file-ab12-pdf') is None
    assert S._workspaces_photo({'asset': {'_ref': 'garbage'}}) == ''


def test_body_layout_follows_site():
    body = S._workspaces_body(SETUP)
    order = [body.index(s) for s in (
        'Hero shot.', 'Product Designer · London, UK', 'Jane builds', 'jane.test</a>',
        'Q one?', 'Close-up.', 'Q two?', 'alt 3', '<h3>Gear</h3>', '<h3>Software</h3>')]
    assert order == sorted(order), body
    assert 'Monitor: <a href="https://amzn.test/x">Studio Display</a> — 5K' in body
    assert '<li>Chair: Aeron</li>' in body
    assert '<a href="https://figma.test">Figma</a> — Design' in body


def test_feed_items_and_skip_empty():
    empty = {'issueNumber': 1, 'slug': '1-empty', 'publishedAt': '2020-01-01T00:00:00Z'}
    original = with_api([SETUP, empty])
    try:
        import feedparser
        feed = feedparser.parse(S.build_source_feed('workspaces://setups', {}))
    finally:
        S._get_json = original
    assert len(feed.entries) == 1
    e = feed.entries[0]
    assert e.title == '#551 Jane Doe · Product Designer'
    assert e.link == 'https://workspaces.xyz/p/551-jane-doe'
    assert e.published_parsed[:5] == (2026, 10, 31, 13, 0)


def test_translation_is_stable_and_title_untranslated():
    """核心保证：同样的接口数据每次生成完全相同的正文，第二次运行零 API 调用；标题保留英文"""
    calls = []
    original_translate = T.translate_with_deepseek
    T.translate_with_deepseek = lambda text: calls.append(text) or f'ZH[{text}]'
    original = with_api([SETUP])
    try:
        cfg = {'name': 'workspaces', 'url': 'workspaces://setups', 'translate_title': False}
        cache, store = T.TranslationCache({}), T.ContentStore({})
        first = T.translate_feed(cfg, cache, store)
        assert calls, '首次运行应该需要翻译'
        assert '#551 Jane Doe · Product Designer' not in calls, '标题不该送去翻译'
        calls.clear()
        second = T.translate_feed(cfg, cache, store)
        assert calls == [], f'第二次运行不该有缓存未命中: {calls}'
        assert first.items[0]['description'] == second.items[0]['description']
        assert second.items[0]['title'] == '#551 Jane Doe · Product Designer'
    finally:
        S._get_json = original
        T.translate_with_deepseek = original_translate


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} 项全部通过 ✅")
