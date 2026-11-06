import feedparser
import yaml
import os
import time
import hashlib
import json
import re
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from pathlib import Path
from bs4 import BeautifulSoup
from feedgenerator import Rss201rev2Feed
from datetime import datetime, timezone, date, timedelta
from openai import OpenAI
import requests
from readability import Document
from site_sources import build_source_feed
import feed_health

# DeepSeek API 配置（从环境变量读取，不会暴露在代码里）
client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

# 模型：用不带版本号的 deepseek-flash，DeepSeek 会把它指向当前最新的 Flash 版本（2026-11 是
# DeepSeek-V4.1-Flash-1017），不用跟着改代码。以前写死的 deepseek-v4-flash 已经下线，只是旧名字
# 还被接受、转给新模型——哪天不接受了，所有翻译都会失败。每次运行先查 /models（resolve_model），
# 记下实际在服务的版本名；版本变了会在健康检查 feed 里通知（新版本价格可能不同）。
DEFAULT_MODEL = 'deepseek-flash'
MODEL = DEFAULT_MODEL
MODEL_NAME = None            # /models 返回的显示名，如 DeepSeek-V4.1-Flash

# 思考模式必须显式关掉：flash 模型默认开启思考（官方文档「支持非思考与思考模式（默认）」），
# 翻译用不着推理，思维链却按输出 token 计费，还和译文共用 max_tokens——标题总结只给 100 个
# token，思维链先把额度吃光、正文为空，就会退回成整段翻译（somepics 一部分标题是全文译文就是这么来的）
NO_THINKING = {'thinking': {'type': 'disabled'}}

# 用量估算用的单价（元/百万 tokens，2026-11 官方空闲时段价格，高峰时段翻倍）。只用于日志里的
# 粗略估算，价格变了改这里
PRICE_PER_M = {'input_hit': 0.02, 'input_miss': 1.0, 'output': 4.0}


def resolve_model():
    """查 /models 选翻译模型：优先 deepseek-flash，没有就选 id 里带 flash 的；查不到就用默认值"""
    global MODEL, MODEL_NAME
    try:
        models = list(client.models.list().data)
    except Exception as e:
        print(f"查询模型列表失败，使用默认模型 {DEFAULT_MODEL}: {e}")
        return MODEL, MODEL_NAME
    by_id = {m.id: m for m in models}
    pick = by_id.get(DEFAULT_MODEL) or next(
        (m for m in sorted(models, key=lambda m: m.id, reverse=True) if 'flash' in m.id), None)
    if pick is None:
        print(f"模型列表里没有 flash 模型（{sorted(by_id)}），使用默认模型 {DEFAULT_MODEL}")
        return MODEL, MODEL_NAME
    MODEL = pick.id
    MODEL_NAME = (getattr(pick, 'model_extra', None) or {}).get('name') or pick.id
    print(f"翻译模型: {MODEL}（{MODEL_NAME}）")
    return MODEL, MODEL_NAME


class ApiUsage:
    """统计每个源的 API 调用次数和 token 用量（多线程共用），运行结束打到日志里。
    以前只能靠逐个 commit 对比 feeds/*.xml 间接估算 token，现在每次运行都有真实数字"""

    FIELDS = ('calls', 'failures', 'prompt', 'cache_hit', 'completion', 'reasoning')

    def __init__(self):
        self._lock = threading.Lock()
        self.by_feed = {}
        self.last_error = ''

    def _row(self, feed):
        return self.by_feed.setdefault(feed, dict.fromkeys(self.FIELDS, 0))

    def record(self, feed, usage):
        extra = getattr(usage, 'model_extra', None) or {}
        details = getattr(usage, 'completion_tokens_details', None)
        with self._lock:
            r = self._row(feed)
            r['calls'] += 1
            r['prompt'] += getattr(usage, 'prompt_tokens', 0) or 0
            r['cache_hit'] += extra.get('prompt_cache_hit_tokens') or 0
            r['completion'] += getattr(usage, 'completion_tokens', 0) or 0
            r['reasoning'] += (getattr(details, 'reasoning_tokens', 0) or 0) if details else 0

    def fail(self, feed, error):
        with self._lock:
            r = self._row(feed)
            r['calls'] += 1
            r['failures'] += 1
            self.last_error = f'{type(error).__name__}: {error}'[:300]

    def totals(self):
        t = dict.fromkeys(self.FIELDS, 0)
        for r in self.by_feed.values():
            for k in self.FIELDS:
                t[k] += r[k]
        return t

    @staticmethod
    def cost(r):
        miss = r['prompt'] - r['cache_hit']
        return (r['cache_hit'] * PRICE_PER_M['input_hit'] + miss * PRICE_PER_M['input_miss']
                + r['completion'] * PRICE_PER_M['output']) / 1e6

    def report(self):
        t = self.totals()
        if not t['calls']:
            print("DeepSeek 用量：本次没有调用 API（全部命中缓存）")
            return
        print(f"DeepSeek 用量（{MODEL} / {MODEL_NAME or '未知版本'}）：{t['calls']} 次调用"
              f"（失败 {t['failures']}），输入 {t['prompt']:,} tokens（缓存命中 {t['cache_hit']:,}），"
              f"输出 {t['completion']:,} tokens（其中推理 {t['reasoning']:,}），"
              f"按空闲时段价约 ¥{self.cost(t):.3f}")
        for feed, r in sorted(self.by_feed.items(), key=lambda kv: -self.cost(kv[1])):
            print(f"  {feed:24} {r['calls']:3} 次  输入 {r['prompt']:>8,}  输出 {r['completion']:>8,}"
                  f"  推理 {r['reasoning']:>6,}  ¥{self.cost(r):.4f}"
                  + (f"  失败 {r['failures']}" if r['failures'] else ''))


USAGE = ApiUsage()
_current = threading.local()          # 当前线程正在处理的源，用量按源归账


def chat(system_prompt, user_content, max_tokens):
    """调用 DeepSeek（非思考模式），记录用量。失败时抛出异常，由调用方决定怎么兜底"""
    feed = getattr(_current, 'feed', '-')
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
            extra_body=NO_THINKING,
        )
    except Exception as e:
        USAGE.fail(feed, e)
        raise
    if response.usage is not None:
        USAGE.record(feed, response.usage)
    return (response.choices[0].message.content or '').strip()

# GitHub Pages 根地址，self_host_images 自托管图片时拼 URL 用
GH_PAGES_BASE = 'https://awesomesnaki.github.io/rss-translator'

