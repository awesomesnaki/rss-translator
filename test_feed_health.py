"""feed_health 的离线测试：python3 test_feed_health.py

不依赖网络，也不会调用 DeepSeek API。
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault('DEEPSEEK_API_KEY', 'dummy-for-tests')
import feed_health as H

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
DAILY = {'name': 'daily', 'url': 'https://daily.test/feed'}
SLOW = {'name': 'slow', 'url': 'https://slow.test/feed'}


def days_ago(n, now=T0):
    return now - timedelta(days=n)


def ok(links, dates):
    return {'ok': True, 'links': links, 'dates': dates}


def fail(msg='boom'):
    return {'ok': False, 'error': msg}


def kinds(state, feed=None):
    return [a['kind'] for a in state['alerts'] if feed is None or a['feed'] == feed]


def test_threshold_follows_cadence():
    assert H.stale_threshold_days({}, (0.1, 1.2)) == 14       # 日更（DF）：下限 14 天
    assert H.stale_threshold_days({}, (8, 10)) == 32          # 周更左右：中位数 × 4
    assert H.stale_threshold_days({}, (2.1, 13.8)) == 27.6    # 爆发式更新（dereksivers）：90 分位 × 2
    assert H.stale_threshold_days({}, (89, 318)) == 60        # 几个月一篇（marco）：上限 60 天
    assert H.stale_threshold_days({}, None) == 30             # 节奏未知
    assert H.stale_threshold_days({'stale_days': 0}, (1, 1)) == 0
    assert H.stale_threshold_days({'stale_days': 90}, (1, 1)) == 90
    assert H.cadence_days([days_ago(0), days_ago(3), days_ago(6), days_ago(9)]) == (3, 3)
    assert H.cadence_days([days_ago(0), days_ago(3)]) is None


def test_first_run_catches_already_dead_feed():
    # AnthropicNews 的情况：上游一直返回同一批旧条目，平时约 3 天一篇，最新一条已是 19 天前
    dead = {'name': 'dead', 'url': 'https://dead.test'}
    dates = [days_ago(19), days_ago(22), days_ago(23), days_ago(29)]
    state = H.update(None, [DAILY, dead], {
        'daily': ok(['d1', 'd2', 'd3'], [days_ago(0), days_ago(1), days_ago(2)]),
        'dead': ok(['x1', 'x2', 'x3', 'x4'], dates),
    }, T0)
    assert sorted(kinds(state)) == ['enabled', 'stale']
    stale = next(a for a in state['alerts'] if a['kind'] == 'stale')
    assert stale['feed'] == 'dead' and stale['title'].startswith('⚠️ dead 已 19 天没有新内容')
    assert 'dead' in state['feeds'] and state['feeds']['daily']['incidents'] == {}


def test_new_links_are_the_signal_and_recovery_is_reported():
    state = H.update(None, [SLOW], {'slow': ok(['a', 'b', 'c'], [days_ago(40), days_ago(41), days_ago(42)])}, T0)
    assert kinds(state, 'slow') == ['stale']
    # pubDate 乱写成「现在」也骗不过：链接没变就不算有新内容
    later = T0 + timedelta(days=1)
    state = H.update(state, [SLOW], {'slow': ok(['a', 'b', 'c'], [later])}, later)
    assert 'stale' in state['feeds']['slow']['incidents'] and kinds(state, 'slow') == ['stale']
    # 出现新链接 → 恢复（停更恢复不发通知，新文章本身就会出现在阅读器里）
    later2 = T0 + timedelta(days=2)
    state = H.update(state, [SLOW], {'slow': ok(['new', 'a', 'b'], [later2, days_ago(40)])}, later2)
    assert kinds(state, 'slow') == ['stale']
    assert state['feeds']['slow']['incidents'] == {}
    assert state['feeds']['slow']['last_new'] == H._iso(later2)


def test_stale_after_threshold_using_observed_links():
    state = H.update(None, [DAILY], {'daily': ok(['1', '2', '3'], [days_ago(0), days_ago(1), days_ago(2)])}, T0)
    state = H.update(state, [DAILY], {'daily': ok(['4', '1', '2'], [days_ago(0)])}, T0)   # 新条目
    assert state['feeds']['daily']['last_new'] == H._iso(T0)
    t = T0 + timedelta(days=13)
    state = H.update(state, [DAILY], {'daily': ok(['4', '1', '2'], [days_ago(0), days_ago(1), days_ago(2)])}, t)
    assert kinds(state, 'daily') == []
    t = T0 + timedelta(days=15)
    state = H.update(state, [DAILY], {'daily': ok(['4', '1', '2'], [days_ago(0), days_ago(1), days_ago(2)])}, t)
    assert kinds(state, 'daily') == ['stale']


def test_failures_need_24_hours_and_two_runs():
    state = H.update(None, [DAILY], {'daily': ok(['1', '2', '3'], [days_ago(0), days_ago(1), days_ago(2)])}, T0)
    for h in (1, 6, 23):
        state = H.update(state, [DAILY], {'daily': fail('HTTP 503')}, T0 + timedelta(hours=h))
    assert kinds(state, 'daily') == []
    state = H.update(state, [DAILY], {'daily': fail('HTTP 503')}, T0 + timedelta(hours=25))
    assert kinds(state, 'daily') == ['failing']
    assert 'HTTP 503' in state['alerts'][0]['body'] and '连续 4 次' in state['alerts'][0]['body']
    # 短时间内连续失败两次也不报：多半是网络抖动
    s2 = H.update(None, [DAILY], {'daily': fail()}, T0)
    s2 = H.update(s2, [DAILY], {'daily': fail()}, T0)
    assert kinds(s2, 'daily') == []
    # 恢复
    state = H.update(state, [DAILY], {'daily': ok(['1', '2', '3'], [days_ago(0)])}, T0 + timedelta(hours=30))
    assert kinds(state, 'daily') == ['failing-recovered', 'failing']
    assert state['feeds']['daily']['fail_runs'] == 0


def test_reminder_every_7_days_no_spam_between():
    state = H.update(None, [SLOW], {'slow': ok(['a', 'b', 'c'], [days_ago(70), days_ago(80), days_ago(90)])}, T0)
    for d in range(1, 7):
        state = H.update(state, [SLOW], {'slow': ok(['a', 'b', 'c'], [days_ago(70)])}, T0 + timedelta(days=d))
    assert kinds(state, 'slow') == ['stale']
    state = H.update(state, [SLOW], {'slow': ok(['a', 'b', 'c'], [days_ago(70)])}, T0 + timedelta(days=7))
    assert kinds(state, 'slow') == ['stale', 'stale']
    assert '第 2 次提醒' in state['alerts'][0]['title']


def test_failure_does_not_close_stale_incident():
    state = H.update(None, [SLOW], {'slow': ok(['a', 'b', 'c'], [days_ago(70), days_ago(80), days_ago(90)])}, T0)
    state = H.update(state, [SLOW], {'slow': fail()}, T0 + timedelta(hours=1))
    assert 'stale' in state['feeds']['slow']['incidents']
    assert kinds(state, 'slow') == ['stale']


def test_disabled_and_removed_feeds():
    quiet = {'name': 'quiet', 'url': 'https://q.test', 'stale_days': 0}
    state = H.update(None, [quiet, SLOW], {
        'quiet': ok(['a', 'b', 'c'], [days_ago(300), days_ago(400), days_ago(500)]),
        'slow': ok(['a'], []),
    }, T0)
    assert kinds(state, 'quiet') == []
    state = H.update(state, [SLOW], {'slow': ok(['a'], [])}, T0)        # quiet 从 config 删掉了
    assert 'quiet' not in state['feeds']


def test_feed_without_dates_counts_from_tracking_start():
    # pixelfed-geraldlanger 这类条目没有日期的源：从开始监控算，节奏未知按 30 天
    state = H.update(None, [SLOW], {'slow': ok(['a'], [])}, T0)
    state = H.update(state, [SLOW], {'slow': ok(['a'], [])}, T0 + timedelta(days=29))
    assert kinds(state, 'slow') == []
    state = H.update(state, [SLOW], {'slow': ok(['a'], [])}, T0 + timedelta(days=31))
    assert kinds(state, 'slow') == ['stale']


def test_feed_and_state_files_roundtrip(tmp='/tmp/feed_health_test'):
    import feedparser, json
    os.makedirs(tmp, exist_ok=True)
    state = H.update(None, [SLOW], {'slow': ok(['a', 'b', 'c'], [days_ago(70), days_ago(80), days_ago(90)])}, T0)
    H.write_feed(state, path=f'{tmp}/health.xml')
    parsed = feedparser.parse(f'{tmp}/health.xml')
    assert sorted(e.title.split(' ')[0] for e in parsed.entries) == ['⚠️', '✅']
    assert len({e.id for e in parsed.entries}) == 2
    json.loads(json.dumps(state))       # 状态可以序列化
    # 同样的状态生成的 XML 完全一致，不会制造无意义的提交
    H.write_feed(state, path=f'{tmp}/health2.xml')
    assert open(f'{tmp}/health.xml').read() == open(f'{tmp}/health2.xml').read()


def test_reserved_name_is_rejected():
    try:
        H.run([{'name': 'health', 'url': 'x'}], {})
    except ValueError:
        return
    raise AssertionError('config 里叫 health 的源应该被拒绝')


def test_empty_upstream_is_a_failure_not_an_empty_feed():
    import translate_rss as T
    original = T.build_source_feed
    T.build_source_feed = lambda url, cfg: '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    try:
        name, success, report = T.process_single_feed(
            {'name': 'empty', 'url': 'fake://x', 'translate': False}, T.TranslationCache({}),
            T.ContentStore({}), __import__('pathlib').Path('/tmp/feed_health_test'))
    finally:
        T.build_source_feed = original
    assert not success and not report['ok'] and '上游 feed 为空' in report['error']
    assert not os.path.exists('/tmp/feed_health_test/empty.xml')


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} 项全部通过 ✅")
