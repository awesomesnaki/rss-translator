# RSS Translator

## 项目用途

这是一个 RSS 订阅源处理工具，部署在 GitHub Actions 上，输出托管在 GitHub Pages，供 Reeder 等 RSS 阅读器订阅。主要功能：

1. **翻译英文 RSS 为中文** — 使用 DeepSeek API，翻译全文而非摘要
2. **代理 RSSHub 源** — 通过 GitHub Actions 套壳访问 RSSHub 官方实例（`rsshub://` 协议），解决客户端直连 RSSHub 不稳定的问题
3. **优化 RSS 阅读体验** — 自动展开全文（`fetch_full_content`）、修复图片防盗链、还原懒加载图片、清理臃肿 HTML、V2EX 评论格式化等
4. **抓取豆瓣榜单** — 独立爬虫脚本（不走 RSS 流程），从豆瓣移动端 API 抓取 subject_collection 榜单，生成带海报、metadata、短评的 RSS

## 架构

- `config.yaml` — Feed 配置（URL、是否翻译、是否抓全文、过滤规则）
- `translate_rss.py` — 核心脚本，处理所有 feed
- `site_sources.py` — 没有 RSS 的站点：调站点自己的数据接口生成 RSS，`url` 写 `{scheme}://参数`（如 `pubg://zh-cn`、`workspaces://setups`），之后照常走过滤/图片修复/输出流程
- `cache.json` — 翻译缓存（key 为内容 MD5 hash），避免重复调用 API
- `content_cache.json` — `fetch_full_content` 抓回来的正文快照（key 为文章链接），保证送去翻译的内容稳定
- `feed_health.py` — 健康检查：源停更、连续失败时往 `feeds/health.xml` 写告警（在 Reeder 里订阅），状态存 `health.json`
- `feeds/*.xml` — 生成的 RSS 文件，通过 GitHub Pages 发布
- `feeds/images/{collection}/` — 豆瓣海报，由 GitHub Pages 自托管避防盗链
- `.github/workflows/translate.yml` — 每天在 DeepSeek 谷时时段运行 10 次
- `douban_scraper.py` — 豆瓣榜单爬虫，独立于 translate_rss.py
- `.github/workflows/douban.yml` — 每周一 08:00（北京时间）自动运行

## config.yaml 配置项

```yaml
- name: "feed名"           # 输出文件名，也是订阅链接的一部分
  url: "https://..."        # RSS 源 URL，支持 rsshub:// 协议，以及 site_sources.py 注册的 scheme（如 pubg://zh-cn）
  translate: true           # 默认 true，中文源设为 false
  fetch_full_content: true  # 从原始网页抓取全文（默认 false）
  filter: "blockquote"      # 可选，按内容类型过滤条目
  filter_out: ["关键词"]     # 可选，标题含任一关键词则排除
  filter_in: ["关键词"]      # 可选，标题含任一关键词才保留（白名单）
  filter_out_content: ["关键词"] # 可选，正文含任一关键词则排除
  filter_in_content: ["关键词"]  # 可选，正文含任一关键词才保留（正文白名单）
  summarize_title: true     # 可选，长标题总结为简短中文短语（≤15字）
  translate_title: false    # 可选，标题保留原文不翻译（正文照常翻译），适用于以人名为标题的源
  images_only: true         # 可选，正文只保留图片，去掉所有文字
  text_only: true           # 可选，正文去掉所有图片，只保留文字
  filter_category: "xxx"    # 可选，只保留带该 <category> 标签的条目（可为字符串或列表）
  self_host_images: true    # 可选，图片下载到 feeds/images/{name}/ 自托管，绕过源站 CDN 防盗链
  max_entries: 20           # 可选，每次处理的条目数上限（默认 5，带过滤时 50，白名单过滤时不限）
  stale_days: 90            # 可选，健康检查的停更阈值（天），默认按发文节奏自动算，0 = 不检查
```

## douban_scraper.py 配置

榜单列表硬编码在脚本顶部 `COLLECTIONS`，加新榜单加一项即可：