# 自托管图片保留期：图片滚出 feed 窗口后再留这么多天才删。
# RSS 阅读器（Reeder 等）会长期缓存历史条目，删早了老文章图片就 404（少数派踩过坑）。
SELF_HOST_RETENTION_DAYS = 30
# 每张图最后一次出现在 feed 的日期，持久化在各自托管目录下。
# CI 每次都是全新 clone，文件 mtime 不可靠，保留期只能靠这个 manifest 自己记。
IMAGE_MANIFEST_NAME = '.image_manifest.json'

def load_config():
    with open('config.yaml', 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

# 翻译缓存淘汰：条目最后一次被命中/写入后超过这么多天未再使用即删除。
# 缓存曾涨到 100MB 撞上 GitHub 单文件限制导致 push 被拒，必须只保留活跃窗口内的条目。
# 正常情况下 feed 窗口内的条目每 2 小时都会命中一次，30 天足够容忍源站长时间抓取失败。
CACHE_RETENTION_DAYS = 30

# 模型偶尔不翻译，反过来回一句「请提供需要翻译的内容」。这种回复一旦写进缓存就永久顶掉
# 正文或标题（已移除的 franzgraf 源标题就被这样顶掉过），必须当成翻译失败处理。
REFUSAL_MARKERS = (
    '请提供需要翻译', '请提供您希望翻译', '请提供具体的英文', '请发送您需要翻译',
    '没有提供需要翻译', '未提供需要翻译', '您似乎只提供了', '我只收到了标题',
    '注意到您没有提供', '您未提供需要翻译',
)

def looks_like_refusal(reply):
    return any(marker in reply[:200] for marker in REFUSAL_MARKERS)

class TranslationCache:
    """dict 式翻译缓存，记录每个 key 的最后使用日期，保存时淘汰过期条目。

    新格式：{key: {"v": 译文, "t": "YYYY-MM-DD"}}；旧格式（值为字符串）加载时
    视为日期未知，本次运行未命中即在保存时被清掉，实现一次性瘦身。
    """

    def __init__(self, data):
        self._store = {}
        for k, v in (data or {}).items():
            value, stamp = (v['v'], v.get('t', '')) if isinstance(v, dict) and 'v' in v else (v, '')
            # 历史上被拒答污染的条目在这里一次性清掉，下次运行会重新翻译
            if looks_like_refusal(value):
                continue
            self._store[k] = [value, stamp]

    def __contains__(self, key):
        return key in self._store

    def __getitem__(self, key):
        entry = self._store[key]
        entry[1] = date.today().isoformat()
        return entry[0]

    def __setitem__(self, key, value):
        self._store[key] = [value, date.today().isoformat()]

    def to_json(self):
        cutoff = (date.today() - timedelta(days=CACHE_RETENTION_DAYS)).isoformat()
        return {k: {'v': v, 't': t} for k, (v, t) in self._store.items() if t >= cutoff}

def load_cache():
    cache_file = Path('cache.json')
    if cache_file.exists():
        with open(cache_file, 'r', encoding='utf-8') as f:
            return TranslationCache(json.load(f))
    return TranslationCache({})

def save_cache(cache):
    with open('cache.json', 'w', encoding='utf-8') as f:
        json.dump(cache.to_json(), f, ensure_ascii=False, separators=(',', ':'))

def get_hash(text):
    return hashlib.md5(text.encode()).hexdigest()

# GitHub Push Protection 会扫描 push 内容里的云服务秘钥（腾讯云 AKID、AWS AKIA 等）。
# 源站正文里出现别人泄露的 key（比如 V2EX 讨论泄露事件的帖子）会随全文抓取进入
# feeds/*.xml 和 content_cache.json，push 被拒（GH013）、workflow 失败。
# 在内容进入翻译/缓存/feed 之前统一打码；打码是确定性的（同样输入同样输出），
# 不会引起内容 hash 抖动导致翻译缓存失效。
SECRET_PATTERNS = [
    re.compile(r'AKID[A-Za-z0-9]{32}'),            # 腾讯云 SecretId
    re.compile(r'AKIA[A-Z0-9]{16}'),               # AWS Access Key ID
    re.compile(r'AIza[0-9A-Za-z_\-]{35}'),         # Google API Key
    re.compile(r'gh[pousr]_[A-Za-z0-9]{36,}'),     # GitHub token
    re.compile(r'github_pat_[A-Za-z0-9_]{22,}'),   # GitHub fine-grained PAT
    re.compile(r'LTAI[A-Za-z0-9]{12,24}'),         # 阿里云 AccessKey ID
    re.compile(r'xox[baprs]-[A-Za-z0-9\-]{10,}'),  # Slack token
]

def redact_secrets(text):
    if not text:
        return text
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0)[:6] + '****[已打码]', text)
    return text

# fetch_full_content 抓回来的正文快照保留期。存的是整篇 HTML，保留期不能像翻译缓存那样
# 开到 30 天，否则文件会涨到几十 MB；只要够覆盖 feed 窗口内的文章就行。
CONTENT_RETENTION_DAYS = 7
# 新抓的正文和上次的可见文字相似度高于此值，视为同一版本，沿用旧正文以命中翻译缓存
FULL_CONTENT_SIMILARITY = 0.97
# 新抓的正文短于上次的这个比例，视为抓取降级而非作者删内容
FULL_CONTENT_SHRINK_LIMIT = 0.8
# 变短的正文要连续这么多次抓到一模一样的内容，才认为是作者真的删了内容。
# 只要求两次是不够的：有的源会稳定地截断在同一个位置，两次确认会让 feed 在
# 完整版和半截版之间来回翻，每翻一次就白翻译一遍。
FULL_CONTENT_SHRINK_CONFIRMATIONS = 3

# 抓取的网页按错误编码解码（UTF-8 字节被当成 cp1252）会变成「é»„é“ç›Š」「â€™」这种乱码。
# 以前无条件用 requests 的 apparent_encoding（charset_normalizer 猜编码），它会把一些
# 明明是 UTF-8 的页面猜成 Windows-1252，V2EX / 蓝点网 / testingcatalog 都中过招。
# 乱码比原文长得多（一个汉字变三个字符），之后抓到正确正文反而会被 stable_full_content
# 当成「明显变短 = 抓取降级」挡掉，乱码就被锁死在 content_cache.json 里直到过期。
_MOJIBAKE_LEAD = bytes(range(0xe0, 0xf0)).decode('cp1252')                    # 三字节 UTF-8 首字节（汉字、中文标点、’ “ ” 等）
_MOJIBAKE_CONT = bytes(range(0x80, 0xc0)).decode('cp1252', errors='replace')  # 续字节，cp1252 未定义的变成 �
MOJIBAKE_PATTERN = re.compile(f'[{re.escape(_MOJIBAKE_LEAD)}][{re.escape(_MOJIBAKE_CONT)}]{{2}}')
# 正常文字里几乎不会出现这种三连（法语等偶尔有「à + 不换行空格 + «」），给点余量
MOJIBAKE_MIN_HITS = 3

