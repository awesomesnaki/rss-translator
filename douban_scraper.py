"""豆瓣 subject_collection 爬虫

调用 m.douban.com 移动端内部 JSON API 抓取榜单条目，
生成带海报图、完整 metadata、剧情简介、热门短评的 RSS feed。

API:
- 列表: https://m.douban.com/rexxar/api/v2/subject_collection/{slug}/items
- 详情: https://m.douban.com/rexxar/api/v2/movie/{id}
- 短评: https://m.douban.com/rexxar/api/v2/movie/{id}/interests?order_by=hot

海报图直接下载到 feeds/images/{name}/ 目录，由 GitHub Pages 自托管，
绕过 doubanio 在 RSS 客户端里的防盗链拦截。

环境变量 DOUBAN_COOKIE 可选，账号 cookie 用于绕过反爬。
"""

import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from html import escape

import requests
from feedgenerator import Rss201rev2Feed


COLLECTIONS = [
    {
        'slug': 'movie_weekly_best',
        'name': 'douban-movie-weekly',
        'title': '豆瓣 一周口碑电影榜',
    },
    {
        'slug': 'tv_global_best_weekly',
        'name': 'douban-tv-weekly',
        'title': '豆瓣 全球口碑剧集榜',
    },
]

UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
      'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
      'Mobile/15E148 Safari/604.1')

GH_PAGES_BASE = 'https://awesomesnaki.github.io/rss-translator'

COMMENT_LIMIT = 5


def make_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA,
        'Accept': 'application/json, text/plain, */*',
    })
    cookie = os.environ.get('DOUBAN_COOKIE')
    if cookie:
        s.headers['Cookie'] = cookie
    return s


