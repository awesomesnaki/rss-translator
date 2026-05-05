"""豆瓣 subject_collection 爬虫

调用 m.douban.com 移动端内部 JSON API 抓取榜单条目，
生成带海报图、评分、演职员的 RSS feed。

API: https://m.douban.com/rexxar/api/v2/subject_collection/{slug}/items
环境变量 DOUBAN_COOKIE 可选，账号 cookie 用于绕过反爬。
"""

import os
import sys
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


def fetch_items(slug):
    url = f'https://m.douban.com/rexxar/api/v2/subject_collection/{slug}/items'
    headers = {
        'User-Agent': UA,
        'Referer': f'https://m.douban.com/subject_collection/{slug}',
        'Accept': 'application/json, text/plain, */*',
    }
    cookie = os.environ.get('DOUBAN_COOKIE')
    if cookie:
        headers['Cookie'] = cookie

    resp = requests.get(
        url,
        headers=headers,
        params={'start': 0, 'count': 20, 'for_mobile': 1},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get('subject_collection_items', [])


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


def build_description(item):
    parts = []

    pic = (item.get('pic') or {}).get('large') or item.get('cover_url') or ''
    if pic:
        parts.append(
            f'<p><img src="{escape(pic)}" referrerpolicy="no-referrer"></p>'
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

    intro = item.get('intro') or ''
    if intro:
        parts.append(f'<p>{escape(intro)}</p>')

    return '\n'.join(parts)


def build_feed(collection, items):
    feed = Rss201rev2Feed(
        title=collection['title'],
        link=f'https://m.douban.com/subject_collection/{collection["slug"]}',
        description=collection['title'],
        language='zh-CN',
    )
    now = datetime.now(timezone.utc)
    for item in items:
        feed.add_item(
            title=build_title(item),
            link=item.get('url', ''),
            description=build_description(item),
            unique_id=str(item.get('id') or item.get('url') or ''),
            pubdate=now,
        )
    return feed


def main():
    output_dir = Path('feeds')
    output_dir.mkdir(exist_ok=True)

    failed = []
    for col in COLLECTIONS:
        try:
            print(f"抓取: {col['title']}")
            items = fetch_items(col['slug'])
            print(f"  获得 {len(items)} 条")
            if not items:
                failed.append(col['name'])
                continue

            feed = build_feed(col, items)
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
