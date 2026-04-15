import feedparser
import yaml
import os
import time
import hashlib
import json
import re
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString
from feedgenerator import Rss201rev2Feed
from datetime import datetime, timezone
from openai import OpenAI
import requests
from readability import Document

# DeepSeek API 配置（从环境变量读取，不会暴露在代码里）
client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

def load_config():
    with open('config.yaml', 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def load_cache():
    cache_file = Path('cache.json')
    if cache_file.exists():
        with open(cache_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open('cache.json', 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def get_hash(text):
    return hashlib.md5(text.encode()).hexdigest()

def translate_with_deepseek(text):
    """调用 DeepSeek API 翻译"""
    if not text or not text.strip():
        return text
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": "你是一个专业翻译。请将用户提供的英文内容翻译成流畅自然的中文。\n要求：\n1. 只输出翻译结果\n2. 完整保留所有HTML标签（如<p>、<strong>、<a>、<em>、<figure>等），只翻译标签内的文字\n3. 不要添加任何markdown格式符号（如**、*、#等）\n4. 保留所有链接URL和图片路径不变\n5. 保持原文的语气和风格"
                },
                {
                    "role": "user",
                    "content": text
                }
            ],
            temperature=0.3,
            max_tokens=8192
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"DeepSeek API 调用失败: {e}")
        return text

def translate_text(text, cache):
    """翻译纯文本，带缓存"""
    if not text or not text.strip():
        return text
    
    # 清理多余空白，但保留段落结构
    text = re.sub(r'[ \t]+', ' ', text)  # 多个空格/tab 合并
    text = re.sub(r'\n\s*\n', '\n\n', text)  # 多个空行合并
    text = text.strip()
    
    if not text:
        return text
    
    text_hash = get_hash(text)
    if text_hash in cache:
        return cache[text_hash]
    
    translated = translate_with_deepseek(text)
    cache[text_hash] = translated
    time.sleep(0.3)  # 避免请求过快
    return translated

def summarize_title_text(text, cache):
    """将长标题总结为简短的中文短语，带缓存"""
    if not text or not text.strip():
        return text

    cache_key = "summarize:" + get_hash(text.strip())
    if cache_key in cache:
        return cache[cache_key]

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": "你是一个标题总结助手。用户会给你一段图片帖子的长文字描述，请将其总结为一个简短的中文标题（一个短语或一句话，不超过15个字）。只输出标题本身，不要加引号或其他格式。"
                },
                {
                    "role": "user",
                    "content": text
                }
            ],
            temperature=0.3,
            max_tokens=100
        )
        result = response.choices[0].message.content.strip()
        cache[cache_key] = result
        time.sleep(0.3)
        return result
    except Exception as e:
        print(f"标题总结失败: {e}")
        return translate_text(text, cache)


def translate_html_direct(html_content, cache):
    """调用 API 翻译一段 HTML，保留标签结构"""
    if not html_content or not html_content.strip():
        return html_content

    content_hash = get_hash(html_content.strip())
    if content_hash in cache:
        return cache[content_hash]

    # 保护 img 标签不被翻译篡改（LLM 偶尔会改动 URL）
    img_store = {}
    def replace_img(match):
        idx = len(img_store)
        placeholder = f'<!-- IMG_{idx} -->'
        img_store[placeholder] = match.group(0)
        return placeholder
    protected = re.sub(r'<img\s[^>]*/?>', replace_img, html_content)

    translated = translate_with_deepseek(protected)

    # 还原 img 标签
    for placeholder, original in img_store.items():
        translated = translated.replace(placeholder, original)

    cache[content_hash] = translated
    time.sleep(0.3)
    return translated

