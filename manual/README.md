# 手动 arXiv / Blog 列表

在本目录或子目录放置 `.txt` 文件，每行写一个 arXiv 编号或完整 Blog
文章地址，然后运行：

```bash
bash run/run_airhub.sh --action manual-import
```

支持裸编号、`arXiv:` 前缀、arXiv abs/pdf/html URL 和 `http(s)` Blog
文章 URL；空行和以 `#` 开头的行会忽略。arXiv 会准备本地 Article 并进入
待解读队列；个人 Blog 和 `*.github.io` 等静态站会保存自包含 HTML 到
`attachments/blog/`；GitHub/Hugging Face 链接只登记、不下载。所有 Blog
均把 `scheme://host[:port]` 主站加入 `data/blog/sites.json`。

菜单 15 生成的 `manual/<结束时间>.txt` 与此格式完全兼容。