```python
{
    'slug': 'movie_weekly_best',          # 豆瓣 URL /subject_collection/{slug} 后面的 slug
    'name': 'douban-movie-weekly',         # 输出文件名 (feeds/{name}.xml)
    'title': '豆瓣 一周口碑电影榜',         # feed 标题
}
```

调用的豆瓣 API：
- 列表: `m.douban.com/rexxar/api/v2/subject_collection/{slug}/items`
- 详情: `m.douban.com/rexxar/api/v2/movie/{id}` — intro / directors / actors / aka / pubdate / imdb / honor_infos / tags 等
- 短评: `m.douban.com/rexxar/api/v2/movie/{id}/interests?order_by=hot`

每条 RSS item 含：海报（自托管）→ 评分 `<h3>⭐ 8.5</h3>` → metadata 块（时间/地区/类型/季数/集数/导演/主演/语言/片长/上映/又名/标签/IMDb，标签加粗 `<br>` 分隔）→ 剧情 → 5 条热门短评（用户名 ⭐⭐⭐⭐ + 评论 + 👍 N）。

剧集额外字段：`current_season` / `season_count` 取 >1 时显示 `第 N 季 / 共 N 季`（单季剧不显示），`episodes_count` 显示 `N 集`。豆瓣不同接口字段名不固定，`season_episode_text` 多个候选都试一遍。

`DOUBAN_COOKIE` (GitHub Secrets) 可选，反爬限流时携带账号 cookie 绕过。

## 关键设计决策