def looks_mojibake(text):
    return bool(text) and len(MOJIBAKE_PATTERN.findall(text)) >= MOJIBAKE_MIN_HITS

def decode_html(resp):
    """把响应正文解码成文字。能按 UTF-8 严格解开就是 UTF-8（GBK 等其他编码的中文几乎不可能
    恰好是合法 UTF-8）；否则信 Content-Type 里声明的 charset；都没有才让 charset_normalizer 猜。"""
    raw = resp.content
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        pass
    declared = requests.utils.get_encoding_from_headers(resp.headers)
    # 没写 charset 的 text/* 响应，requests 按 RFC 默认给 ISO-8859-1，这个不算数
    if declared and 'charset' in resp.headers.get('content-type', '').lower():
        try:
            return raw.decode(declared, errors='replace')
        except LookupError:
            pass
    return raw.decode(resp.apparent_encoding or 'utf-8', errors='replace')

class ContentStore:
    """fetch_full_content 抓回来的正文快照，key 为文章链接。

    同一篇文章每次抓到的 HTML 常有微小抖动（模板噪音、CDN 差异、抓取被截断、拦截页），
    抖动会改变内容 hash → 翻译缓存全部失效 → 整篇重新翻译。franzgraf（已移除）和 workspaces
    就是这样在两个月里几乎没有新文章的情况下吃掉了九成 token。这里记住上次真正翻译过
    的那一版正文，只有内容确实变了才替换。

    格式：{url: {"c": 正文HTML, "i": 封面图, "t": 最后使用日期,
                 "p": 待确认的短正文 hash, "n": 连续抓到该短正文的次数}}
    """

    def __init__(self, data):
        self._store = {}
        for k, v in (data or {}).items():
            if isinstance(v, dict) and 'c' in v:
                if looks_mojibake(v['c']):
                    continue    # 历史上按错误编码解码的正文，丢掉让下次重新抓（见 looks_mojibake）
                self._store[k] = {'c': v['c'], 'i': v.get('i', ''), 't': v.get('t', ''),
                                  'p': v.get('p', ''), 'n': v.get('n', 0)}

    def get(self, url):
        entry = self._store.get(url)
        if entry is None:
            return None, None
        entry['t'] = date.today().isoformat()
        return entry['c'], (entry['i'] or None)

    def put(self, url, content, cover):
        self._store[url] = {'c': content, 'i': cover or '',
                            't': date.today().isoformat(), 'p': '', 'n': 0}

    def bump_shrink(self, url, digest):
        """记一次「抓到明显更短的正文」，返回连续抓到同样内容的次数（内容一变就归零）"""
        entry = self._store.get(url)
        if entry is None:
            return 0
        entry['n'] = entry['n'] + 1 if entry['p'] == digest else 1
        entry['p'] = digest
        return entry['n']

    def to_json(self):
        cutoff = (date.today() - timedelta(days=CONTENT_RETENTION_DAYS)).isoformat()
        return {k: v for k, v in self._store.items() if v['t'] >= cutoff}

def load_content_store():
    store_file = Path('content_cache.json')
    if store_file.exists():
        with open(store_file, 'r', encoding='utf-8') as f:
            return ContentStore(json.load(f))
    return ContentStore({})

def save_content_store(store):
    with open('content_cache.json', 'w', encoding='utf-8') as f:
        json.dump(store.to_json(), f, ensure_ascii=False, separators=(',', ':'))

def visible_text(html):
    """取 HTML 里的可见文字，用来判断两次抓取是不是同一篇正文（忽略标签和属性的抖动）"""
    return re.sub(r'\s+', ' ', BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)).strip()

def same_article(old_text, new_text):
    """两段正文文字是否只是抖动（视为同一版本），而不是文章本身改了"""
    if old_text == new_text:
        return True
    if not old_text or not new_text:
        return False
    # 旧正文原封不动地出现在新正文里 → 作者确实追加了内容（比如补了一段 Update），
    # 这种情况哪怕只多了 1% 也要重新翻译；散点式的噪音则不会保留旧正文的完整片段。
    if len(new_text) > len(old_text) and old_text in new_text:
        return False
    short, long_ = sorted((len(old_text), len(new_text)))
    if short < long_ * FULL_CONTENT_SIMILARITY:
        return False
    # quick_ratio 只比字符构成，O(n)，判断「同一篇文章的轻微抖动」够用，
    # 也不会像 ratio() 那样在上万字的正文上跑到超时
    return difflib.SequenceMatcher(None, old_text, new_text).quick_ratio() >= FULL_CONTENT_SIMILARITY

def stable_full_content(url, fetched, cover, store):
    """把这次抓取的结果和上次的快照对齐，返回真正拿去翻译的 (正文, 封面图)。

    抓取失败或被截断时沿用上次的正文：既省掉重复翻译，也避免 feed 里突然冒出空正文
    或半截文章（franzgraf 的正文长度曾在 0 / 379 / 5400 字之间反复横跳）。
    """
    fetched = redact_secrets(fetched)
    previous, prev_cover = store.get(url)
    if not fetched or not fetched.strip():
        return previous, (prev_cover or cover)
    if previous is None:
        store.put(url, fetched, cover)
        return fetched, cover
    if fetched == previous:                         # 完全没变，省掉两次 HTML 解析
        return previous, (prev_cover or cover)

    old_text, new_text = visible_text(previous), visible_text(fetched)
    if same_article(old_text, new_text):
        return previous, (prev_cover or cover)      # 只是抖动，沿用旧正文，翻译缓存照常命中

    if len(new_text) >= len(old_text) * FULL_CONTENT_SHRINK_LIMIT:
        store.put(url, fetched, cover)              # 正文确实变了（补充内容 / 新增评论）
        return fetched, cover

    # 明显变短：更可能是这次抓取降级，而不是作者删了内容。要连续多次抓到一模一样的短正文
    # 才认——抓取抖动每次截断的位置都不一样，凑不齐连续几次；作者真删了则很快就通过。
    digest = get_hash(new_text)
    times = store.bump_shrink(url, digest)
    if times >= FULL_CONTENT_SHRINK_CONFIRMATIONS:
        store.put(url, fetched, cover)
        return fetched, cover
    print(f"  正文比上次短很多（{len(new_text)} vs {len(old_text)} 字，第 {times} 次），"
          f"按抓取降级处理，沿用上次内容")
    return previous, (prev_cover or cover)

