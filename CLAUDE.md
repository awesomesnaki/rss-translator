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
```

## 关键设计决策

- **翻译缓存很重要** — `cache.json` 基于内容 hash。任何在翻译之前改变内容的操作（如 HTML 清理）都会导致缓存失效、全量重新翻译、workflow 超时。所以 HTML 清理必须放在翻译之后
- **3 线程并行** — 使用 ThreadPoolExecutor 同时处理 3 个 feed，显著缩短运行时间
- **图片修复** — 所有 `<img>` 添加 `referrerpolicy="no-referrer"`（防盗链）；自动将 `data-src` 等懒加载属性还原为 `src`
- **站点专用解析** — V2EX 帖子绕过 readability，用专用解析器提取主帖+评论，格式化为 `#楼层 用户名：内容`
- **HTML 清理** — 去掉 CSS class、简化 `<picture>` 为 `<img>`、展开无用 `<span>`/`<div>` wrapper，让 RSS 阅读器渲染更干净

## 操作注意事项

- 新增 feed 只需在 `config.yaml` 添加条目，push 后 workflow 自动处理
- 中文源记得加 `translate: false`，否则会浪费 API 调用
- RSS 摘要不含图片/全文的源需要加 `fetch_full_content: true`
- 仓库是公开的，API key 存在 GitHub Secrets（`DEEPSEEK_API_KEY`），安全无泄露风险
- 订阅地址格式：`https://awesomesnaki.github.io/rss-translator/feeds/{name}.xml`
- 开发请在独立分支上进行，通过 PR 合并到 main
