"""没有 RSS 的站点：直接调站点自己的数据接口，生成 RSS XML，交给 translate_rss 的主流程。

config.yaml 里 url 写成 `{scheme}://{参数}`（scheme 在 SOURCES 里注册），之后过滤、
图片修复、HTML 清理、输出都和普通 feed 走同一套流程。

为什么不按 CSS 选择器抓网页（FetchRSS / RSSHub transform 那一路）：这类站点多是
Nuxt / Next 前端渲染，列表卡片常常连 href 都没有，靠点击事件跳转，选择器抓不到链接；
而页面背后的 JSON 接口直接给出完整正文 HTML，比 readability 从网页里抠正文稳定得多，
也不需要 fetch_full_content。
"""
import time
from datetime import datetime, timezone

import requests
from feedgenerator import Rss201rev2Feed

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'


def _get_json(url, headers, attempts=3):
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            if i == attempts - 1:
                raise
            print(f"  请求失败重试 ({i + 1}): {url} — {e}")
            time.sleep(2 * (i + 1))


# ---------------------------------------------------------------------------
# PUBG 官网新闻 pubg.com/{lang}/news
#
# 官网是 Nuxt 渲染，新闻卡片没有 href。数据来自 Krafton 内容接口 api-foc.krafton.com，
# 必须带 Service-Namespace / Service-Game 头，否则 401 NOT_FOUND_NAMESPACE。
#   列表: /content/post/news?lang=zh-cn&displayLocationType=NORMAL&size=20&page=1
#         按 displayStartTime 倒序，字段有 postId / title / summary / category / landingType / images
#   详情: /content/post/news/{postId}?lang=zh-cn  → post.contents[] 里按 lang 取 content（完整正文 HTML）
# 时间字段是 UTC（官网前端拼上 "Z" 再格式化）。
# landingType=LINK 的条目是外链活动页，没有正文，只用摘要 + 封面。
# ---------------------------------------------------------------------------
PUBG_API = 'https://api-foc.krafton.com/content/post/news'
PUBG_HEADERS = {
    'User-Agent': UA,
    'Service-Namespace': 'PUBG_OFFICIAL',
    'Service-Game': 'pubg',
    'Origin': 'https://pubg.com',
    'Referer': 'https://pubg.com/',
}
PUBG_CATEGORIES = {
    'patch_notes': '更新公告',
    'notice': '通知',
    'dev_notes': '开发者日志',
    'labs': '实验室',
}
PUBG_LIST_SIZE = 20


def _pubg_time(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _pubg_cover(post):
    for img in post.get('images') or []:
        if img.get('imageUrl'):
            return img['imageUrl']
    return None


def _pubg_body(content, cover, embeds):
    """正文前补封面图（详情正文里通常没有列表封面），末尾补视频外链"""
    parts = []
    if cover and cover.rsplit('/', 1)[-1] not in content:
        parts.append(f'<p><img src="{cover}" alt=""></p>')
    parts.append(content)
    for emb in embeds or []:
        if emb.get('url'):
            parts.append(f'<p>视频：<a href="{emb["url"]}">{emb.get("type") or emb["url"]}</a></p>')
    return '\n'.join(parts)


def pubg_news(lang, feed_config):
    lang = lang.strip('/') or 'zh-cn'
    page_url = f'https://pubg.com/{lang}/news'
    # 标题被 filter_out 排除的条目不必请求详情（主流程还会再过滤一遍）
    skip = feed_config.get('filter_out', [])

    data = _get_json(
        f'{PUBG_API}?lang={lang}&displayLocationType=NORMAL&size={PUBG_LIST_SIZE}&page=1',
        PUBG_HEADERS,
    )
    posts = (data.get('_embedded') or {}).get('post') or []
    if not posts:
        raise RuntimeError('PUBG 新闻接口返回空列表')

    feed = Rss201rev2Feed(
        title='PUBG 新闻',
        link=page_url,
        description='PUBG: BATTLEGROUNDS 官网新闻',
        language=lang,
    )
    for post in posts:
        title = (post.get('title') or '').strip()
        if any(kw in title for kw in skip):
            continue
        post_id = post['postId']
        cover = _pubg_cover(post)

        if post.get('landingType') == 'LINK' and post.get('landingUrl'):
            link = post['landingUrl']
            body = _pubg_body(f"<p>{post.get('summary') or ''}</p>", cover, post.get('embeds'))
        else:
            link = f'{page_url}/{post_id}'
            try:
                detail = _get_json(f'{PUBG_API}/{post_id}?lang={lang}', PUBG_HEADERS)
            except (requests.RequestException, ValueError) as e:
                # 跳过而不是回退成摘要：阅读器一般不会更新已收录条目的正文，
                # 先发摘要就永远是摘要了。下次运行再补上
                print(f"  跳过 {post_id}（详情获取失败: {e}）")
                continue
            contents = (detail.get('post') or {}).get('contents') or []
            content = next((c for c in contents if c.get('lang') == lang), contents[0] if contents else {})
            html = content.get('content')
            if not html:
                print(f"  跳过 {post_id}（详情无正文）")
                continue
            body = _pubg_body(html, cover, content.get('embeds') or post.get('embeds'))
            time.sleep(0.3)

        category = PUBG_CATEGORIES.get(post.get('category'), post.get('category'))
        feed.add_item(
            title=title,
            link=link,
            description=body,
            pubdate=_pubg_time(post.get('displayStartTime')) or _pubg_time(post.get('createdAt')),
            unique_id=f'{page_url}/{post_id}',
            categories=[category] if category else None,
        )
    return feed.writeString('utf-8')


SOURCES = {
    'pubg': pubg_news,
}


def build_source_feed(url, feed_config):
    """url 命中已注册的 scheme 时返回生成的 RSS XML 字符串，否则返回 None"""
    scheme, sep, rest = url.partition('://')
    if sep and scheme in SOURCES:
        return SOURCES[scheme](rest, feed_config)
    return None