def translate_with_deepseek(text):
    """调用 DeepSeek API 翻译。失败或模型拒答时返回 None，调用方不要把结果写进缓存——
    以前这里出错会原样返回英文，然后被当成译文永久缓存下来，一次网络抖动污染到缓存过期。"""
    if not text or not text.strip():
        return text
    
    try:
        reply = chat(
            "你是一个专业翻译。请将用户提供的英文内容翻译成流畅自然的中文。\n要求：\n1. 只输出翻译结果\n2. 完整保留所有HTML标签（如<p>、<strong>、<a>、<em>、<figure>等），只翻译标签内的文字\n3. 不要添加任何markdown格式符号（如**、*、#等）\n4. 保留所有链接URL和图片路径不变\n5. 保持原文的语气和风格",
            text, max_tokens=8192)
        if not reply or looks_like_refusal(reply):
            print(f"模型未返回译文（拒答或空回复），本次跳过缓存: {text[:40]!r}")
            return None
        return reply
    except Exception as e:
        print(f"DeepSeek API 调用失败: {e}")
        return None

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
    time.sleep(0.3)  # 避免请求过快
    if translated is None:
        return text  # 本次先用原文，不写缓存，下次运行重试
    cache[text_hash] = translated
    return translated

def summarize_title_text(text, cache):
    """将长标题总结为简短的中文短语，带缓存"""
    if not text or not text.strip():
        return text

    cache_key = "summarize:" + get_hash(text.strip())
    if cache_key in cache:
        return cache[cache_key]

    try:
        result = chat(
            "你是一个标题总结助手。用户会给你一段图片帖子的长文字描述，请将其总结为一个简短的中文标题（一个短语或一句话，不超过15个字）。只输出标题本身，不要加引号或其他格式。",
            text, max_tokens=100)
        time.sleep(0.3)
        if not result or looks_like_refusal(result):
            return translate_text(text, cache)
        cache[cache_key] = result
        return result
    except Exception as e:
        print(f"标题总结失败: {e}")
        return translate_text(text, cache)


# ---------------------------------------------------------------------------
# HTML 翻译：瘦身 + 段落级缓存
#
# 以前按「顶层元素凑够 3000 字一组」翻译、按组缓存。问题有两个：
# 1. 作者在中间插一段、改一个错字，插入点之后所有组的边界都会移位，hash 全变 → 几乎整篇重翻。
#    basicappleguy 一篇边用边记的长文作者追加了 8 次，每次都整篇重翻，重翻量是新文的 1.8 倍；
#    DaringFireball 的 Gruber 改稿也是同样的情况。
# 2. class / id / data-* / <picture><source srcset> 这些标记，模型要读一遍、原样输出一遍，翻译后又被
#    clean_readability_html 删掉，白花 token（实测约占送翻字符的两成）；而且上游只改了标记、
#    正文一字没变时（Substack、Squarespace 改模板），hash 也会变 → 整篇重翻。
#
# 现在的做法：
# - 先查整篇缓存（key 是原始 HTML 的 hash，和以前一样），已经翻译过的文章直接命中，
#   这个改动上线时不会让现有缓存失效。
# - 没命中就先瘦身（slim_html），再切成段落（split_segments：每个块级元素一段，连续的行内内容
#   合成一段），每段单独缓存（key = "seg:" + 段落 hash）。只把没命中的段落送去翻译，
#   相邻的打成一包（不超过 TRANSLATE_BATCH_CHARS），调用次数和以前一样。
# - 译文用同一个切分器切开，逐段核对标签名能和原文对上，才按段写缓存；对不上（模型合并/拆分了
#   段落）就整包原样使用、不按段缓存——效果和以前按组缓存完全一样，不会多一次调用。
#   所以这套机制不依赖模型保留任何特殊标记。
# - 没有英文字母的段落（纯图片、分隔线、数字）不翻译，原样保留。
# ---------------------------------------------------------------------------
TRANSLATE_BATCH_CHARS = 3000
SEGMENT_CACHE_PREFIX = 'seg:'

# 瘦身时保留的属性：链接、图片说明、表格结构等语义信息。图片/媒体标签原样不动（图片在翻译时
# 会被换成占位符，属性再长也不花 token；懒加载的 data-src 之后还要靠 fix_image_tags 还原）
SLIM_KEEP_ATTRS = {'href', 'src', 'alt', 'title', 'colspan', 'rowspan', 'start', 'datetime', 'cite'}
SLIM_MEDIA_TAGS = {'img', 'iframe', 'video', 'audio', 'source', 'track', 'embed', 'object'}
_IMAGE_HREF = re.compile(r'\.(jpe?g|png|gif|webp|avif|heic)(\?|$)|/image/', re.I)

BLOCK_TAGS = {
    'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'dl', 'blockquote', 'pre', 'table',
    'figure', 'hr', 'div', 'section', 'article', 'aside', 'header', 'footer', 'details', 'nav',
    'main', 'address', 'center', 'form', 'fieldset', 'hgroup', 'iframe', 'video', 'audio', 'img',
}
_WRAPPER_TAGS = {'div', 'section', 'article', 'main', 'body', 'html'}


def slim_html(html):
    """去掉翻译用不到的标记，只保留语义。输出效果和翻译后 clean_readability_html 清理的结果一致"""
    from bs4 import Comment
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.find_all(['script', 'style', 'noscript', 'head']):
        tag.decompose()
    for tag in soup.find_all(['html', 'body']):          # readability 的输出带 <html><body> 外壳
        tag.unwrap()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for picture in soup.find_all('picture'):            # 和 clean_readability_html 一样简化成 <img>
        img = picture.find('img')
        if img:
            img.extract()
            picture.replace_with(img)
        else:
            picture.decompose()
    for tag in soup.find_all(True):
        if tag.name in SLIM_MEDIA_TAGS:
            if tag.name == 'img':
                tag.attrs.pop('srcset', None)
                tag.attrs.pop('sizes', None)
            continue
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in SLIM_KEEP_ATTRS}
    for tag in soup.find_all(['span', 'div']):
        if not tag.attrs:
            tag.unwrap()
    # 只包着一张图、指向图片本身的链接（「点击看大图」，Substack 每张图都这样包一层超长 CDN 链接）
    for a in soup.find_all('a'):
        if (len(a.find_all('img')) == 1 and not a.get_text(strip=True)
                and _IMAGE_HREF.search(a.get('href', ''))):
            a.unwrap()
    return str(soup)