def fetch_items(s, slug):
    url = f'https://m.douban.com/rexxar/api/v2/subject_collection/{slug}/items'
    resp = s.get(
        url,
        headers={'Referer': f'https://m.douban.com/subject_collection/{slug}'},
        params={'start': 0, 'count': 20, 'for_mobile': 1},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get('subject_collection_items', [])


def fetch_detail(s, item):
    """从详情接口取完整 metadata，失败返回空 dict。"""
    subject_id = item.get('id')
    if not subject_id:
        return {}
    url = f'https://m.douban.com/rexxar/api/v2/movie/{subject_id}'
    try:
        resp = s.get(
            url,
            headers={'Referer': item.get('url') or 'https://m.douban.com/'},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  详情失败 {subject_id}: {e}")
        return {}


def fetch_short_comments(s, item, limit=COMMENT_LIMIT):
    """从 interests 接口取热门短评 (有评论文字的)。"""
    subject_id = item.get('id')
    if not subject_id:
        return []
    url = f'https://m.douban.com/rexxar/api/v2/movie/{subject_id}/interests'
    try:
        resp = s.get(
            url,
            headers={'Referer': item.get('url') or 'https://m.douban.com/'},
            params={'count': 20, 'start': 0, 'order_by': 'hot'},
            timeout=15,
        )
        resp.raise_for_status()
        all_interests = resp.json().get('interests', [])
        with_text = [c for c in all_interests if (c.get('comment') or '').strip()]
        return with_text[:limit]
    except Exception as e:
        print(f"  短评失败 {subject_id}: {e}")
        return []


def names_of(lst):
    """从 [{'name': ...}, ...] 提取名字列表。"""
    if not lst:
        return []
    out = []
    for x in lst:
        if isinstance(x, dict) and x.get('name'):
            out.append(x['name'])
        elif isinstance(x, str):
            out.append(x)
    return out


def extract_tags(item, detail):
    """从 item / detail 提取榜单页底部展示的标签（奖项 + 内容）。

    豆瓣不同接口的字段名不太一致，多个候选都试一遍，去重后返回。
    """
    out = []
    seen = set()
    candidates = [
        ('honor_infos', ('title', 'name')),
        ('tags', ('name', 'title')),
        ('subject_tags', ('name', 'title')),
        ('topic_tags', ('name', 'title')),
        ('content_tags', ('name', 'title')),
    ]
    for src in (item, detail):
        if not isinstance(src, dict):
            continue
        for field, keys in candidates:
            for t in src.get(field) or []:
                if isinstance(t, dict):
                    name = next((t[k] for k in keys if t.get(k)), None)
                else:
                    name = str(t)
                if name and name not in seen:
                    out.append(name)
                    seen.add(name)
    return out


def stars_of(rating):
    v = (rating or {}).get('value', 0)
    if not isinstance(v, (int, float)) or v <= 0:
        return ''
    full = max(1, min(5, int(round(v))))
    return '⭐' * full


def image_ext(url):
    ext = url.rsplit('?', 1)[0].rsplit('.', 1)[-1].lower()
    return ext if ext in ('jpg', 'jpeg', 'png', 'webp') else 'jpg'


def manage_images(s, items, images_dir):
    """下载本期新图、清理离榜旧图。返回 {subject_id: filename}"""
    images_dir.mkdir(parents=True, exist_ok=True)
    name_map = {}

    for item in items:
        sid = str(item.get('id') or '')
        url = (item.get('pic') or {}).get('large') or item.get('cover_url') or ''
        if not sid or not url:
            continue
        filename = f'{sid}.{image_ext(url)}'
        filepath = images_dir / filename
        name_map[sid] = filename

        if filepath.exists():
            continue
        try:
            resp = s.get(
                url,
                headers={'Referer': item.get('url') or 'https://m.douban.com/'},
                timeout=30,
            )
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
            print(f"  下载: {filename}")
        except Exception as e:
            print(f"  图片失败 {sid}: {e}")
            del name_map[sid]

    keep = set(name_map.values())
    for f in images_dir.iterdir():
        if f.is_file() and f.name not in keep:
            f.unlink()
            print(f"  清理: {f.name}")

    return name_map


def build_title(item):
    parts = [item.get('title', '')]
    orig = item.get('original_title')
    if orig and orig != item.get('title'):
        parts.append(f'/ {orig}')
    if item.get('year'):
        parts.append(f'({item["year"]})')
    rating = (item.get('rating') or {}).get('value')
    if rating:
        parts.append(f'⭐{rating}')
    return ' '.join(p for p in parts if p)


def info_block(detail, item):
    """metadata 块：每个字段一行，标签加粗。"""
    year = detail.get('year') or item.get('year')
    countries = detail.get('countries') or []
    genres = detail.get('genres') or []
    directors = names_of(detail.get('directors'))
    actors = names_of(detail.get('actors') or detail.get('casts'))
    languages = detail.get('languages') or []
    durations = detail.get('durations') or []
    pubdate = detail.get('pubdate') or detail.get('pubdates') or []
    aka = detail.get('aka') or []
    imdb = detail.get('imdb') or ''

    rows = []

    def row(label, value):
        if not value:
            return
        if isinstance(value, list):
            value = ' / '.join(str(v) for v in value if v)
        rows.append(f'<strong>{escape(label)}</strong>: {escape(str(value))}')

    row('时间', year)
    row('地区', countries)
    row('类型', genres)
    row('导演', directors)
    row('主演', actors[:5])
    row('语言', languages)
    row('片长', durations)
    row('上映', pubdate[:3])
    row('又名', aka[:3])
    row('标签', extract_tags(item, detail)[:10])
    if imdb:
        rows.append(
            f'<strong>IMDb</strong>: '
            f'<a href="https://www.imdb.com/title/{escape(imdb)}/">'
            f'{escape(imdb)}</a>'
        )

    if not rows:
        subtitle = item.get('card_subtitle', '')
        return f'<p>{escape(subtitle)}</p>' if subtitle else ''

    return '<p>' + '<br>'.join(rows) + '</p>'


def comments_block(comments):
    if not comments:
        return ''
    parts = ['<hr><h3>短评</h3>']
    for i, c in enumerate(comments):
        if i > 0:
            parts.append('<br>')
        user = (c.get('user') or {}).get('name') or '匿名'
        stars = stars_of(c.get('rating'))
        votes = c.get('vote_count', 0)
        text = (c.get('comment') or '').strip()

        meta = [f'<strong>{escape(user)}</strong>']
        if stars:
            meta.append(stars)
        parts.append(f'<p>{" · ".join(meta)}</p>')

        if text:
            parts.append(f'<p>{escape(text)}</p>')
        if votes:
            parts.append(f'<p><strong>👍 {votes}</strong></p>')
    return '\n'.join(parts)


def build_description(item, detail, comments, image_url):
    parts = []

    if image_url:
        parts.append(
            f'<p><img src="{escape(image_url)}" referrerpolicy="no-referrer"></p>'
        )

    rating = item.get('rating') or detail.get('rating') or {}
    if rating.get('value'):
        parts.append(
            f'<h3>⭐ {rating["value"]} '
            f'<small>（{rating.get("count", 0)} 人评价）</small></h3>'
        )
    elif item.get('null_rating_reason'):
        parts.append(f'<p>{escape(item["null_rating_reason"])}</p>')

    info = info_block(detail, item)
    if info:
        parts.append(info)

    intro = (detail.get('intro') or '').strip()
    if intro:
        parts.append(f'<p><strong>剧情</strong>: {escape(intro)}</p>')

    cb = comments_block(comments)
    if cb:
        parts.append(cb)

    return '\n'.join(parts)


def image_url_for(item, collection_name, name_map):
    sid = str(item.get('id') or '')
    filename = name_map.get(sid)
    if filename:
        return f'{GH_PAGES_BASE}/feeds/images/{collection_name}/{filename}'
    return (item.get('pic') or {}).get('large') or item.get('cover_url') or ''


def build_feed(s, collection, items, name_map):
    feed = Rss201rev2Feed(
        title=collection['title'],
        link=f'https://m.douban.com/subject_collection/{collection["slug"]}',
        description=collection['title'],
        language='zh-CN',
    )
    now = datetime.now(timezone.utc)
    for item in items:
        detail = fetch_detail(s, item)
        time.sleep(0.5)
        comments = fetch_short_comments(s, item)
        time.sleep(0.5)
        feed.add_item(
            title=build_title(item),
            link=item.get('url', ''),
            description=build_description(
                item, detail, comments,
                image_url_for(item, collection['name'], name_map),
            ),
            unique_id=str(item.get('id') or item.get('url') or ''),
            pubdate=now,
        )
    return feed


def main():
    output_dir = Path('feeds')
    output_dir.mkdir(exist_ok=True)
    s = make_session()

    failed = []
    for col in COLLECTIONS:
        try:
            print(f"抓取: {col['title']}")
            items = fetch_items(s, col['slug'])
            print(f"  获得 {len(items)} 条")
            if not items:
                failed.append(col['name'])
                continue

            images_dir = output_dir / 'images' / col['name']
            name_map = manage_images(s, items, images_dir)

            feed = build_feed(s, col, items, name_map)
            output_file = output_dir / f"{col['name']}.xml"
            with open(output_file, 'w', encoding='utf-8') as f:
                feed.write(f, 'utf-8')
            print(f"  写入: {output_file}")
        except Exception as e:
            print(f"  失败: {e}")
            failed.append(col['name'])

    if failed:
        print(f"\n失败: {failed}")
        sys.exit(1)


if __name__ == '__main__':
    main()
