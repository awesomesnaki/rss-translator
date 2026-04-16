# RSS Translator

## 项目用途

这是一个 RSS 订阅源处理工具，部署在 GitHub Actions 上，输出托管在 GitHub Pages，供 Reeder 等 RSS 阅读器订阅。主要功能：

1. **翻译英文 RSS 为中文** — 使用 DeepSeek API，翻译全文而非摘要
2. **代理 RSSHub 源** — 通过 GitHub Actions 套壳访问 RSSHub 官方实例（`rsshub://` 协议），解决客户端直连 RSSHub 不稳定的问题
3. **优化 RSS 阅读体验** — 自动展开全文（`fetch_full_content`）、修复图片防盗链、还原懒加载图片、清理臃肿 HTML、V2EX 评论格式化等

## 架构

- `config.yaml` — Feed 配置（URL、是否翻译、是否抓全文、过滤规则）
- `translate_rss.py` — 核心脚本，处理所有 feed
- `cache.json` — 翻译缓存（key 为内容 MD5 hash），避免重复调用 API
- `feeds/*.xml` — 生成的 RSS 文件，通过 GitHub Pages 发布
- `.github/workflows/translate.yml` — 每 2 小时自动运行

## config.yaml 配置项

```yaml
- name: "feed名"           # 输出文件名，也是订阅链接的一部分
  url: "https://..."        # RSS 源 URL，支持 rsshub:// 协议
  translate: true           # 默认 true，中文源设为 false
  fetch_full_content: true  # 从原始网页抓取全文（默认 false）
  filter: "blockquote"      # 可选，按内容类型过滤条目
  filter_out: ["关键词"]     # 可选，标题含任一关键词则排除
  filter_out_content: ["关键词"] # 可选，正文含任一关键词则排除
  summarize_title: true     # 可选，长标题总结为简短中文短语（≤15字）
  images_only: true         # 可选，正文只保留图片，去掉所有文字
  text_only: true           # 可选，正文去掉所有图片，只保留文字
  filter_category: "xxx"    # 可选，只保留带该 <category> 标签的条目（可为字符串或列表）
  max_age_days: 2           # 可选，只保留发布时间在 N 天内的条目
```

## 关键设计决策

- **翻译缓存很重要** — `cache.json` 基于内容 hash。任何在翻译之前改变内容的操作（如 HTML 清理）都会导致缓存失效、全量重新翻译、workflow 超时。所以 HTML 清理必须放在翻译之后
- **3 线程并行** — 使用 ThreadPoolExecutor 同时处理 3 个 feed，显著缩短运行时间
- **图片修复** — 所有 `<img>` 添加 `referrerpolicy="no-referrer"`（防盗链）；自动将 `data-src` 等懒加载属性还原为 `src`
- **站点专用解析** — V2EX 帖子绕过 readability，用专用解析器提取主帖+评论，格式化为 `#楼层 用户名：内容`
- **HTML 清理** — 去掉 CSS class、简化 `<picture>` 为 `<img>`、展开无用 `<span>`/`<div>` wrapper，让 RSS 阅读器渲染更干净
- **标题总结** — `summarize_title` 用 DeepSeek 将长描述压缩为简短中文标题，适用于图片分享类 feed（如 some.pics），原始描述翻译后放入正文
- **纯图模式** — `images_only` 配合 `summarize_title` 使用，正文只保留 `<img>` 标签，去掉所有文字，适用于 Pixelfed 等摄影类 feed
- **关键词过滤** — `filter_out` 按标题排除、`filter_out_content` 按正文排除，用于去广告和不需要的内容类型（如 B 站视频嵌入）
- **分类过滤** — `filter_category` 按 RSS `<category>` 精确保留，适用于只想要源站某个栏目的场景（如 luobo8 只要"微语录精选"）
- **日期过滤** — `max_age_days` 按发布时间截断，适用于希望只看最近 N 天内容的场景
- **纯文字模式** — `text_only` 在 `fix_image_tags` 之后剥掉所有 `<img>`，适用于图片源防盗链无解、只想看文字的站点
- **RSSHub URL 策略** — `rsshub://` 协议走 CI 本地实例；直接写 `https://rsshub.app/...` 则走官方实例不被改写，适用于本地实例无法抓取的源

## 操作注意事项

- 新增 feed 只需在 `config.yaml` 添加条目，push 后 workflow 自动处理
- 中文源记得加 `translate: false`，否则会浪费 API 调用
- RSS 摘要不含图片/全文的源需要加 `fetch_full_content: true`
- 仓库是公开的，API key 存在 GitHub Secrets（`DEEPSEEK_API_KEY`），安全无泄露风险
- 订阅地址格式：`https://awesomesnaki.github.io/rss-translator/feeds/{name}.xml`
- Pixelfed 等图片 feed 建议配置 `summarize_title: true` + `images_only: true`，标题简洁正文纯图
- some.pics 等图文 feed 建议只用 `summarize_title: true`，原始描述翻译后保留在正文
- `filter_out` 和 `filter_out_content` 可叠加使用，先过滤标题再过滤正文
- 开发请在独立分支上进行，通过 PR 合并到 main