def split_segments(html):
    """切成段落：每个块级元素一段，相邻的文字/行内元素合成一段，块之间的空白丢掉。
    包着块级元素的包裹层（<div>/<section>/<article>/<main>）会被展开再切——AnthropicNews 的
    正文整个包在一个 <article> 里，不展开的话整篇就是一段。返回 [(签名, html)]，签名用于核对译文"""
    from bs4 import NavigableString, Tag

    def has_blocks(tag):
        return any(isinstance(c, Tag) and (c.name in BLOCK_TAGS or (c.name in _WRAPPER_TAGS and has_blocks(c)))
                   for c in tag.children)

    def expand(nodes):
        for n in nodes:
            if isinstance(n, Tag) and n.name in _WRAPPER_TAGS and has_blocks(n):
                yield from expand(list(n.contents))
            else:
                yield n

    nodes = list(expand(list(BeautifulSoup(html, 'html.parser').contents)))
    segments, inline = [], []

    def flush():
        text = ''.join(str(n) for n in inline)
        if text.strip():
            segments.append(('#inline', text.strip()))
        inline.clear()

    for node in nodes:
        if isinstance(node, Tag) and node.name in BLOCK_TAGS:
            flush()
            segments.append((node.name, str(node)))
        else:
            inline.append(node)
    flush()
    return segments


def _needs_translation(segment_html):
    text = BeautifulSoup(segment_html, 'html.parser').get_text()
    return bool(re.search(r'[A-Za-z]', text))


_IMG_TAG = re.compile(r'<img\s[^>]*/?>')


def _protected_len(segment_html):
    return len(_IMG_TAG.sub('<!-- IMG_0 -->', segment_html))


def translate_html_fragment(html_content):
    """调用 API 翻译一段 HTML，保留标签结构，不读写缓存。失败返回 None"""
    if not html_content or not html_content.strip():
        return html_content
    # 保护 img 标签不被翻译篡改（LLM 偶尔会改动 URL）
    img_store = {}
    def replace_img(match):
        placeholder = f'<!-- IMG_{len(img_store)} -->'
        img_store[placeholder] = match.group(0)
        return placeholder
    protected = _IMG_TAG.sub(replace_img, html_content)

    translated = translate_with_deepseek(protected)
    if translated is None:
        return None
    for placeholder, original in img_store.items():
        translated = translated.replace(placeholder, original)
    time.sleep(0.3)
    return translated


def translate_html_content(html_content, cache):
    """翻译整篇 HTML 正文（瘦身 + 段落级缓存，见上方说明）"""
    if not html_content or not html_content.strip():
        return html_content

    whole_key = get_hash(html_content.strip())
    if whole_key in cache:
        return cache[whole_key]

    segments = split_segments(slim_html(html_content))
    out = [None] * len(segments)          # None = 还没有译文，要送翻
    for i, (_, seg) in enumerate(segments):
        if not _needs_translation(seg):
            out[i] = seg
        else:
            key = SEGMENT_CACHE_PREFIX + get_hash(seg)
            if key in cache:
                out[i] = cache[key]
    todo = [i for i, o in enumerate(out) if o is None]

    # 把待翻段落按原文顺序打包：同一包必须是连续区间（中间只允许夹不需要翻译的段落），
    # 这样对不上时可以整包原样放回去，顺序不乱
    batches, current, size = [], [], 0
    for i in todo:
        gap = list(range(current[-1] + 1, i)) if current else []
        bridgeable = all(not _needs_translation(segments[j][1]) for j in gap)
        add = sum(_protected_len(segments[j][1]) for j in gap + [i])
        if current and (not bridgeable or size + add > TRANSLATE_BATCH_CHARS):
            batches.append(current)
            current, size = [], 0
            gap, add = [], _protected_len(segments[i][1])
        current.extend(gap + [i])
        size += add
    if current:
        batches.append(current)

    any_failed = False
    for batch in batches:
        source = ''.join(segments[i][1] for i in batch)
        translated = translate_html_fragment(source)
        if translated is None:
            any_failed = True             # 本次用原文顶上，不写缓存，下次重试
            for i in batch:
                out[i] = segments[i][1]
            continue
        parts = split_segments(translated) if len(batch) > 1 else [(segments[batch[0]][0], translated)]
        if len(parts) == len(batch) and all(p[0] == segments[i][0] for p, i in zip(parts, batch)):
            for (_, part), i in zip(parts, batch):
                if _needs_translation(segments[i][1]):
                    out[i] = part
                    cache[SEGMENT_CACHE_PREFIX + get_hash(segments[i][1])] = part
                else:
                    out[i] = segments[i][1]
        else:
            # 模型合并/拆分了段落，对不上：整包原样使用，不按段缓存（和以前按组缓存效果相同）
            print(f"  译文段落对不上（{len(parts)} vs {len(batch)}），这一包不按段缓存")
            out[batch[0]] = translated
            for i in batch[1:]:
                out[i] = ''

    result = ''.join(out)
    # 有分包翻译失败就不写整篇缓存，否则半中半英会被永久缓存下来
    if not any_failed:
        cache[whole_key] = result
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

def download_remote_image(url, images_dir, referer, attempts=3):
    """下载单张远程图片到 images_dir，文件名为 URL 的 md5 + 扩展名。
    已下载过则命中缓存直接跳过。返回文件名，彻底失败返回 None。"""
    key = get_hash(url)
    for cached in images_dir.glob(f'{key}.*'):
        return cached.name

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    }
    if referer:
        headers['Referer'] = referer

    # 七牛云 imageView2 会把图转成 webp，扩展名以实际 Content-Type 为准
    ct_ext = {
        'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png',
        'image/webp': 'webp', 'image/gif': 'gif', 'image/avif': 'avif',
    }
    last_err = None
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            ct = resp.headers.get('Content-Type', '').split(';')[0].strip().lower()
            ext = ct_ext.get(ct)
            if not ext:
                path_ext = url.split('?')[0].rsplit('.', 1)[-1].lower()
                ext = path_ext if path_ext in ('jpg', 'jpeg', 'png', 'webp', 'gif') else 'jpg'
            filename = f'{key}.{ext}'
            (images_dir / filename).write_bytes(resp.content)
            return filename
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(2 ** i)  # 1s, 2s
    print(f"  图片下载失败 {url}: {last_err}")
    return None