def translate_html_content(html_content, cache):
    """
    整段翻译 HTML 内容，让 LLM 保留标签结构。
    短内容直接翻译，超长内容按段落分组翻译。
    """
    if not html_content or not html_content.strip():
        return html_content

    # 先查整段缓存
    content_hash = get_hash(html_content.strip())
    if content_hash in cache:
        return cache[content_hash]

    # 短内容（<=3000字符），直接整段翻译
    if len(html_content) <= 3000:
        translated = translate_html_direct(html_content, cache)
        cache[content_hash] = translated
        return translated

    # 长内容：按顶层元素分组，每组不超过 3000 字符
    soup = BeautifulSoup(html_content, 'html.parser')
    top_elements = list(soup.children)

    groups = []
    current = []
    current_len = 0

    for el in top_elements:
        el_str = str(el)
        if current_len + len(el_str) > 3000 and current:
            groups.append(current)
            current = [el]
            current_len = len(el_str)
        else:
            current.append(el)
            current_len += len(el_str)
    if current:
        groups.append(current)

    # 翻译每个分组
    translated_parts = []
    for group in groups:
        group_html = "".join(str(el) for el in group)
        translated_group = translate_html_direct(group_html, cache)
        translated_parts.append(translated_group)

    result = "".join(translated_parts)
    cache[content_hash] = result
    return result

def fix_image_tags(html_content):
    """修复图片：防盗链 + 懒加载还原"""
    if not html_content:
        return html_content
    soup = BeautifulSoup(html_content, 'html.parser')
    for img in soup.find_all('img'):
        # 防盗链：添加 no-referrer
        img['referrerpolicy'] = 'no-referrer'
        # 删除 srcset/sizes（RSS 里没用，且 srcset URL 常有防盗链问题）
        img.attrs.pop('srcset', None)
        img.attrs.pop('sizes', None)
        # 懒加载还原：把 data-src / data-original 等属性写回 src
        if not img.get('src') or 'placeholder' in img.get('src', '') or 'loading' in img.get('src', ''):
            for attr in ['data-src', 'data-original', 'data-lazy-src', 'data-actualsrc']:
                if img.get(attr):
                    img['src'] = img[attr]
                    break
    return str(soup)

def clean_readability_html(html_content):
    """清理 readability 提取的 HTML，精简为 RSS 阅读器友好的格式"""
    if not html_content:
        return html_content
    soup = BeautifulSoup(html_content, 'html.parser')
    # readability 通常会加一个外层 <div class="page"> 或 <html><body>，去掉它
    for tag_name in ['html', 'body']:
        tag = soup.find(tag_name)
        if tag:
            tag.unwrap()

    # 去掉 script / style / noscript 标签
    for tag in soup.find_all(['script', 'style', 'noscript']):
        tag.decompose()

    # <picture> → 简单 <img>（很多 RSS 阅读器不支持 picture/source）
    for picture in soup.find_all('picture'):
        img = picture.find('img')
        if img:
            img.extract()
            picture.replace_with(img)
        else:
            picture.decompose()

    # 去掉所有 CSS class 属性（没有样式表，class 毫无意义）
    for tag in soup.find_all(True):
        if tag.get('class'):
            del tag['class']

    # 展开纯包装的 <span>（没有任何属性的 span 只是多余嵌套）
    for span in soup.find_all('span'):
        if not span.attrs:
            span.unwrap()

    # 去掉空的 div 包装
    for div in soup.find_all('div'):
        if not div.attrs:
            div.unwrap()

    return str(soup)

def fetch_full_article(url):
    """从原文 URL 抓取完整文章内容，保留图片等 HTML 结构"""
    try:
        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding

        # V2EX 帖子用专用解析，避免 readability 乱排版
        if 'v2ex.com/t/' in url:
            content = extract_v2ex_content(resp.text)
            if content:
                return content

        # 博海拾贝（luobo8 源）用专用解析，readability 经常扒到打赏弹窗而不是正文
        if 'bohaishibei.com/post/' in url:
            content = extract_bohaishibei_content(resp.text)
            if content:
                return content

        doc = Document(resp.text)
        content = doc.summary()
        if content and content.strip():
            # 确认有真正的文字内容，而不只是空的 HTML 标签壳子
            text = BeautifulSoup(content, 'html.parser').get_text(strip=True)
            if text:
                return content
        print(f"  未提取到内容: {url}")
        return None
    except Exception as e:
        print(f"  抓取出错 {url}: {e}")
        return None