- **翻译缓存很重要** — `cache.json` 基于内容 hash。`translate_html_content` 先用**原始 HTML** 的 hash 查整篇缓存，所以任何改变「送进 `translate_html_content` 之前的内容」的操作（比如在主流程里提前清理 HTML）仍然会让整篇缓存失效；好在没命中后还有段落级缓存兜底（见下一条），只要段落文字没变就不会真的重翻。`clean_readability_html` 仍然放在翻译之后
- **翻译前瘦身 + 段落级缓存** — 以前按「顶层元素凑够 3000 字一组」翻译、按组缓存，有两个漏洞：作者在中间插一段或改个错字，插入点之后所有组的边界都会移位 → 几乎整篇重翻（basicappleguy 一篇边用边记的长文作者追加了 8 次，重翻量是新文的 1.8 倍；DF 的 Gruber 经常改稿）；class/id/data-*/`<picture><source>` 这些标记模型要读一遍、原样再输出一遍，翻译后又被删掉，而且上游只改模板、正文一字没变时（Substack、Squarespace）hash 也会变 → 整篇重翻。现在整篇缓存没命中时：`slim_html` 先瘦身（只留 href/src/alt 等语义属性，图片和媒体标签原样不动），`split_segments` 切段（每个块级元素一段，连续的行内内容合成一段，包着块级元素的 div/section/article 和 readability 的 html/body 外壳会被展开），每段单独缓存（key 前缀 `seg:`），只把没命中的段落按连续区间打包送翻（每包 ≤3000 字，调用次数不变）。译文用同一个切分器切回去，逐段核对标签名对得上才按段写缓存；对不上（模型合并/拆分了段落）就整包原样使用，效果等同于以前的按组缓存，不会多花一次调用——所以不依赖模型保留任何特殊标记。没有英文字母的段落（纯图片、分隔线）不送翻。用 14 个源各 5 篇真实文章回放对比旧算法：新文章送翻字符少 19%，中间插一段时重发量少 96%，改一个错字少 90%，只改标记的模板变化从整篇重翻变成零调用；最终输出的图片、链接、段落、表格等结构元素与旧算法逐篇一致。上线时已在 feed 窗口里的文章靠整篇缓存照常命中，不会全量重翻
- **缓存淘汰** — 缓存只增不减曾涨到 100MB 撞上 GitHub 单文件限制、push 被拒（GH001）。现在缓存格式为 `{key: {"v": 译文, "t": 最后使用日期}}`，每次命中/写入刷新日期，保存时淘汰超过 `CACHE_RETENTION_DAYS`（30 天）未使用的条目。feed 窗口内的条目每次运行都命中所以永远保鲜；旧格式字符串条目加载时视为日期未知，未命中即被清掉（一次性迁移瘦身）。序列化用紧凑格式（无缩进）
- **3 线程并行** — 使用 ThreadPoolExecutor 同时处理 3 个 feed，显著缩短运行时间
- **图片修复** — 所有 `<img>` 添加 `referrerpolicy="no-referrer"`（防盗链）；自动将 `data-src` 等懒加载属性还原为 `src`
- **站点专用解析** — V2EX 帖子绕过 readability，用专用解析器提取主帖+评论，格式化为 `#楼层 用户名：内容`；张洪Heo 博客（Butterfly 主题）只取 `#article-container` 正文容器，天然排除作者卡片/打赏/版权声明/「最近发布」等噪音，并把 `data-lazy-src` 懒加载图片还原成 `src`；该博客不补 og:image 封面图（其 og:image 常是站点头像而非正文图）
- **HTML 清理** — 去掉 CSS class、简化 `<picture>` 为 `<img>`、展开无用 `<span>`/`<div>` wrapper，让 RSS 阅读器渲染更干净
- **标题总结** — `summarize_title` 用 DeepSeek 将长描述压缩为简短中文标题，适用于图片分享类 feed（如 some.pics），原始描述翻译后放入正文
- **纯图模式** — `images_only` 配合 `summarize_title` 使用，正文只保留 `<img>` 标签，去掉所有文字，适用于 Pixelfed 等摄影类 feed
- **关键词过滤** — `filter_out` 按标题排除、`filter_in` 按标题白名单保留、`filter_out_content` 按正文排除、`filter_in_content` 按正文白名单保留，用于去广告和不需要的内容类型（如 B 站视频嵌入）
- **正文白名单去广告** — `filter_in_content` 用于「广告无规律、但想要的内容有共同正文特征」的源。典型是 4K影视屋 Telegram（dianying4k）：广告多为博彩 emoji 标题（😂😂😂 / 🤩🤩 / Y3国际 / 6G.COM 等），标题毫无规律，黑名单是打地鼠；而真正的资源贴正文必带网盘下载链接。于是反向白名单 `filter_in_content: ["pan.quark.cn", "pan.baidu.com", "pan.xunlei.com", "115cdn.com"]`，只保留含网盘链接的条目，一条规则同时干掉广告 + 预告/官宣/特辑等非资源贴，且对未来新广告形态天然免疫。比按标题 `filter_in: ["名称"]` 更稳——少数从二级频道转发的资源贴标题没有「名称：」前缀，但正文一样有网盘链接，不会误删。`filter_in`/`filter_in_content` 触发时 `max_entries=None`（扫描源返回的全部条目，因为白名单会大幅缩减数量）
- **分类过滤** — `filter_category` 按 RSS `<category>` 精确保留，注意部分第三方 feed 服务（如 feed.luobo8.com）不含 category 标签，此时应改用 `filter_in` 按标题过滤
- **纯文字模式** — `text_only` 在 `fix_image_tags` 之后剥掉所有 `<img>`，适用于图片源防盗链无解、只想看文字的站点
- **图片自托管** — `self_host_images` 适用于源站图片 CDN 防盗链严格、`referrerpolicy="no-referrer"` 也救不回来的源（如少数派 `cdnfile.sspai.com`，源站不接受空 Referer）。开启后在 `fix_image_tags` 之后把正文图片下载到 `feeds/images/{name}/`，下载时带源站 Referer 绕过防盗链，`<img src>` 改写为 GitHub Pages URL。文件名为图片 URL 的 md5，已下载的命中缓存跳过。图片清理用**保留期**机制（`SELF_HOST_RETENTION_DAYS`，默认 30 天）：图片滚出 feed 窗口后不立即删，再保留 30 天才删——因为 RSS 阅读器（Reeder 等）会长期缓存历史条目、仍指着这些本地图，删早了老文章图片就 404（少数派踩过的坑）。每张图最后一次出现在 feed 的日期记在 `feeds/images/{name}/.image_manifest.json`（CI 每次全新 clone，文件 mtime 不可靠，保留期只能靠它自己持久化）。这点不同于豆瓣海报的「离榜即删」。下载失败则保留原直链，下次运行重试
- **模型：deepseek-flash + 关闭思考模式** — 用不带版本号的 `deepseek-flash`，DeepSeek 会把它指向当前最新的 Flash（2026-11 是 DeepSeek-V4.1-Flash-1017），不用跟着改代码。以前写死的 `deepseek-v4-flash` 已下线，只是旧名字还被接受、转给新模型，哪天不接受了翻译会全部失败。每次运行先查 `/models`（`resolve_model`），优先 `deepseek-flash`，没有就选 id 里带 flash 的，查不到就用默认名；实际在服务的版本名记在 `health.json`，变了会在健康检查 feed 里通知（新版本价格可能不同，记得同步 `PRICE_PER_M`）。所有调用都走 `chat()`，显式传 `thinking: {"type": "disabled"}`：flash 默认开思考模式，翻译用不着推理，思维链却按输出 token 计费（输出单价是输入的 4 倍），还和译文共用 `max_tokens`——标题总结只给 100 token，思维链先吃光额度、正文为空，就退回成整段翻译（somepics 部分标题是一整段译文就是这么来的）。切换模型不影响翻译缓存（key 是内容 hash），已翻译的不会重翻
- **谷时调度** — DeepSeek 按峰谷计费，谷时单价是峰时的一半。峰时是**北京时间周一至周五** 09:00-12:00、14:00-18:00，其余（含整个周末）都是谷时。换算成 cron 用的 UTC 是周一至周五 01:00-04:00、06:00-10:00（北京时间减 8 小时不跨日，星期几不用平移）。工作日的 cron 避开这两个窗口，并且不排在峰时开始前一小时内（否则一次运行会跨进峰时）；周末全天谷时，没有窗口要避，照每 2 小时跑。代价是工作日 UTC 04:00 → 10:00 有个 6 小时空档
- **max_entries 与运行间隔配套** — 默认窗口 5 条。运行间隔拉长后，更新快的源会在两次运行之间推出超过 5 条，多出来的就永远漏掉了。cnbeta（约 25 条/天）、v2ex-hot（约 12 条/天）、landiannews、sspai、somepics 因此显式配了更大的 `max_entries`。加新源或再改调度间隔时都要按「源的日更条数 × 最大间隔」重新核一遍。注意 GitHub 并不会执行每一次定时任务：整点的 schedule 在高峰期会被丢弃或延迟，2026-10 实测每天只跑了约 4.8 次（排了 10–12 次），最长间隔约 11 小时，启动延迟最多近 1 小时。核算时按实际最大间隔，而不是 cron 上写的间隔
- **全文抓取必须稳定，否则翻译缓存形同虚设** — 这是历史上最大的 token 黑洞。`fetch_full_content` 每次运行都重新抓原网页，而抓取结果并不稳定：超时截断、Cloudflare 拦截页、模板噪音，都会改变内容 hash → 翻译缓存全部失效 → 整篇重新翻译。franzgraf（The Beauty of Nature，已移除）和 workspaces 两个源在两个月里几乎没有新文章，却吃掉了九成 token（workspaces 后来查明另有元凶，见下面「Workspaces 改走 Sanity 接口」）——franzgraf 同一篇文章的正文长度在 0 / 379 / 989 / 5400 字之间反复横跳，每次运行重翻一遍。现在抓取结果先经过 `stable_full_content` 和 `content_cache.json` 里的上一版快照对齐：可见文字相同 → 沿用旧正文（翻译缓存照常命中）；旧正文完整出现在新正文里 → 作者真的追加了内容，重新翻译；明显变短 → 按抓取降级处理，要连续 3 次抓到一模一样的短正文才认（抓取抖动每次截断位置都不同，凑不齐 3 次；有的源会稳定截断在同一位置，所以 2 次确认不够，会让 feed 在完整版和半截版之间来回翻）。副作用是 feed 里不会再出现空正文和半截文章
- **content_cache.json 保留期只有 7 天** — 存的是整篇 HTML，不能像翻译缓存那样开 30 天，否则文件会涨到几十 MB（`cache.json` 撞过 GitHub 单文件限制）。7 天足够覆盖 feed 窗口内的文章
- **网页编码：UTF-8 优先，不信 charset_normalizer** — 以前 `fetch_full_article` 无条件 `resp.encoding = resp.apparent_encoding`，charset_normalizer 会把明明是 UTF-8 的页面猜成 Windows-1252，正文变成「é»„é“ç›Š」「â€™」式乱码（V2EX、蓝点网、testingcatalog 都中过；cp1252 未定义的字节变成 �，是有损的，没法反向修复）。现在 `decode_html` 先按 UTF-8 严格解码，失败才用 Content-Type 声明的 charset，最后才让它猜。更阴险的是乱码比原文长三倍，之后抓到的正确正文会被 `stable_full_content` 当成「明显变短 = 抓取降级」挡掉，V2EX 热帖评论一直在涨、凑不齐 3 次相同短正文，乱码就锁死在 `content_cache.json` 里。所以 `looks_mojibake` 两头把关：抓回来的页面有乱码特征就当抓取失败，加载 `content_cache.json` 时有乱码特征的快照直接丢弃重抓
- **翻译失败绝不写缓存** — 以前 API 出错时 `translate_with_deepseek` 会原样返回英文原文，然后被当成译文永久缓存，一次网络抖动能污染到缓存过期。模型偶尔还会拒答、回一句「请提供需要翻译的内容」，同样会被缓存下来顶掉正文或标题（已移除的 franzgraf 源标题就这样坏了很久）。现在失败和拒答一律返回 `None`，本次运行用原文顶上、不写缓存，下次运行重试；加载缓存时也会顺手清掉历史上被污染的条目
- **无 RSS 站点走数据接口，不走 CSS 选择器** — PUBG 官网新闻（`pubg://zh-cn`）是 Nuxt 渲染，新闻卡片连 href 都没有，FetchRSS / RSSHub `transform/html` 那种按选择器抓网页的方式拿不到链接。改为直接调官网背后的 Krafton 内容接口 `api-foc.krafton.com/content/post/news`（必须带 `Service-Namespace: PUBG_OFFICIAL` / `Service-Game: pubg` 头，否则 401），详情接口直接给完整正文 HTML，比 readability 抠网页稳定，所以不需要 `fetch_full_content`。接口时间是 UTC。详情获取失败的条目本次跳过而不是回退成摘要（阅读器不会更新已收录条目的正文）。标题命中 `filter_out` 的条目连详情都不请求。以后再加没有 RSS 的站点，先在浏览器开发者工具里找页面背后的 JSON 接口，在 `site_sources.py` 的 `SOURCES` 里注册一个 scheme
- **Workspaces 改走 Sanity 接口** — Workspaces 2026 年 7 月从 beehiiv 搬到自建的 Astro 站 workspaces.xyz，旧的 `rss.beehiiv.com` feed 停在 #534 不再更新，新站没有 RSS（`/feed`、`/rss.xml`、`/atom.xml` 等全 404），订阅悄无声息地停了 4 个月才发现。正文存在 Sanity CMS 里，数据集公开可读，`workspaces://setups` 用一条 GROQ 查询（`ui5qde1a.apicdn.sanity.io`）拿最近几期的结构化字段，按网站的排版拼正文：头图 → 头衔/简介/链接 → 每段问答后跟一张照片 → 剩余照片 → 装备清单 → 软件；不取 `censusFacts`（站内统计）、`featuredGear`（推广块）、`heroQuote`/`pullQuote`（问答里摘出来的重复引语）。早期条目的 `guestBio` 是纯字符串而不是 Portable Text，渲染器两种都认。标题是「#期号 人名 · 头衔」并配 `translate_title: false`——旧 feed 只用人名、交给翻译，结果音译很不稳定（Jack Zerby 保留、Jack Raines 变「杰克·雷恩斯」、Tiancheng Luo 被猜成「罗天成」）。图片在 `cdn.sanity.io`，不校验 Referer，不需要自托管。如果哪天 Sanity 数据集改成私有，这个源会直接报错（主流程不覆盖旧 XML），比悄悄停更好发现
- **beehiiv 正文里藏着每天变的时间戳** — 旧 workspaces 源停更后依然每天全量重翻 5 篇旧文：beehiiv 每篇正文 footer 的「Powered by beehiiv」链接带 `publication_logo=...pfp.png?v={时间戳}`，beehiiv 每天重建 feed 都会更新它，整篇 HTML hash 跟着变 → 翻译缓存全失效。正文外层只有一个 `<div class="beehiiv">`，按顶层元素分组翻译也拆不开，15K 字符整块重来。`stable_full_content` 也没挡住：抓回来的网页没图，代码走「RSS 自带图更多就用 RSS 内容」分支，RSS 内容不经过快照对齐。以后再接 beehiiv 源，要在翻译前剥掉 `beehiiv__footer`（一次性缓存失效，之后稳定）
- **RSSHub URL 策略** — `rsshub://` 协议走 CI 本地实例；直接写 `https://rsshub.app/...` 则走官方实例不被改写，适用于本地实例无法抓取的源。别用来路不明的第三方 RSSHub 实例：AnthropicNews 原来用 `rsshub.bestblogs.dev`，它 2026-10-18 起冻结、一直返回旧缓存（`lastBuildDate` 不再变），HTTP 200 照常，从外面看不出坏了，漏了 3 周才发现，已改回 `rsshub://anthropic/news`
- **健康检查** — 源坏掉时这个项目不会报错，只是 feed 不再出新条目，看起来和作者没写一样（workspaces 停了 4 个月、AnthropicNews 停了 3 周才被发现）。`feed_health.py` 每次运行后检查两类问题，告警写进 `feeds/health.xml`：**停更**——上游很久没有新链接，阈值 = max(发文间隔中位数 × 4, 90 分位间隔 × 2)，限制在 14–60 天，节奏未知时 30 天，`stale_days` 可覆盖（0 关闭）。只用中位数的话「一阵密集、一阵停很久」的作者（dereksivers、appaddict、pixelfed）会被频繁误报，拿各源上游的完整历史回测，每年约 24 次误报，加上 90 分位后约 12 次，日更源仍然 14 天就报。「有没有新内容」以上游条目链接为准而不是 pubDate（有的源 pubDate 缺失、乱序，或者被填成抓取时间），只在第一次观察某个源时参考 pubDate，这样已经停更的源上线当天就能报出来；**连续失败**——处理一直抛异常超过 24 小时（偶发的一两次失败多半是网络抖动）。每个问题只报一次，没恢复的话每 7 天提醒一次；失败恢复会通知，停更恢复不通知（新文章本身会出现在阅读器里）。状态存 `health.json`，只在有事件时变化。健康检查本身出错只打日志，不影响翻译结果提交。douban 的 feed 由独立 workflow 生成，不在检查范围内
- **DeepSeek API 告警** — API 出错时 `translate_with_deepseek` 返回 None、正文用英文原文顶上，源本身照常「成功」，按源的健康检查看不出来；余额用完（402）、key 失效（401）、模型名下线都会这样。而阅读器不会更新已收录的条目，英文原文会永久留在那，所以连续 2 次运行所有调用都失败就告警（不等 24 小时），部分失败当抖动不报，恢复后通知
- **上游为空当失败处理** — 上游 feed 一条都没有，基本都是出错了（RSSHub 路由报错、源站挂了、连接被重置）。以前会照常生成一个空 feed，把已经发布的条目全覆盖掉；现在 `translate_feed` 直接抛异常，保留旧 XML，计入健康检查的失败次数
- **豆瓣图片自托管** — wsrv.nl 等代理对 doubanio 返回 404（海外 IP 段被拦），改为 Actions 直接下载海报到 `feeds/images/{name}/`，由 GH Pages 提供，零防盗链。每次运行只保留本期榜单的图，已离榜的自动清理
- **豆瓣海报下载重试** — `download_image` 每个 URL 重试 3 次（1s/2s backoff），并按 `pic.large` → `pic.normal` → `cover_url` → `pic.small` 顺序 fallback。任一组合成功即可。下载彻底失败的条目从 `name_map` 移除，正文不带图（不回落 doubanio 直链，防盗链拉不到）
- **豆瓣标签字段不固定** — 不同接口字段名不一致（honor_infos / tags / subject_tags / topic_tags / content_tags），`extract_tags` 多个候选都试一遍去重
- **秘钥打码** — GitHub Push Protection 会扫描 push 内容，源站正文里出现泄露的云服务秘钥（如 V2EX 讨论泄露事件的帖子贴出腾讯云 AKID）会随抓取进入 `feeds/*.xml` 和 `content_cache.json`，push 被拒（GH013）、workflow 连续失败且重试无用（帖子在热榜窗口里一直在）。`redact_secrets` 在内容进入翻译/缓存/feed 之前把常见秘钥格式（腾讯云 AKID、AWS AKIA、Google AIza、GitHub token、阿里云 LTAI、Slack xox）统一打码。打码是确定性且幂等的，不会造成内容 hash 抖动
- **Push 重试** — translate.yml 和 douban.yml 都 push 到 main，可能撞上 PR merge 或对方 workflow 的提交。两边 commit step 都加了「push 失败 → rebase origin/main → 重试」循环（最多 3 次，3/6/9s sleep）