def localize_images(html_content, images_dir, referer, used_filenames):
    """把正文里的远程图片下载到本地自托管，<img src> 改写为 GitHub Pages URL。
    适用于源站 CDN 防盗链严格、referrerpolicy=no-referrer 也救不回来的源（如少数派）。
    used_filenames 收集本次引用到的文件名，供之后清理离站旧图。"""
    if not html_content:
        return html_content
    soup = BeautifulSoup(html_content, 'html.parser')
    for img in soup.find_all('img'):
        src = img.get('src', '')
        if not src.startswith(('http://', 'https://')):
            continue
        if src.startswith(GH_PAGES_BASE):  # 已是本地托管的图，跳过
            continue
        filename = download_remote_image(src, images_dir, referer)
        if filename:
            img['src'] = f'{GH_PAGES_BASE}/feeds/images/{images_dir.name}/{filename}'
            used_filenames.add(filename)
        # 下载失败：保留原 src（带 no-referrer），下次运行再试
    return str(soup)

def gc_self_hosted_images(images_dir, used_filenames, retention_days=SELF_HOST_RETENTION_DAYS):
    """自托管图片的保留期清理。
    本期 feed 引用到的图把 last-seen 刷成今天；不再引用的图保留 retention_days 天后才删。
    不像豆瓣榜单离榜即删——文章流的旧条目会长期留在 RSS 阅读器里，仍指着这些本地图，
    删早了就 404。每张图的 last-seen 持久化在 images_dir/IMAGE_MANIFEST_NAME。"""
    manifest_path = images_dir / IMAGE_MANIFEST_NAME
    try:
        last_seen = json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception:
        last_seen = {}

    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    on_disk = {
        f.name for f in images_dir.iterdir()
        if f.is_file() and f.name != IMAGE_MANIFEST_NAME
    }

    # 本期引用到的图刷新为今天；磁盘上有、manifest 没记的老图（本功能上线前就在的）
    # 也补登为今天，给一个完整保留期，避免首次运行就把存量图误删
    for name in used_filenames:
        last_seen[name] = today_str
    for name in on_disk:
        last_seen.setdefault(name, today_str)

    # 清理：超过保留期且本期未引用的删掉；顺手清掉 manifest 里文件已不在的条目
    cutoff = today - timedelta(days=retention_days)
    for name in list(last_seen.keys()):
        if name not in on_disk:
            del last_seen[name]
            continue
        if name in used_filenames:
            continue
        try:
            seen = date.fromisoformat(last_seen[name])
        except (ValueError, TypeError):
            last_seen[name] = today_str  # 脏数据按今天算，保守保留
            continue
        if seen < cutoff:
            (images_dir / name).unlink()
            del last_seen[name]
            print(f"  清理过期图片(>{retention_days}天未出现): {name}")

    manifest_path.write_text(
        json.dumps(last_seen, ensure_ascii=False, sort_keys=True, indent=0),
        encoding='utf-8',
    )

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

    # Beehiiv 等源的 RSS 用非标准 <graphic> 标签包图片，转成标准 <img> 让阅读器渲染
    for graphic in soup.find_all('graphic'):
        src = graphic.get('src') or graphic.get('url')
        if src:
            img = soup.new_tag('img', src=src)
            if graphic.get('alt'):
                img['alt'] = graphic['alt']
            graphic.replace_with(img)
        else:
            graphic.decompose()

    # <picture> → 简单 <img>（很多 RSS 阅读器不支持 picture/source）
    for picture in soup.find_all('picture'):
        img = picture.find('img')
        if img:
            img.extract()
            picture.replace_with(img)
        else:
            picture.decompose()

    # 去掉所有 CSS class 和 style 属性（没有样式表 / RSS 阅读器有自己的样式）
    # Beehiiv 等源会内联绿色边框、文字对齐等样式，去掉后渲染更干净
    for tag in soup.find_all(True):
        if tag.get('class'):
            del tag['class']
        if tag.get('style'):
            del tag['style']

    # 展开纯包装的 <span>（没有任何属性的 span 只是多余嵌套）
    for span in soup.find_all('span'):
        if not span.attrs:
            span.unwrap()

    # 去掉空的 div 包装
    for div in soup.find_all('div'):
        if not div.attrs:
            div.unwrap()

    return str(soup)

def extract_cover_image(html):
    """从原文 HTML 提取 og:image / twitter:image 作为封面图。
    readability 提取正文时通常会漏掉 WordPress 等 CMS 的 Featured Image
    （它在文章容器之外的页面头部渲染），改用 meta 标签拿到准确的封面。"""
    soup = BeautifulSoup(html, 'html.parser')
    for attrs in [
        {'property': 'og:image'},
        {'property': 'og:image:url'},
        {'name': 'twitter:image'},
        {'name': 'twitter:image:src'},
    ]:
        tag = soup.find('meta', attrs=attrs)
        if tag and tag.get('content'):
            return tag['content']
    return None

def normalize_image_basename(url):
    """提取图片基础文件名，去掉 WordPress 自动生成的尺寸后缀（-1024x768）"""
    if not url:
        return ''
    name = url.split('?')[0].rsplit('/', 1)[-1]
    return re.sub(r'-\d+x\d+(\.\w+)$', r'\1', name)

# 被 Cloudflare / WAF 拦下时页面里根本没有正文，readability 会把拦截页当正文抽出来，
# 于是拦截页被送去翻译、写进缓存，还顶掉了 feed 里的真正文。
BLOCKED_PAGE_MARKERS = (
    'cf-error-details', 'cf-browser-verification', 'cf-challenge-running',
    'Just a moment...', 'Attention Required! | Cloudflare',
    'Checking your browser before accessing',
    'Enable JavaScript and cookies to continue',
)

def looks_blocked(html):
    return any(marker in html for marker in BLOCKED_PAGE_MARKERS)

