"""豆瓣 subject_collection 爬虫

调用 m.douban.com 移动端内部 JSON API 抓取榜单条目，
生成带海报图、评分、演职员、剧情简介的 RSS feed。

API:
- 列表: https://m.douban.com/rexxar/api/v2/subject_collection/{slug}/items
- 详情: https://m.douban.com/rexxar/api/v2/movie/{id}

海报图直接下载到 feeds/images/{name}/ 目录，由 GitHub Pages 自托管，
绕过 doubanio 在 RSS 客户端里的防盗链拦截，不依赖第三方代理。

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
]

UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
      'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
      'Mobile/15E148 Safari/604.1')

GH_PAGES_BASE = 'https://awesomesnaki.github.io/rss-translator'


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


def fetch_intro(s, item):
    subject_id = item.get('id')
    if not subject_id:
        return ''
    url = f'https://m.douban.com/rexxar/api/v2/movie/{subject_id}'
    try:
        resp = s.get(
            url,
            headers={'Referer': item.get('url') or 'https://m.douban.com/'},
            timeout=15,
        )
        resp.raise_for_status()
        return (resp.json().get('intro') or '').strip()
    except Exception as e:
        print(f"  详情失败 {subject_id}: {e}")
        return ''


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
        parts.append(f'★{rating}')
    return ' '.join(p for p in parts if p)


def build_description(item, intro, image_url):
    parts = []

    if image_url:
        parts.append(
            f'<p><img src="{escape(image_url)}" referrerpolicy="no-referrer"></p>'
        )

    rating = item.get('rating') or {}
    if rating.get('value'):
        parts.append(
            f'<p>★ <strong>{rating["value"]}</strong> '
            f'（{rating.get("count", 0)} 人评价）</p>'
        )
    elif item.get('null_rating_reason'):
        parts.append(f'<p>{escape(item["null_rating_reason"])}</p>')

    subtitle = item.get('card_subtitle', '')
    if subtitle:
        parts.append(f'<p>{escape(subtitle)}</p>')

    if intro:
        parts.append(f'<p>{escape(intro)}</p>')

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
        intro = fetch_intro(s, item)
        time.sleep(1)
        feed.add_item(
            title=build_title(item),
            link=item.get('url', ''),
            description=build_description(
                item, intro, image_url_for(item, collection['name'], name_map)
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
