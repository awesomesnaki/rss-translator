"""没有 RSS 的站点：直接调站点自己的数据接口，生成 RSS XML，交给 translate_rss 的主流程。

config.yaml 里 url 写成 `{scheme}://{参数}`（scheme 在 SOURCES 里注册），之后过滤、
图片修复、HTML 清理、输出都和普通 feed 走同一套流程。

为什么不按 CSS 选择器抓网页（FetchRSS / RSSHub transform 那一路）：这类站点多是
Nuxt / Next 前端渲染，列表卡片常常连 href 都没有，靠点击事件跳转，选择器抓不到链接；
而页面背后的 JSON 接口直接给出完整正文 HTML，比 readability 从网页里抠正文稳定得多，
也不需要 fetch_full_content。
"""
import html as html_lib
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

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


# ---------------------------------------------------------------------------
# Workspaces workspaces.xyz（每周六一期的桌面布置访谈）
#
# 原来托管在 beehiiv，2026 年 7 月搬到自建的 Astro 站 workspaces.xyz，旧的
# rss.beehiiv.com feed 停在 #534 不再更新，新站没有 RSS。正文存在 Sanity CMS 里，
# 数据集公开可读，一条 GROQ 查询就能拿到最近几期的全部结构化字段，不用逐篇请求：
#   https://{project}.apicdn.sanity.io/v2021-10-21/data/query/{dataset}?query=...
# setup 文档字段：issueNumber / slug / publishedAt / guestName / guestTitle / guestLocation /
#   guestBio(Portable Text) / guestLinks[{platform,url}] / photos[{asset._ref,alt,caption}] /
#   qa[{question,answer(Portable Text)}] / gear[{category,name,description,affiliateUrl}] /
#   software[{name,description,url}]
# 不取的字段：censusFacts（站内统计冷知识）、featuredGear（带购买按钮的推广块）、
#   heroQuote / pullQuote（从问答里摘出来的重复引语）。
#
# 正文由 JSON 确定性地拼出来，每次运行输入完全一致，翻译缓存稳定命中。旧 beehiiv 源恰恰
# 相反：正文 footer 里有个每天变的 `publication_logo=...?v=时间戳`，整篇 hash 天天变，
# 停更的 5 篇旧文每天被全量重翻。
# 图片在 cdn.sanity.io，不校验 Referer，不需要自托管。
# ---------------------------------------------------------------------------
WORKSPACES_SITE = 'https://workspaces.xyz'
WORKSPACES_SANITY_PROJECT = 'ui5qde1a'
WORKSPACES_SANITY_DATASET = 'production'
WORKSPACES_API = (f'https://{WORKSPACES_SANITY_PROJECT}.apicdn.sanity.io/v2021-10-21'
                  f'/data/query/{WORKSPACES_SANITY_DATASET}')
WORKSPACES_LIST_SIZE = 10
WORKSPACES_IMAGE_WIDTH = 1600
WORKSPACES_QUERY = (
    '*[_type == "setup" && defined(slug.current) && publishedAt <= now()]'
    ' | order(publishedAt desc)[0...%d]'
    '{issueNumber, publishedAt, "slug": slug.current, guestName, guestTitle, guestLocation,'
    ' guestBio, guestLinks, photos, qa, gear, software}'
)
# Portable Text 的装饰 mark → HTML 标签（未知 mark 直接忽略，只保留文字）
_PT_MARKS = {'strong': 'strong', 'em': 'em', 'code': 'code', 'underline': 'u',
             'strike-through': 's', 'highlight': 'strong'}


def _esc(s):
    return html_lib.escape(s or '', quote=True)


def _pt_span(span, mark_defs):
    text = _esc(span.get('text')).replace('\n', '<br/>')
    if not text:
        return ''
    for mark in span.get('marks') or []:
        d = mark_defs.get(mark)
        if d is not None:
            if d.get('_type') == 'link' and d.get('href'):
                text = f'<a href="{_esc(d["href"])}">{text}</a>'
        elif mark in _PT_MARKS:
            tag = _PT_MARKS[mark]
            text = f'<{tag}>{text}</{tag}>'
    return text


def _pt_html(blocks):
    """Portable Text → HTML。站点实际只用到普通段落、bullet 列表和 link/strong/em"""
    if isinstance(blocks, str):
        # 少数早期条目的 guestBio 是纯文本，空行分段
        return ''.join(f'<p>{_esc(p.strip())}</p>' for p in re.split(r'\n\s*\n', blocks) if p.strip())
    out, open_list = [], None
    for b in blocks or []:
        if b.get('_type') != 'block':
            continue
        defs = {d.get('_key'): d for d in b.get('markDefs') or []}
        inner = ''.join(_pt_span(c, defs) for c in b.get('children') or [] if c.get('_type') == 'span')
        if not inner.strip():
            continue
        list_tag = {'bullet': 'ul', 'number': 'ol'}.get(b.get('listItem'))
        if list_tag != open_list:
            if open_list:
                out.append(f'</{open_list}>')
            if list_tag:
                out.append(f'<{list_tag}>')
            open_list = list_tag
        if list_tag:
            out.append(f'<li>{inner}</li>')
        else:
            tag = {'h2': 'h3', 'h3': 'h4', 'h4': 'h4', 'blockquote': 'blockquote'}.get(b.get('style'), 'p')
            out.append(f'<{tag}>{inner}</{tag}>')
    if open_list:
        out.append(f'</{open_list}>')
    return ''.join(out)


