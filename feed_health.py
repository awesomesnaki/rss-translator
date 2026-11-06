"""Feed 健康检查：发现源停更、连续失败，告警写进 feeds/health.xml，在 RSS 阅读器里订阅即可。

为什么需要：源坏掉时这个项目不会报错，只是 feed 不再出新条目，阅读器里看起来和「作者最近没写」
一模一样。workspaces 搬家后停更了 4 个月、AnthropicNews 的第三方 RSSHub 实例冻结了 3 周，
都是靠人肉觉得「好像很久没更新」才发现。

两类问题：
- 停更（stale）：上游很久没有新条目。阈值按各源自己的发文节奏算：
  max(发文间隔中位数 × 4, 90 分位间隔 × 2)，限制在 14–60 天；节奏未知时 30 天。
  config.yaml 里 `stale_days: N` 可覆盖，0 表示不检查。只看中位数的话，「一阵密集、一阵停很久」
  的作者（dereksivers、appaddict、pixelfed）中位数只有两三天，用各源上游的完整历史回测，
  每年要误报约 24 次；加上 90 分位后降到约 12 次，日更源（DF、Anthropic）仍然是 14 天就报。
  「有没有新条目」以上游条目链接为准（出现没见过的链接 = 有新内容），比 pubDate 可靠：
  有的源 pubDate 缺失、乱序（旧 beehiiv feed 把 2023 年的文章混在最前面），甚至天天被填成
  抓取时间。只有第一次观察某个源时参考上游最新一条的 pubDate，这样已经停更的源上线当天
  就能报出来；之后活跃时间只随新链接刷新。
- 连续失败（failing）：处理这个源一直抛异常（含上游返回空列表）超过 24 小时。
  只失败一两次多半是网络抖动，不报。

每个问题只在出现时报一次，没恢复的话每 7 天提醒一次。连续失败恢复时发一条「已恢复」（失败是悄悄自愈的，不说就不知道）；停更恢复不发——新文章本身就会出现在阅读器里。

状态存在 health.json（CI 每次全新 clone，只能靠提交的文件持久化）。只在有事件时才变化
（出现新条目、失败、告警），不会让没有实际变化的运行也产生提交。
"""
import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedgenerator import Rss201rev2Feed

STATE_FILE = Path('health.json')
FEED_FILE = Path('feeds') / 'health.xml'
FEED_NAME = 'health'                    # 占用的输出文件名，config.yaml 里不要再用

STALE_CADENCE_FACTOR = 4         # × 发文间隔中位数
STALE_P90_FACTOR = 2             # × 90 分位间隔
STALE_MIN_DAYS = 14
STALE_MAX_DAYS = 60
STALE_DEFAULT_DAYS = 30
FAIL_ALERT_HOURS = 24
REMIND_DAYS = 7
MAX_ALERT_ITEMS = 30
SEEN_LIMIT = 200
NEW_TIMES_LIMIT = 20

REPO_URL = 'https://github.com/awesomesnaki/rss-translator'
ACTIONS_URL = f'{REPO_URL}/actions/workflows/translate.yml'


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


def _parse(s):
    return datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc) if s else None


def _link_key(link):
    return hashlib.md5(link.encode()).hexdigest()[:12]


def cadence_days(dates):
    """发文间隔的 (中位数, 90 分位)，单位天。少于 3 个不同日期时返回 None"""
    ds = sorted(set(dates), reverse=True)
    gaps = sorted(g for g in ((a - b).total_seconds() / 86400 for a, b in zip(ds, ds[1:])) if g > 0)
    if len(gaps) < 2:
        return None
    return statistics.median(gaps), gaps[min(len(gaps) - 1, int(len(gaps) * 0.9))]


def stale_threshold_days(feed_config, cadence):
    """停更阈值（天），0 表示不检查。cadence 是 cadence_days() 的结果"""
    override = feed_config.get('stale_days')
    if override is not None:
        return override
    if cadence is None:
        return STALE_DEFAULT_DAYS
    median, p90 = cadence
    return min(max(median * STALE_CADENCE_FACTOR, p90 * STALE_P90_FACTOR, STALE_MIN_DAYS), STALE_MAX_DAYS)


def entry_report(entries, now):
    """从 feedparser 条目里取健康检查需要的信息（过滤之前的全部上游条目）"""
    links, dates = [], []
    for e in entries:
        link = e.get('link') or e.get('id') or e.get('title')
        if link:
            links.append(link)
        parsed = e.get('published_parsed') or e.get('updated_parsed')
        if parsed:
            d = datetime(*parsed[:6], tzinfo=timezone.utc)
            if d <= now + timedelta(days=1):       # 未来时间多半是源站时区写错，不算
                dates.append(d)
    return {'links': links, 'dates': dates}


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def _fmt_days(d):
    return f'{d:.0f}' if d >= 10 else f'{d:.1f}'.rstrip('0').rstrip('.')


