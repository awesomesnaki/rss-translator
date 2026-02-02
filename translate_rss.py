import feedparser
import yaml
import os
import time
import hashlib
import json
from pathlib import Path
from deep_translator import GoogleTranslator
from feedgenerator import Rss201rev2Feed
from datetime import datetime, timezone

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

def translate_text(text, cache):
    if not text or not text.strip():
        return text
    
    text_hash = get_hash(text)
    if text_hash in cache:
        return cache[text_hash]
    
    try:
        # 截断过长文本（Google Translate 限制 5000 字符）
        truncated = text[:4500] if len(text) > 4500 else text
        translated = GoogleTranslator(source='auto', target='zh-CN').translate(truncated)
        cache[text_hash] = translated
        time.sleep(0.5)  # 避免请求过快
        return translated
    except Exception as e:
        print(f"翻译失败: {e}")
        return text

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
    
    # 只处理最新 20 条
    for entry in feed.entries[:20]:
        title = translate_text(entry.get('title', ''), cache)
        
        # 处理内容
        content = ''
        if 'content' in entry:
            content = entry.content[0].get('value', '')
        elif 'summary' in entry:
            content = entry.summary
        
        translated_content = translate_text(content, cache)
        
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