def _workspaces_image_url(asset_ref):
    # image-{id}-{w}x{h}-{ext} → https://cdn.sanity.io/images/{project}/{dataset}/{id}-{w}x{h}.{ext}
    m = re.fullmatch(r'image-([0-9a-f]+-\d+x\d+)-(\w+)', asset_ref or '')
    if not m:
        return None
    return (f'https://cdn.sanity.io/images/{WORKSPACES_SANITY_PROJECT}/{WORKSPACES_SANITY_DATASET}'
            f'/{m.group(1)}.{m.group(2)}?w={WORKSPACES_IMAGE_WIDTH}&fit=max&auto=format')


def _workspaces_photo(photo):
    src = _workspaces_image_url((photo.get('asset') or {}).get('_ref'))
    if not src:
        return ''
    caption = (photo.get('caption') or '').strip()
    figcaption = f'<figcaption>{_esc(caption)}</figcaption>' if caption else ''
    return f'<figure><img src="{_esc(src)}" alt="{_esc(photo.get("alt"))}"/>{figcaption}</figure>'


def _workspaces_link_text(url):
    return re.sub(r'^https?://(www\.)?', '', url).rstrip('/')


def _workspaces_body(setup):
    """排版照网站：头图 → 头衔/简介/链接 → 每段问答后跟一张照片 → 剩余照片 → 装备 → 软件"""
    photos = [p for p in (_workspaces_photo(p) for p in setup.get('photos') or []) if p]
    parts = []
    if photos:
        parts.append(photos.pop(0))

    headline = ' · '.join(_esc(x) for x in (setup.get('guestTitle'), setup.get('guestLocation')) if x)
    if headline:
        parts.append(f'<p><strong>{headline}</strong></p>')
    parts.append(_pt_html(setup.get('guestBio')))
    links = [
        f'{_esc(l.get("platform") or "Link")} → <a href="{_esc(l["url"])}">{_esc(_workspaces_link_text(l["url"]))}</a>'
        for l in setup.get('guestLinks') or [] if l.get('url')
    ]
    if links:
        parts.append('<p>' + '<br/>'.join(links) + '</p>')

    for qa in setup.get('qa') or []:
        question = (qa.get('question') or '').strip()
        if question:
            parts.append(f'<h3>{_esc(question)}</h3>')
        parts.append(_pt_html(qa.get('answer')))
        if photos:
            parts.append(photos.pop(0))
    parts.extend(photos)

    gear = []
    for g in setup.get('gear') or []:
        name = _esc(g.get('name'))
        if not name:
            continue
        if g.get('affiliateUrl'):
            name = f'<a href="{_esc(g["affiliateUrl"])}">{name}</a>'
        line = f'{_esc(g["category"])}: {name}' if g.get('category') else name
        if g.get('description'):
            line += f' — {_esc(g["description"])}'
        gear.append(f'<li>{line}</li>')
    if gear:
        parts.append('<h3>Gear</h3><ul>' + ''.join(gear) + '</ul>')

    software = []
    for s in setup.get('software') or []:
        name = _esc(s.get('name'))
        if not name:
            continue
        if s.get('url'):
            name = f'<a href="{_esc(s["url"])}">{name}</a>'
        if s.get('description'):
            name += f' — {_esc(s["description"])}'
        software.append(f'<li>{name}</li>')
    if software:
        parts.append('<h3>Software</h3><ul>' + ''.join(software) + '</ul>')
    return ''.join(p for p in parts if p)


def _iso_time(s):
    try:
        return datetime.fromisoformat((s or '').replace('Z', '+00:00'))
    except ValueError:
        return None


def workspaces_setups(_param, feed_config):
    query = WORKSPACES_QUERY % WORKSPACES_LIST_SIZE
    data = _get_json(f'{WORKSPACES_API}?query={quote(query)}', {'User-Agent': UA})
    setups = data.get('result') or []
    if not setups:
        raise RuntimeError('Workspaces Sanity 接口返回空列表')

    feed = Rss201rev2Feed(
        title='Workspaces',
        link=f'{WORKSPACES_SITE}/',
        description='A weekly tour of real desk setups from designers, developers, and founders.',
        language='en',
    )
    for s in setups:
        link = f'{WORKSPACES_SITE}/p/{s["slug"]}'
        if not s.get('qa') and not s.get('photos'):
            print(f'  跳过 {link}（没有问答也没有照片）')
            continue
        # 标题保留英文：人名交给翻译会被不稳定地音译甚至瞎猜中文名，
        # config 里配 translate_title: false；中文头衔/所在地在正文开头
        name = (s.get('guestName') or s['slug']).strip()
        title = f'#{s["issueNumber"]} {name}' if s.get('issueNumber') else name
        if s.get('guestTitle'):
            title += f' · {s["guestTitle"].strip()}'
        feed.add_item(
            title=title,
            link=link,
            description=_workspaces_body(s),
            pubdate=_iso_time(s.get('publishedAt')),
            unique_id=link,
        )
    return feed.writeString('utf-8')


SOURCES = {
    'pubg': pubg_news,
    'workspaces': workspaces_setups,
}


def build_source_feed(url, feed_config):
    """url 命中已注册的 scheme 时返回生成的 RSS XML 字符串，否则返回 None"""
    scheme, sep, rest = url.partition('://')
    if sep and scheme in SOURCES:
        return SOURCES[scheme](rest, feed_config)
    return None
