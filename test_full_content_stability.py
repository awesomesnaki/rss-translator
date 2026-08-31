"""全文抓取稳定性的回归测试：python3 test_full_content_stability.py

守的是历史上最大的 token 黑洞——fetch_full_content 每次抓回来的 HTML 有抖动，
hash 一变翻译缓存就全部失效，同一篇文章每次运行都要重新翻译一遍。
不依赖网络，也不会调用 DeepSeek API。
"""
import os, random

os.environ.setdefault('DEEPSEEK_API_KEY', 'dummy-for-tests')
import translate_rss as T

URL = 'https://example.test/blog/the-trail-less-maintained/'
BODY = "".join(f"<p>Paragraph {i} about a hike through the woods and up the ridge.</p>\n"
               for i in range(60))
FULL = f'<div class="entry-content" itemprop="articleBody">\n{BODY}</div>'


def jitter(html):
    """模板噪音：包裹层 class 变了、末尾多了个渲染时间戳注释，正文文字其实没变"""
    n = random.randint(1000, 9999)
    return html.replace('entry-content', f'entry-content post-{n}') + f'\n<!-- rendered {n} -->'


def truncate(html, frac):
    return html[:int(len(html) * frac)] + '</div>'


def count_translations(sequence, warm=None):
    """按抓取序列跑一遍，返回实际需要调用 API 的次数（缓存未命中的次数）"""
    store = T.ContentStore({})
    seen = set()
    if warm:
        T.stable_full_content(URL, warm, None, store)
        seen.add(T.get_hash(warm))
    calls = 0
    for fetched in sequence:
        content, _ = T.stable_full_content(URL, fetched, None, store)
        if content and T.get_hash(content) not in seen:
            seen.add(T.get_hash(content))
            calls += 1
    return calls


def test_markup_jitter_does_not_retranslate():
    random.seed(7)
    assert count_translations([jitter(FULL) for _ in range(12)], warm=FULL) == 0


def test_flapping_fetches_do_not_retranslate():
    """franzgraf 实测模式：正文长度在 0 / 半截 / 完整之间反复横跳"""
    random.seed(11)
    seq = []
    for _ in range(12):
        r = random.random()
        seq.append(None if r < .25 else
                   (truncate(FULL, random.uniform(.07, .65)) if r < .55 else jitter(FULL)))
    assert count_translations(seq, warm=FULL) == 0


def test_real_update_is_retranslated():
    """作者补了一段 Update，长度只多 1%，仍然要重新翻译"""
    grown = FULL.replace('</div>', '<p>Update: the trail was cleared the next weekend.</p></div>')
    assert count_translations([jitter(FULL), grown, jitter(grown)], warm=FULL) == 1


def test_consistent_shrink_is_eventually_accepted():
    """正文真的被删短了：连续抓到同样的短正文，确认够次数后接受"""
    short = truncate(FULL, .3)
    assert count_translations([short] * T.FULL_CONTENT_SHRINK_CONFIRMATIONS, warm=FULL) == 1
    # 少一次确认就不该接受，否则一次抓取降级就能顶掉正文
    assert count_translations([short] * (T.FULL_CONTENT_SHRINK_CONFIRMATIONS - 1), warm=FULL) == 0


def test_failed_fetch_keeps_previous_content_and_cover():
    store = T.ContentStore({})
    T.stable_full_content(URL, FULL, 'https://example.test/cover.jpg', store)
    content, cover = T.stable_full_content(URL, None, None, store)
    assert content == FULL and cover == 'https://example.test/cover.jpg'


def test_blocked_page_detected():
    assert T.looks_blocked('<div id="cf-wrapper"><div class="p-0" id="cf-error-details">')
    assert not T.looks_blocked('<p>a normal article about a cloudflare outage</p>')


def test_refusal_never_reaches_cache():
    assert T.looks_like_refusal('请提供需要翻译的完整英文内容，目前我只收到了标题“The Trail”')
    assert not T.looks_like_refusal('<p>他请提供了一份详细的路线图。</p>')
    cache = T.TranslationCache({
        'poisoned': {'v': '请提供需要翻译的英文内容，我会按照您的要求进行处理。', 't': '2099-01-01'},
        'good': {'v': '正常译文', 't': '2099-01-01'},
    })
    assert 'poisoned' not in cache and 'good' in cache


def test_content_store_retention():
    store = T.ContentStore({'old': {'c': 'x', 'i': '', 't': '2000-01-01', 'p': '', 'n': 0}})
    store.put('fresh', 'y', None)
    assert list(store.to_json()) == ['fresh']


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} 项全部通过 ✅")