def extract_bohaishibei_content(html):
    """从博海拾贝文章页提取正文，去掉打赏弹窗、面包屑、分享按钮等站内装饰"""
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.find('article', id=re.compile(r'^post-\d+'))
    if not article:
        return None

    # 去掉打赏弹窗（里面有 wechat.gif / alipay.gif）
    for m in article.select('#myModal'):
        m.decompose()

    # 去掉 header（标题/作者/日期/分享按钮）
    for h in article.find_all('header'):
        h.decompose()

    # 去掉分享按钮容器
    for s in article.select('[data-sites]'):
        s.decompose()

    # 去掉打赏按钮（连同它的 <p> 父元素一起）
    for btn in article.select('#myBtn'):
        parent = btn.find_parent('p')
        if parent:
            parent.decompose()
        else:
            btn.decompose()

    # 去掉面包屑导航：指向首页/分类/作者页的链接
    for a in list(article.find_all('a')):
        href = a.get('href', '')
        if (href.rstrip('/') == 'https://www.bohaishibei.com'
                or '/post/category/' in href
                or '/post/author/' in href):
            a.decompose()

    # 去掉站内上传的图片（打赏二维码等装饰 gif 都在 wp-content/uploads 下）
    for img in article.find_all('img'):
        src = img.get('src', '')
        if 'bohaishibei.com/wp-content/uploads' in src:
            img.decompose()

    # 清掉空的 <i>（图标占位符）
    for i in article.find_all('i'):
        if not i.get_text(strip=True) and not i.find('img'):
            i.decompose()

    # 去掉底部版权/标签行（"本文来自网络..." + 标签链接），以及它后面的所有内容
    for el in list(article.descendants):
        if isinstance(el, NavigableString) and ('本文来自网络' in el or '转载请注明出处' in el):
            # 向上走到 article 的直接子元素
            anchor = el
            while anchor.parent is not None and anchor.parent is not article:
                anchor = anchor.parent
            # 删掉 anchor 后的所有兄弟节点
            for sib in list(anchor.next_siblings):
                if hasattr(sib, 'decompose'):
                    sib.decompose()
                else:
                    sib.extract()
            # 删掉 anchor 本身
            if hasattr(anchor, 'decompose'):
                anchor.decompose()
            else:
                anchor.extract()
            break

    # assets.bohaishibei.com 有防盗链（x-deny-reason: host_not_allowed），走图片代理中转
    for img in article.find_all('img'):
        src = img.get('src', '')
        if 'assets.bohaishibei.com' in src:
            img['src'] = f'https://images.weserv.nl/?url={quote(src, safe="")}'

    # 必须有实际文字或图片才算提取成功
    text = article.get_text(strip=True)
    imgs = article.find_all('img')
    if not text and not imgs:
        return None

    return str(article)

