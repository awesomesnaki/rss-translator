import feedparser
import yaml
import os
import time
import hashlib
import json
import re
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString
from feedgenerator import Rss201rev2Feed
from datetime import datetime, timezone
from openai import OpenAI

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

def translate_html_direct(html_content, cache):
    """调用 API 翻译一段 HTML，保留标签结构"""
    if not html_content or not html_content.strip():
        return html_content

    content_hash = get_hash(html_content.strip())
    if content_hash in cache:
        return cache[content_hash]

    translated = translate_with_deepseek(html_content)
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

def translate_feed(feed_config, cache):
    print(f"处理: {feed_config['name']}")
    feed = feedparser.parse(feed_config['url'])
    
    # 创建新 feed
    translated_feed = Rss201rev2Feed(
        title=f"{feed.feed.get('title', feed_config['name'])} (中文)",
        link=feed.feed.get('link', ''),
        description=translate_text(feed.feed.get('description', ''), cache),
        language='zh-CN'
    )
    
    # 只处理最新 10 条
    for entry in feed.entries[:10]:
        title = translate_text(entry.get('title', ''), cache)
        
        # 处理内容
        content = ''
        if 'content' in entry:
            content = entry.content[0].get('value', '')
        elif 'summary' in entry:
            content = entry.summary
        
        # 翻译 HTML 内容（保留标签结构）
        translated_content = translate_html_content(content, cache)
        
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
    
    feed_links = []
    
    for feed_config in config['feeds']:
        try:
            translated_feed = translate_feed(feed_config, cache)
            
            # 保存 feed
            output_file = output_dir / f"{feed_config['name']}.xml"
            with open(output_file, 'w', encoding='utf-8') as f:
                translated_feed.write(f, 'utf-8')
            
            feed_links.append(f"- {feed_config['name']}: `feeds/{feed_config['name']}.xml`")
            print(f"完成: {feed_config['name']}")
            
        except Exception as e:
            print(f"处理 {feed_config['name']} 失败: {e}")
    
    save_cache(cache)
    
    # 生成索引页
    index_content = f"""# RSS 中文翻译源

更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}

## 订阅链接

{chr(10).join(feed_links)}

将上面的 xml 文件链接替换为完整 URL 即可订阅。

基础 URL: `https://你的用户名.github.io/仓库名/`
"""
    
    with open('index.md', 'w', encoding='utf-8') as f:
        f.write(index_content)

if __name__ == '__main__':
    main()