def _alert(state, now, feed, kind, title, body, link):
    seq = sum(1 for a in state['alerts'] if a['feed'] == feed and a['kind'] == kind)
    state['alerts'].insert(0, {
        'id': f'health:{feed}:{kind}:{_iso(now)}:{seq}',
        'feed': feed, 'kind': kind, 'time': _iso(now),
        'title': title, 'body': body, 'link': link,
    })
    del state['alerts'][MAX_ALERT_ITEMS:]


def _stale_body(name, url, info):
    rows = []
    if info['last_new']:
        rows.append(f"最后一次出现新条目：{info['last_new'][:10]}")
    if info['upstream_latest']:
        rows.append(f"上游最新一条的发布时间：{info['upstream_latest'][:10]}")
    if info['cadence'] is not None:
        rows.append(f"这个源平时大约每 {_fmt_days(info['cadence'][0])} 天更新一次，"
                    f"超过 {_fmt_days(info['threshold'])} 天没有新内容就会报警")
    else:
        rows.append(f"这个源的更新节奏未知，按默认 {info['threshold']} 天报警")
    rows.append(f'源地址：<a href="{url}">{url}</a>')
    rows.append('常见原因：网站改版或搬家（workspaces 就是这样从 beehiiv 搬走的）、第三方 RSSHub '
                '实例停止刷新（AnthropicNews 遇到过）、RSSHub 路由失效、作者确实停更了。'
                f'确认是作者停更的话，在 config.yaml 里给 {name} 加 <code>stale_days</code> 调大阈值，'
                '或设为 0 关闭检查。')
    return ''.join(f'<p>{r}</p>' for r in rows)


def _fail_body(s):
    return (f"<p>从 {s['fail_since'][:16].replace('T', ' ')} UTC 开始，连续 {s['fail_runs']} 次运行处理失败，"
            f"这段时间 feed 一直停留在最后一次成功时的内容。</p>"
            f"<p>最近一次的错误：<code>{_esc(s['last_error'])}</code></p>"
            f'<p>详细日志见 <a href="{ACTIONS_URL}">GitHub Actions</a>。</p>')