def fetch_full_article(url):
    """从原文 URL 抓取完整文章内容和封面图，返回 (content, cover_image_url)"""
    try:
        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        resp.raise_for_status()
        html = decode_html(resp)

        if looks_blocked(html):
            print(f"  抓取被拦截（Cloudflare/WAF），当作抓取失败: {url}")
            return None, None
        if looks_mojibake(html):
            print(f"  页面解码出乱码，当作抓取失败: {url}")
            return None, None

        # V2EX 帖子用专用解析，避免 readability 乱排版
        if 'v2ex.com/t/' in url:
            content = extract_v2ex_content(html)
            if content:
                return content, None

        # 张洪Heo 博客用专用解析：只取 Butterfly 正文容器，
        # 去掉作者卡片/打赏/版权/相关文章等 readability 会抓进来的噪音。
        # 不返回封面图：该博客 og:image 常是站点头像而非正文图，只要正文内容
        if 'blog.zhheo.com' in url:
            content = extract_zhheo_content(html)
            if content:
                return content, None

        cover = extract_cover_image(html)

        doc = Document(html)
        content = doc.summary()
        if content and content.strip():
            # 确认有真正的文字内容，而不只是空的 HTML 标签壳子
            text = BeautifulSoup(content, 'html.parser').get_text(strip=True)
            if text:
                return content, cover
        print(f"  未提取到内容: {url}")
        return None, cover
    except Exception as e:
        print(f"  抓取出错 {url}: {e}")
        return None, None

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

def extract_zhheo_content(html):
    """从张洪Heo博客（Butterfly 主题）文章页提取正文。

    只取 #article-container 这个正文容器：作者卡片、打赏、版权声明、
    "最近发布"、相关文章、评论等都是该容器外的兄弟节点，天然被排除。
    RSS 自带的「AI 文章摘要」也随之丢掉（本函数返回的全文会替换它）。"""
    soup = BeautifulSoup(html, 'html.parser')
    # Butterfly 正文容器 id 固定为 article-container；兼容退回 .post-content
    container = soup.find(id='article-container') or soup.find(class_='post-content')
    if not container:
        return None

    for tag in container.find_all(['script', 'style', 'noscript']):
        tag.decompose()

    # AI 文章摘要（TianliGPT）一般由 JS 注入、不在静态 HTML 里；
    # 万一被服务端渲染进了正文容器，按 id/class 关键字兜底去掉
    for el in list(container.find_all(True)):
        ident = (el.get('id') or '').lower()
        classes = ' '.join(el.get('class', [])).lower()
        if 'tianligpt' in ident or 'tianligpt' in classes or 'post-ai' in classes:
            el.decompose()

    # Butterfly 懒加载：真实图片 URL 在 data-lazy-src，src 只是占位图。
    # RSS 阅读器不跑懒加载 JS，不还原就会一直转圈加载不出来
    for img in container.find_all('img'):
        lazy = img.get('data-lazy-src') or img.get('data-src')
        if lazy:
            img['src'] = lazy
            img.attrs.pop('data-lazy-src', None)
            img.attrs.pop('data-src', None)

    inner = container.decode_contents()
    if BeautifulSoup(inner, 'html.parser').get_text(strip=True):
        return inner
    return None

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