def extract_v2ex_content(html):
    """从 V2EX 帖子页面提取主帖和评论，生成干净的 HTML"""
    soup = BeautifulSoup(html, 'html.parser')
    parts = []

    # 提取主帖内容
    topic_content = soup.find('div', class_='topic_content')
    if topic_content:
        parts.append(str(topic_content))

    # 提取附加信息（有些帖子用 subtle 补充）
    for subtle in soup.find_all('div', class_='subtle'):
        parts.append('<hr/>' + str(subtle))

    # 提取评论
    reply_cells = soup.select('div[id^="r_"]')
    if reply_cells:
        parts.append('<hr/><h3>评论</h3>')
        for floor, cell in enumerate(reply_cells, 1):
            # 提取用户名
            username_tag = cell.select_one('strong a')
            username = username_tag.get_text() if username_tag else '匿名'

            # 提取评论内容
            reply_content = cell.find('div', class_='reply_content')
            if not reply_content:
                continue
            content_html = str(reply_content)

            # 提取感谢数（V2EX 点赞可能在 span.small 或其他位置）
            thank_text = ''
            for span in cell.find_all('span'):
                span_text = span.get_text().strip()
                num = re.search(r'(\d+)', span_text)
                if num and ('♥' in span_text or '❤' in span_text):
                    thank_text = f' ♥{num.group(1)}'
                    break

            parts.append(
                f'<p><strong>#{floor} {username}</strong>{thank_text}：{content_html}</p><hr/>'
            )

    if not parts:
        return None

    result = '\n'.join(parts)

    # 清理：移除头像图片（小于 100px 的或 class 含 avatar 的）
    result_soup = BeautifulSoup(result, 'html.parser')
    for img in result_soup.find_all('img'):
        classes = ' '.join(img.get('class', []))
        src = img.get('src', '')
        # 移除头像
        if 'avatar' in classes or 'avatar' in src:
            img.decompose()
            continue
        # 移除 V2EX 站内小图标（心形、感谢等）
        if '/static/' in src or 'heart' in src:
            img.decompose()
            continue

    # 移除感谢按钮区域
    for el in result_soup.find_all(class_=re.compile(r'thank|fade')):
        # 保留有实际文字内容的 small fade（感谢数），去掉按钮
        if el.find('a') or el.find('img'):
            el.decompose()

    return str(result_soup)

def resolve_feed_url(url):
    """解析 feed URL，处理 rsshub:// 协议"""
    rsshub_base = 'http://localhost:1200' if os.environ.get('GITHUB_ACTIONS') else 'https://rsshub.app'
    # rsshub://path 格式 → 转为 RSSHub 完整 URL（CI 中用本地实例，本地开发用 rsshub.app）
    if url.startswith('rsshub://'):
        return f"{rsshub_base}/{url[len('rsshub://'):]}"
    # 其他 URL（包括 rsshub.app 直链）原样返回，不做替换
    return url

def apply_entry_filter(entries, filter_type):
    """Filter feed entries based on configured filter type."""
    if filter_type == "blockquote":
        filtered = []
        for entry in entries:
            content = ''
            if 'content' in entry:
                content = entry.content[0].get('value', '')
            elif 'summary' in entry:
                content = entry.summary
            if re.match(r'\s*<blockquote', content):
                filtered.append(entry)
        return filtered
    else:
        print(f"  警告: 未知的过滤类型 '{filter_type}'，跳过过滤")
        return entries