def _esc(s):
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def update(state, feeds_config, reports, now):
    """用本次运行的结果更新健康状态，返回更新后的 state。

    reports: {feed名: {'ok': bool, 'error': str, 'links': [...], 'dates': [...]}}
             不在 reports 里的源（本次没跑）保持原状。
    """
    first_run = state is None
    state = state or {'feeds': {}, 'alerts': []}
    names = {f['name'] for f in feeds_config}
    for gone in set(state['feeds']) - names:            # 从 config 删掉的源不再跟踪
        del state['feeds'][gone]

    for fc in feeds_config:
        name = fc['name']
        r = reports.get(name)
        if r is None:
            continue
        s = state['feeds'].setdefault(name, {
            'since': _iso(now), 'seen': None, 'activity': None, 'last_new': None, 'new_times': [],
            'fail_since': None, 'fail_runs': 0, 'last_error': '', 'incidents': {},
        })
        link = fc.get('url', '')
        conditions = {}

        if r['ok']:
            s['fail_since'], s['fail_runs'], s['last_error'] = None, 0, ''
            keys = [_link_key(l) for l in r['links']][:SEEN_LIMIT]
            upstream_latest = max(r['dates']) if r['dates'] else None
            if s['seen'] is None:
                # 第一次观察：还不知道哪些是新条目，只能先信上游最新一条的 pubDate
                s['activity'] = _iso(min(upstream_latest or now, now))
            elif set(keys) - set(s['seen']):
                s['last_new'] = s['activity'] = _iso(now)
                s['new_times'] = [_iso(now)] + s['new_times'][:NEW_TIMES_LIMIT - 1]
            # 之后只有出现新链接才刷新活跃时间，不再看 pubDate：有的源（部分 RSSHub 路由）
            # 缺日期时把 pubDate 填成抓取时间，链接没变、日期却天天是「今天」
            s['seen'] = keys

            cadence = cadence_days(r['dates'])
            if cadence is None and len(s['new_times']) >= 3:
                cadence = cadence_days([_parse(t) for t in s['new_times']])
            threshold = stale_threshold_days(fc, cadence)
            activity = _parse(s['activity'])
            age = (now - activity).total_seconds() / 86400
            if threshold and age > threshold:
                conditions['stale'] = {
                    'since': activity, 'age': age, 'threshold': threshold, 'cadence': cadence,
                    'last_new': s['last_new'], 'upstream_latest': _iso(upstream_latest),
                }
        else:
            s['fail_since'] = s['fail_since'] or _iso(now)
            s['fail_runs'] += 1
            s['last_error'] = (r.get('error') or '')[:300]
            hours = (now - _parse(s['fail_since'])).total_seconds() / 3600
            if hours >= FAIL_ALERT_HOURS and s['fail_runs'] >= 2:
                conditions['failing'] = {'since': _parse(s['fail_since']), 'hours': hours}
            # 失败期间拿不到上游数据，停更状态保持原样，不开也不关

        for kind, info in conditions.items():
            inc = s['incidents'].get(kind)
            if inc and (now - _parse(inc['alerted'])) < timedelta(days=REMIND_DAYS):
                continue
            n = inc['n'] + 1 if inc else 1
            prefix = '' if n == 1 else f'（第 {n} 次提醒）'
            if kind == 'stale':
                title = f"⚠️ {name} 已 {info['age']:.0f} 天没有新内容{prefix}"
                body, alert_link = _stale_body(name, link, info), link
            else:
                title = f"❌ {name} 已连续失败 {info['hours']:.0f} 小时{prefix}"
                body, alert_link = _fail_body(s), ACTIONS_URL
            _alert(state, now, name, kind, title, body, alert_link)
            s['incidents'][kind] = {'since': _iso(info['since']), 'alerted': _iso(now), 'n': n}

        for kind in list(s['incidents']):
            if kind in conditions or (kind == 'stale' and not r['ok']):
                continue
            inc = s['incidents'].pop(kind)
            if kind == 'failing':           # 停更恢复不通知：新文章本身就会出现在阅读器里
                hours = (now - _parse(inc['since'])).total_seconds() / 3600
                _alert(state, now, name, 'failing-recovered', f'✅ {name} 已恢复',
                       f'<p>从 {inc["since"][:16].replace("T", " ")} UTC 开始的处理失败持续了约 '
                       f'{hours:.0f} 小时，现在已恢复正常。</p>', ACTIONS_URL)

    if first_run:
        _alert(state, now, '*', 'enabled', f'✅ Feed 健康检查已启用，正在监控 {len(names)} 个源',
               f'<p>源停更或连续失败超过 {FAIL_ALERT_HOURS} 小时时，这里会出现一条告警；'
               f'问题持续的话每 {REMIND_DAYS} 天提醒一次，处理失败恢复后再发一条通知。</p>'
               f'<p>停更阈值按各源自己的发文节奏计算（发文间隔中位数 × {STALE_CADENCE_FACTOR} 与 '
               f'90 分位间隔 × {STALE_P90_FACTOR} 取大，限制在 {STALE_MIN_DAYS}–{STALE_MAX_DAYS} 天），config.yaml 里用 '
               f'<code>stale_days</code> 覆盖，设为 0 关闭。</p>', REPO_URL)
    return state


def open_incidents(state):
    for name, s in sorted((state or {}).get('feeds', {}).items()):
        for kind, inc in s['incidents'].items():
            yield name, kind, inc


def write_feed(state, path=FEED_FILE):
    feed = Rss201rev2Feed(
        title='RSS Translator 健康检查',
        link=REPO_URL,
        description='源停更、连续失败时的告警',
        language='zh-CN',
    )
    for a in state['alerts']:
        feed.add_item(title=a['title'], link=a['link'], description=a['body'],
                      pubdate=_parse(a['time']), unique_id=a['id'])
    with open(path, 'w', encoding='utf-8') as f:
        feed.write(f, 'utf-8')


def run(feeds_config, reports, now=None):
    """主流程调用入口：更新状态、写 health.json 和 feeds/health.xml，并在 Actions 日志里打出未恢复的问题"""
    if any(fc['name'] == FEED_NAME for fc in feeds_config):
        raise ValueError(f'config.yaml 里的源不能叫 {FEED_NAME}，会和健康检查的输出 feeds/{FEED_NAME}.xml 冲突')
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    state = update(load_state(), feeds_config, reports, now)
    save_state(state)
    write_feed(state)
    for name, kind, inc in open_incidents(state):
        what = '停更' if kind == 'stale' else '连续失败'
        print(f"::warning title=Feed 健康检查::{name} {what}（自 {inc['since'][:10]} 起）")
    return state