## 操作注意事项

- 新增 feed 只需在 `config.yaml` 添加条目，push 后 workflow 自动处理（源的名字不能叫 `health`，那是健康检查的输出）
- 健康检查订阅地址：`https://awesomesnaki.github.io/rss-translator/feeds/health.xml`。收到停更告警先去源站看是不是真的没更新：作者停更就给这个源加 `stale_days` 调大阈值，或设为 0 关闭；源站改版、搬家、RSSHub 实例冻结就换源
- 中文源记得加 `translate: false`，否则会浪费 API 调用
- RSS 摘要不含图片/全文的源需要加 `fetch_full_content: true`
- 仓库是公开的，API key 存在 GitHub Secrets（`DEEPSEEK_API_KEY`），安全无泄露风险
- 订阅地址格式：`https://awesomesnaki.github.io/rss-translator/feeds/{name}.xml`
- Pixelfed 等图片 feed 建议配置 `summarize_title: true` + `images_only: true`，标题简洁正文纯图
- some.pics 等图文 feed 建议只用 `summarize_title: true`，原始描述翻译后保留在正文
- `filter_out` 和 `filter_out_content` 可叠加使用，先过滤标题再过滤正文
- 图片 CDN 防盗链严格、`no-referrer` 无效的源（如少数派），加 `self_host_images: true` 把图下载到本仓库自托管，代价是仓库体积会随运行增长
- 开发请在独立分支上进行，通过 PR 合并到 main
- 改 `translate.yml` 的 cron 时注意别排进 DeepSeek 峰时（UTC 周一至周五 01:00-04:00、06:00-10:00），也别排在峰时开始前一小时内（运行会跨进去）；周末不受限
- 看真实用量：每次运行结束 Actions 日志里有「DeepSeek 用量」一行（调用次数、输入/缓存命中/输出/推理 tokens、按空闲时段价的费用估算），下面按源列出，按费用排序。「推理」不为 0 说明思考模式没关掉
- 排查 token 消耗：`git log` 里逐个 commit 对比 `feeds/*.xml`，区分「新 guid」和「同一 guid 正文变了」——后者就是缓存失效在重复翻译，是真正的花钱大户。Actions 日志里出现「译文段落对不上」说明模型改动了段落结构、那一包没能按段缓存，偶尔出现正常，某个源频繁出现就值得看看它的 HTML 结构。源站已停更、却每次都有 commit 的 feed 尤其可疑。按条目对比时注意正文里随时间变化的 URL 参数（如 beehiiv 的 `?v=` 时间戳）
- 加新豆瓣榜单：`douban_scraper.py` 顶部 `COLLECTIONS` 列表加一项，slug 即豆瓣 URL `/subject_collection/` 后面那部分
- 豆瓣订阅地址：`https://awesomesnaki.github.io/rss-translator/feeds/douban-{collection}.xml`，不进 config.yaml 流程