def translate_feed(feed_config, cache):
    print(f"处理: {feed_config['name']}")
    url = resolve_feed_url(feed_config['url'])
    print(f"  URL: {url}")
    feed = feedparser.parse(url, request_headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    })

    should_translate = feed_config.get('translate', True)

    # 创建新 feed
    original_title = feed.feed.get('title', feed_config['name'])
    translated_feed = Rss201rev2Feed(
        title=f"{original_title} (中文)" if should_translate else original_title,
        link=feed.feed.get('link', ''),
        description=translate_text(feed.feed.get('description', ''), cache) if should_translate else feed.feed.get('description', ''),
        language='zh-CN'
    )
    
    # 只处理最新条目
    should_fetch_full = feed_config.get('fetch_full_content', False)

    # Apply entry filter if configured
    entry_filter = feed_config.get('filter')
    filter_out = feed_config.get('filter_out', [])
    filter_out_content = feed_config.get('filter_out_content', [])
    max_entries = 50 if (entry_filter or filter_out or filter_out_content) else 5
    entries = feed.entries[:max_entries]
    if entry_filter:
        entries = apply_entry_filter(entries, entry_filter)
        print(f"  过滤后保留 {len(entries)} 条 (类型: {entry_filter})")
    if filter_out:
        before = len(entries)
        entries = [e for e in entries if not any(kw in e.get('title', '') for kw in filter_out)]
        print(f"  标题过滤: {before} → {len(entries)} 条")
    if filter_out_content:
        before = len(entries)
        def get_entry_content(e):
            if 'content' in e:
                return e.content[0].get('value', '')
            return e.get('summary', '')
        entries = [e for e in entries if not any(kw in get_entry_content(e) for kw in filter_out_content)]
        print(f"  正文过滤: {before} → {len(entries)} 条")

    should_summarize_title = feed_config.get('summarize_title', False)
    should_images_only = feed_config.get('images_only', False)

    for entry in entries:
        original_title = entry.get('title', '')

        if should_summarize_title and should_translate:
            title = summarize_title_text(original_title, cache)
        elif should_translate:
            title = translate_text(original_title, cache)
        else:
            title = original_title

        # 处理内容
        content = ''
        if 'content' in entry:
            content = entry.content[0].get('value', '')
        elif 'summary' in entry:
            content = entry.summary

        # images_only 模式：正文只保留图片，去掉所有文字，跳过翻译
        if should_images_only:
            soup = BeautifulSoup(content, 'html.parser')
            imgs = soup.find_all('img')
            translated_content = ''.join(str(img) for img in imgs)
        # summarize_title 模式：只翻译标题文字放进正文，原有正文保留不翻译
        elif should_summarize_title and original_title.strip():
            translated_desc = translate_text(original_title, cache)
            content = f"<p>{translated_desc}</p>" + content
            translated_content = content
        else:
            # 如果配置了全文抓取，从原文 URL 获取完整内容
            if should_fetch_full and entry.get('link'):
                print(f"  抓取全文: {entry['link']}")
                full_content = fetch_full_article(entry['link'])
                if full_content:
                    content = full_content
                else:
                    print(f"  回退到 RSS 摘要")
                time.sleep(1)

            # 翻译 HTML 内容（保留标签结构）
            translated_content = translate_html_content(content, cache) if should_translate else content

        # 翻译后清理 HTML（放在翻译之后，避免改变 hash 导致缓存失效）
        translated_content = clean_readability_html(translated_content)

        # 修复图片防盗链问题
        translated_content = fix_image_tags(translated_content)

        # 获取发布时间
        pub_date = None
        if 'published_parsed' in entry and entry.published_parsed:
            pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        
        translated_feed.add_item(
            title=title,
            link=entry.get('link', ''),
            description=translated_content,
            pubdate=pub_date
        )
    
    return translated_feed

def process_single_feed(feed_config, cache, output_dir):
    """处理单个 feed（供线程池调用）"""
    try:
        translated_feed = translate_feed(feed_config, cache)

        # 保存 feed
        output_file = output_dir / f"{feed_config['name']}.xml"
        with open(output_file, 'w', encoding='utf-8') as f:
            translated_feed.write(f, 'utf-8')

        print(f"完成: {feed_config['name']}")
        return feed_config['name'], True
    except Exception as e:
        print(f"处理 {feed_config['name']} 失败: {e}")
        return feed_config['name'], False

def main():
    # 检查 API key
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误: 未设置 DEEPSEEK_API_KEY 环境变量")
        print("本地运行: export DEEPSEEK_API_KEY='你的key'")
        print("GitHub Actions: 在仓库 Settings > Secrets 中添加")
        return

    config = load_config()
    cache = load_cache()

    # 创建输出目录
    output_dir = Path('feeds')
    output_dir.mkdir(exist_ok=True)

    # 并行处理所有 feed（最多 3 个同时）
    completed_feeds = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(process_single_feed, fc, cache, output_dir): fc
            for fc in config['feeds']
        }
        for future in as_completed(futures):
            name, success = future.result()
            if success:
                completed_feeds.append(name)

    save_cache(cache)
    
    # 生成索引页
    index_content = f"""# RSS 中文翻译源

更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}

## 订阅链接

{chr(10).join(f"- {name}: https://awesomesnaki.github.io/rss-translator/feeds/{name}.xml" for name in sorted(completed_feeds))}

点击上面的链接即可获取订阅地址。
"""
    
    with open('index.md', 'w', encoding='utf-8') as f:
        f.write(index_content)

if __name__ == '__main__':
    main()
