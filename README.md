# AirHub

AirHub 是一个面向 Linux 的个人 AI 研究资料中枢，用于收集、筛选、准备和阅读
技术资料。它支持 arXiv 论文处理流水线、Blog 自包含网页归档、小红书帖子免登录
纯文本批量提取，以及小宇宙音频转录和 DeepSeek/Codex 文本校订。

本公开仓库只包含源代码、可复现的依赖定义、Codex skill、测试和配置模板；不包含
任何凭据、论文、网页归档、音频、模型权重、缓存、日志或生成结果。

## 环境要求

- Linux 和 Bash；
- Python 3.9 或更高版本，并支持 `venv`；
- 论文解读、XHS 分类和转录校订需要安装 `codex` CLI；
- 本机执行小宇宙 Whisper 转录需要支持 CUDA 的 NVIDIA GPU；
- 如果使用可选的 Whisper 功能，建议至少预留 20 GB 磁盘空间。

XHS 适配器使用独立且带锁文件的 Python 3.12 环境；需要时由 `uv` 自动获取对应
解释器。Whisper 依赖与模型文件同样保存在项目内被 Git 忽略的目录中。

## 安装与启动

```bash
git clone https://github.com/LaiAng8086/AirHub.git
cd AirHub
bash run/setup_airhub.sh
cp config/deepseek.example.json config/deepseek.json
chmod 600 config/deepseek.json
bash run/run_airhub.sh
```

调用 AI skill 前，请在 `config/deepseek.json` 中填写自己的 API key。
`run/run_airhub.sh` 会在首次运行时自动创建核心 Python 环境。所有缓存默认位于
仓库内的 `cache/`；如有需要，也可以通过标准环境变量 `PIP_CACHE_DIR`、
`HF_HOME`、`MODELSCOPE_CACHE` 和 `UV_CACHE_DIR` 修改缓存位置。

若要根据个人参考文献库建立优选频度表，请从 Zotero 导出 CSV 并放入 `scope/`。
系统只读取修改时间最新的 CSV。`scope/example.csv` 仅提供可接受的表头示例，
不包含任何个人文献记录。

## 主要功能

交互式菜单提供以下功能：

1. 从最新 scope CSV 更新作者、机构优选频度，并发现候选文章；
2. 使用优选策略或固定策略筛选文章并准备本地原文；
3. 通过内置 `paper-digest` skill 批量生成论文中文解读；
4. 手动导入 arXiv 编号或 Blog 地址，保存 Blog 自包含网页，并查询或删除 Blog
   主站列表；GitHub 和 Hugging Face 链接只登记而不下载；
5. 免登录批量提取 XHS 帖子纯文本，保存到精确到秒的 `cache/xhs/` 子目录；
   下载前确认有效链接数，处理时逐帖显示进度、原帖标题和成功/失败状态；
6. 处理全部 XHS 文本缓存，识别 Blog URL 或经核验的 arXiv ID，将结果写入按
   结束时间命名的 `manual/*.txt`；无论成功或失败，处理后均清理本次缓存；
7. 小宇宙短信登录、公开单集下载、本机 `whisper-large-v3-turbo` 转录，以及
   多说话人 HTML 文本校订；
8. 从已解读论文回灌作者和机构频度权重，并管理文章队列和每日执行状态。

常用非交互式命令示例：

```bash
bash run/run_airhub.sh --action prepare
bash run/run_airhub.sh --action priority
bash run/run_airhub.sh --action fixed
bash run/run_airhub.sh --action paper-digest
bash run/run_airhub.sh --action manual-import
bash run/run_airhub.sh --action xhs-download
bash run/run_airhub.sh --action xhs-classify
bash run/run_airhub.sh --action xiaoyuzhou-podcast
bash run/run_airhub.sh --action priority-feedback
```

脚本参数、运行目录和故障恢复说明见 [run/usage.md](run/usage.md)。运行离线测试：

```bash
bash run/run_tests.sh
```

## 配置与数据边界

- `config/settings.json`：每日文章上限；
- `config/sources.json`：来源查询和原文准备阈值；
- `config/filters.yaml`：固定策略规则。地点条件为国家 **OR** 机构；彼此独立且
  已配置的规则组之间使用 **AND**；
- `config/deepseek.json`：本地 API 凭据，默认被 Git 忽略；
- `config/xiaoyuzhou_credentials.json`：自动生成的小宇宙登录态，默认被忽略；
- `manual/`：用户手动输入列表与 XHS 分类输出，生成的 txt 默认被忽略；
- `attachments/`、`data/`、Article 队列、缓存、工作目录和日志均为运行态数据，
  默认不会进入 Git。

XHS 提取器只读取公开 HTML，并使用空 Cookie 容器。小宇宙音频下载器只读取公开
单集页面，并拒绝私有或非公开媒体。请仅处理你有权访问、分析和保留的内容。

## 许可证与第三方代码

本公开版采用 GPL-3.0 许可证。精简 XHS 适配器基于 XHS-Downloader 的公开页面
提取链路改造；来源、许可及未包含的上游组件记录在
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中。
