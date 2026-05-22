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
- `cache.json` — 翻译缓存（key 为内容 MD5 hash），避免重复调用 API
- `feeds/*.xml` — 生成的 RSS 文件，通过 GitHub Pages 发布
- `feeds/images/{collection}/` — 豆瓣海报，由 GitHub Pages 自托管避防盗链
- `.github/workflows/translate.yml` — 每 2 小时自动运行
- `douban_scraper.py` — 豆瓣榜单爬虫，独立于 translate_rss.py
- `.github/workflows/douban.yml` — 每周一 08:00（北京时间）自动运行

## config.yaml 配置项

```yaml
- name: "feed名"           # 输出文件名，也是订阅链接的一部分
  url: "https://..."        # RSS 源 URL，支持 rsshub:// 协议
  translate: true           # 默认 true，中文源设为 false
  fetch_full_content: true  # 从原始网页抓取全文（默认 false）
  filter: "blockquote"      # 可选，按内容类型过滤条目
  filter_out: ["关键词"]     # 可选，标题含任一关键词则排除
  filter_in: ["关键词"]      # 可选，标题含任一关键词才保留（白名单）
  filter_out_content: ["关键词"] # 可选，正文含任一关键词则排除
  summarize_title: true     # 可选，长标题总结为简短中文短语（≤15字）
  images_only: true         # 可选，正文只保留图片，去掉所有文字
  text_only: true           # 可选，正文去掉所有图片，只保留文字
  filter_category: "xxx"    # 可选，只保留带该 <category> 标签的条目（可为字符串或列表）
  self_host_images: true    # 可选，图片下载到 feeds/images/{name}/ 自托管，绕过源站 CDN 防盗链
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

- **翻译缓存很重要** — `cache.json` 基于内容 hash。任何在翻译之前改变内容的操作（如 HTML 清理）都会导致缓存失效、全量重新翻译、workflow 超时。所以 HTML 清理必须放在翻译之后
- **3 线程并行** — 使用 ThreadPoolExecutor 同时处理 3 个 feed，显著缩短运行时间
- **图片修复** — 所有 `<img>` 添加 `referrerpolicy="no-referrer"`（防盗链）；自动将 `data-src` 等懒加载属性还原为 `src`
- **站点专用解析** — V2EX 帖子绕过 readability，用专用解析器提取主帖+评论，格式化为 `#楼层 用户名：内容`；张洪Heo 博客（Butterfly 主题）只取 `#article-container` 正文容器，天然排除作者卡片/打赏/版权声明/「最近发布」等噪音，并把 `data-lazy-src` 懒加载图片还原成 `src`
- **HTML 清理** — 去掉 CSS class、简化 `<picture>` 为 `<img>`、展开无用 `<span>`/`<div>` wrapper，让 RSS 阅读器渲染更干净
- **标题总结** — `summarize_title` 用 DeepSeek 将长描述压缩为简短中文标题，适用于图片分享类 feed（如 some.pics），原始描述翻译后放入正文
- **纯图模式** — `images_only` 配合 `summarize_title` 使用，正文只保留 `<img>` 标签，去掉所有文字，适用于 Pixelfed 等摄影类 feed
- **关键词过滤** — `filter_out` 按标题排除、`filter_in` 按标题白名单保留、`filter_out_content` 按正文排除，用于去广告和不需要的内容类型（如 B 站视频嵌入）
- **分类过滤** — `filter_category` 按 RSS `<category>` 精确保留，注意部分第三方 feed 服务（如 feed.luobo8.com）不含 category 标签，此时应改用 `filter_in` 按标题过滤
- **纯文字模式** — `text_only` 在 `fix_image_tags` 之后剥掉所有 `<img>`，适用于图片源防盗链无解、只想看文字的站点
- **图片自托管** — `self_host_images` 适用于源站图片 CDN 防盗链严格、`referrerpolicy="no-referrer"` 也救不回来的源（如少数派 `cdnfile.sspai.com`，源站不接受空 Referer）。开启后在 `fix_image_tags` 之后把正文图片下载到 `feeds/images/{name}/`，下载时带源站 Referer 绕过防盗链，`<img src>` 改写为 GitHub Pages URL。文件名为图片 URL 的 md5，已下载的命中缓存跳过；每次运行结束清理掉本期 feed 不再引用的旧图（同豆瓣海报的思路）。下载失败则保留原直链，下次运行重试
- **RSSHub URL 策略** — `rsshub://` 协议走 CI 本地实例；直接写 `https://rsshub.app/...` 则走官方实例不被改写，适用于本地实例无法抓取的源
- **豆瓣图片自托管** — wsrv.nl 等代理对 doubanio 返回 404（海外 IP 段被拦），改为 Actions 直接下载海报到 `feeds/images/{name}/`，由 GH Pages 提供，零防盗链。每次运行只保留本期榜单的图，已离榜的自动清理
- **豆瓣海报下载重试** — `download_image` 每个 URL 重试 3 次（1s/2s backoff），并按 `pic.large` → `pic.normal` → `cover_url` → `pic.small` 顺序 fallback。任一组合成功即可。下载彻底失败的条目从 `name_map` 移除，正文不带图（不回落 doubanio 直链，防盗链拉不到）
- **豆瓣标签字段不固定** — 不同接口字段名不一致（honor_infos / tags / subject_tags / topic_tags / content_tags），`extract_tags` 多个候选都试一遍去重
- **Push 重试** — translate.yml 和 douban.yml 都 push 到 main，可能撞上 PR merge 或对方 workflow 的提交。两边 commit step 都加了「push 失败 → rebase origin/main → 重试」循环（最多 3 次，3/6/9s sleep）

## 操作注意事项

- 新增 feed 只需在 `config.yaml` 添加条目，push 后 workflow 自动处理
- 中文源记得加 `translate: false`，否则会浪费 API 调用
- RSS 摘要不含图片/全文的源需要加 `fetch_full_content: true`
- 仓库是公开的，API key 存在 GitHub Secrets（`DEEPSEEK_API_KEY`），安全无泄露风险
- 订阅地址格式：`https://awesomesnaki.github.io/rss-translator/feeds/{name}.xml`
- Pixelfed 等图片 feed 建议配置 `summarize_title: true` + `images_only: true`，标题简洁正文纯图
- some.pics 等图文 feed 建议只用 `summarize_title: true`，原始描述翻译后保留在正文
- `filter_out` 和 `filter_out_content` 可叠加使用，先过滤标题再过滤正文
- 图片 CDN 防盗链严格、`no-referrer` 无效的源（如少数派），加 `self_host_images: true` 把图下载到本仓库自托管，代价是仓库体积会随运行增长
- 开发请在独立分支上进行，通过 PR 合并到 main
- 加新豆瓣榜单：`douban_scraper.py` 顶部 `COLLECTIONS` 列表加一项，slug 即豆瓣 URL `/subject_collection/` 后面那部分
- 豆瓣订阅地址：`https://awesomesnaki.github.io/rss-translator/feeds/douban-{collection}.xml`，不进 config.yaml 流程