def translate_feed(feed_config, cache, content_store, report=None):
    """report 不为 None 时，往里填上游条目的链接和发布时间（供 feed_health 判断停更）"""
    print(f"处理: {feed_config['name']}")
    url = resolve_feed_url(feed_config['url'])
    print(f"  URL: {url}")
    # 没有 RSS 的站点（如 pubg://）：site_sources 调站点数据接口生成 RSS XML
    source_xml = build_source_feed(feed_config['url'], feed_config)
    if source_xml is not None:
        feed = feedparser.parse(source_xml)
    else:
        feed = feedparser.parse(url, request_headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })

    # 上游一条都没有基本都是出错了（RSSHub 路由报错返回 HTML、源站挂了）。以前这里会照常
    # 生成一个空 feed，把已经发布的条目全部覆盖掉；现在当成失败处理：保留旧 XML，计入健康检查
    if not feed.entries:
        reason = feed.get('bozo_exception') or '上游没有返回任何条目'
        raise RuntimeError(f"上游 feed 为空: {reason}")
    if report is not None:
        report.update(feed_health.entry_report(feed.entries, datetime.now(timezone.utc)))

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
    filter_in = feed_config.get('filter_in', [])
    filter_in_content = feed_config.get('filter_in_content', [])
    filter_category = feed_config.get('filter_category')
    # max_entries 可在 config.yaml 里显式指定：更新快的源在两次运行的间隔里会推出
    # 好几条，窗口太小就会漏条目（谷时调度把运行间隔拉长之后尤其明显）
    configured_max = feed_config.get('max_entries')
    if configured_max:
        max_entries = configured_max
    else:
        max_entries = None if (filter_in or filter_in_content) else (50 if (entry_filter or filter_out or filter_out_content or filter_category) else 5)
    entries = feed.entries[:max_entries] if max_entries else feed.entries

    def get_entry_content(e):
        if 'content' in e:
            return e.content[0].get('value', '')
        return e.get('summary', '')

    if entry_filter:
        entries = apply_entry_filter(entries, entry_filter)
        print(f"  过滤后保留 {len(entries)} 条 (类型: {entry_filter})")
    if filter_out:
        before = len(entries)
        entries = [e for e in entries if not any(kw in e.get('title', '') for kw in filter_out)]
        print(f"  标题过滤: {before} → {len(entries)} 条")
    if filter_out_content:
        before = len(entries)
        entries = [e for e in entries if not any(kw in get_entry_content(e) for kw in filter_out_content)]
        print(f"  正文过滤: {before} → {len(entries)} 条")
    if filter_in:
        before = len(entries)
        entries = [e for e in entries if any(kw in e.get('title', '') for kw in filter_in)]
        print(f"  标题保留: {before} → {len(entries)} 条 (关键词: {filter_in})")
    if filter_in_content:
        before = len(entries)
        entries = [e for e in entries if any(kw in get_entry_content(e) for kw in filter_in_content)]
        print(f"  正文保留: {before} → {len(entries)} 条 (关键词: {filter_in_content})")
    if filter_category:
        before = len(entries)
        wanted = [filter_category] if isinstance(filter_category, str) else list(filter_category)
        entries = [
            e for e in entries
            if any(t.get('term') in wanted for t in e.get('tags', []))
        ]
        print(f"  分类过滤: {before} → {len(entries)} 条 (保留: {wanted})")

    should_summarize_title = feed_config.get('summarize_title', False)
    # translate_title: false — 标题保留原文（比如以人名为标题的源，翻译会把人名音译走样）
    should_translate_title = should_translate and feed_config.get('translate_title', True)
    should_images_only = feed_config.get('images_only', False)
    should_text_only = feed_config.get('text_only', False)
    should_self_host = feed_config.get('self_host_images', False)

    images_dir = None
    used_images = set()
    if should_self_host:
        images_dir = Path('feeds') / 'images' / feed_config['name']
        images_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        original_title = redact_secrets(entry.get('title', ''))

        if should_summarize_title and should_translate:
            title = summarize_title_text(original_title, cache)
        elif should_translate_title:
            title = translate_text(original_title, cache)
        else:
            title = original_title

        # 处理内容
        content = ''
        if 'content' in entry:
            content = entry.content[0].get('value', '')
        elif 'summary' in entry:
            content = entry.summary
        content = redact_secrets(content)

        cover_image = None

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
                fetched, fetched_cover = fetch_full_article(entry['link'])
                # 和上次抓到的正文对齐：抖动/截断/拦截页一律沿用上次内容，
                # 保证送去翻译的内容稳定，翻译缓存才命中得住
                full_content, cover_image = stable_full_content(
                    entry['link'], fetched, fetched_cover, content_store
                )
                if full_content:
                    # 智能回退：如果抓回来的内容图片数比 RSS 自带的少，
                    # 说明原网页可能是 JS 渲染或被改版，readability 拿不全，
                    # 此时 RSS 自带的全文（包含 Beehiiv 的 <graphic> 等）反而更完整
                    rss_imgs = len(BeautifulSoup(content, 'html.parser').find_all(['img', 'graphic']))
                    full_imgs = len(BeautifulSoup(full_content, 'html.parser').find_all(['img', 'graphic']))
                    if full_imgs >= rss_imgs:
                        content = full_content
                    else:
                        print(f"  RSS 内容图片更多 ({rss_imgs} vs {full_imgs})，保留 RSS 内容")
                else:
                    print(f"  回退到 RSS 摘要")
                time.sleep(1)

            # 翻译 HTML 内容（保留标签结构）
            translated_content = translate_html_content(content, cache) if should_translate else content

        # 翻译后清理 HTML（放在翻译之后，避免改变 hash 导致缓存失效）
        translated_content = clean_readability_html(translated_content)

        # 把封面图（og:image）加到正文最前面：
        # readability 提取正文时常漏掉 WordPress Featured Image 等 hero 图，这里补回来。
        # 必须放在翻译之后，否则会改变内容 hash 导致缓存全部失效。
        if cover_image:
            cover_norm = normalize_image_basename(cover_image)
            existing_norms = {
                normalize_image_basename(img.get('src', ''))
                for img in BeautifulSoup(translated_content, 'html.parser').find_all('img')
            }
            if cover_norm and cover_norm not in existing_norms:
                translated_content = f'<figure><img src="{cover_image}" alt=""/></figure>\n' + translated_content

        # 修复图片防盗链问题
        translated_content = fix_image_tags(translated_content)

        # 自托管图片：下载到本仓库，绕过源站 CDN 防盗链（no-referrer 救不回来时用）
        if should_self_host:
            referer = entry.get('link') or feed.feed.get('link', '')
            translated_content = localize_images(
                translated_content, images_dir, referer, used_images
            )

        # 纯文字模式：去掉所有图片
        if should_text_only:
            soup = BeautifulSoup(translated_content, 'html.parser')
            for img in soup.find_all('img'):
                img.decompose()
            translated_content = str(soup)

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

    # 自托管图片清理：保留期内的旧图不动（覆盖阅读器长期缓存的历史条目），超期才删
    if should_self_host and used_images:
        gc_self_hosted_images(images_dir, used_images)
    
    return translated_feed

def process_single_feed(feed_config, cache, content_store, output_dir):
    """处理单个 feed（供线程池调用）。返回 (名字, 是否成功, 健康检查报告)"""
    report = {}
    _current.feed = feed_config['name']
    try:
        translated_feed = translate_feed(feed_config, cache, content_store, report)

        # 保存 feed
        output_file = output_dir / f"{feed_config['name']}.xml"
        with open(output_file, 'w', encoding='utf-8') as f:
            translated_feed.write(f, 'utf-8')

        print(f"完成: {feed_config['name']}")
        return feed_config['name'], True, {'ok': True, **report}
    except Exception as e:
        print(f"处理 {feed_config['name']} 失败: {e}")
        return feed_config['name'], False, {'ok': False, 'error': f'{type(e).__name__}: {e}'}

def main():
    # 检查 API key
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误: 未设置 DEEPSEEK_API_KEY 环境变量")
        print("本地运行: export DEEPSEEK_API_KEY='你的key'")
        print("GitHub Actions: 在仓库 Settings > Secrets 中添加")
        return

    config = load_config()
    resolve_model()
    cache = load_cache()
    content_store = load_content_store()

    # 创建输出目录
    output_dir = Path('feeds')
    output_dir.mkdir(exist_ok=True)

    # 并行处理所有 feed（最多 3 个同时）
    completed_feeds = []
    reports = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(process_single_feed, fc, cache, content_store, output_dir): fc
            for fc in config['feeds']
        }
        for future in as_completed(futures):
            name, success, report = future.result()
            reports[name] = report
            if success:
                completed_feeds.append(name)

    save_cache(cache)
    save_content_store(content_store)

    USAGE.report()

    # 健康检查：停更 / 连续失败的源写进 feeds/health.xml。它出错不能影响翻译结果的提交
    totals = USAGE.totals()
    api = {'model': MODEL, 'model_name': MODEL_NAME, 'calls': totals['calls'],
           'failures': totals['failures'], 'last_error': USAGE.last_error}
    try:
        feed_health.run(config['feeds'], reports, api=api)
    except Exception as e:
        print(f"健康检查失败: {type(e).__name__}: {e}")
    
    # 生成索引页
    index_content = f"""# RSS 中文翻译源

更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}

## 订阅链接

{chr(10).join(f"- {name}: https://awesomesnaki.github.io/rss-translator/feeds/{name}.xml" for name in sorted(completed_feeds))}

点击上面的链接即可获取订阅地址。

健康检查（源停更、连续失败时告警）: https://awesomesnaki.github.io/rss-translator/feeds/health.xml
"""
    
    with open('index.md', 'w', encoding='utf-8') as f:
        f.write(index_content)

if __name__ == '__main__':
    main()
