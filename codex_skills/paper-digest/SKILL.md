---
name: paper-digest
description: Convert a prepared AirHub Article JSON package into a Chinese, self-contained HTML research digest. Use when an Article is available in inbox/ or processing/ and its Producer-prepared attachments and media manifests should be rendered into a readable HTML file. Do not use this skill for URLs, raw PDFs, source crawling, downloads, or media preparation.
---

# AirHub Article HTML Converter (Codex)

Invoke this skill explicitly as `$paper-digest` when needed. Read an AirHub
Article JSON package prepared by the Python Producer, analyze the paper section
by section in Chinese, and write the result as a **single self-contained HTML
file** at the user-requested path (normally `attachments/html/`). Keep the
Producer-provided figures, tables, and videos in place.

> ⚠️ **三条强制规则——无例外：**
> 1. **纯中文解读：** 所有解读内容必须用中文撰写。直接引用原文（blockquote）和公式保留原文，其余所有 prose 一律用中文。
> 2. **逐节深度解读：** 论文每一个主要节都必须深度解读，每节至少 3–5 段实质内容。每节只写一两句话不可接受。
> 3. **HTML 交付 + 保留图表视频：** 最终产物是一个独立 HTML 文件。论文中的图、表、视频必须来自 Article JSON 的 `metadata.media_manifest`、`metadata.pdf_figures` 或 `attachments`，并出现在 HTML 中，不得丢弃，也不得只用文字描述代替。不要在 skill 中抓取、下载或生成这些素材。

## When to Use

- 用户要求处理 AirHub `inbox/` 或 `processing/` 中的 Article JSON
- 用户明确提供了由 AirHub Producer 生成的 Article 工作目录
- Article 的 `type` 是 `paper`，并且完整 ArXiv HTML 原文或 PDF，以及图片、表格、视频素材已经由 Python 服务端准备好

## Do Not Use

- 用户只给出 arxiv URL、PDF URL、paper ID 或博客 URL，但没有 Article JSON 包
- 需要抓取网页、下载 PDF、解析 arxiv HTML、裁剪 PDF 图表、生成素材 manifest
- 需要处理 Producer 尚未准备好的来源输入

遇到这些情况时，先要求运行 Python Producer 或指定已经生成的 Article JSON；不要在本 skill 内补做抓取下载。

## Workflow

### Step 1: 读取 Article 包

从 AirHub Producer 生成的 Article JSON 开始。重点读取：

- `title`、`authors`、`publish_date`、`url`、`tags`
- `metadata.abstract`、`metadata.categories`
- `attachments` 中 Producer 保存的 `html` 原文或 PDF；优先读取完整 HTML，只有 HTML 不足时 Article 才会带 PDF
- `metadata.media_manifest` 中由 Producer 从 arxiv HTML 或博客来源准备的图、表、视频
- `metadata.pdf_figures` 中由 Producer 从 PDF 裁出的本地图像和 caption

禁止在此步骤中访问外部 URL 来补充素材；`url` 仅作为引用链接展示。

### Step 2: 识别所有章节与所有图表视频

撰写前先列两份清单：
1. **章节清单**：论文所有主要节（Abstract、Introduction、Related Work、Method、Experiments、Conclusion 等）。这构成输出结构——每节都必须覆盖。
2. **媒体清单**：只使用 Article JSON 中已提供的每一张图（Figure）、每一张表（Table）、每一个视频，连同它们的编号、标题（caption）和资源地址/本地路径。这份清单确保后面没有遗漏任何图表视频。

### Step 3: 撰写逐节解读

对**每一节**，按下方"输出格式"的深度要求撰写中文解读。不得跳过或合并章节。把媒体清单里的图、表、视频**插入到它们对应的章节位置**——讲到哪一节，就在那一节嵌入它引用的图表。逐节解读之后，再写一个结尾的"评价与延伸阅读"章节（见规则六）。

### Step 4: 构建并交付 HTML

按下方"HTML 输出结构"生成文件。**图片内嵌是默认且不可省略的收尾步骤**：HTML 中必须优先引用 Producer 在 Article JSON 中准备的本地 `src`/`path`，不得改用 `original_src` 热链；交付前从 AirHub 根目录调用 `python3 -m airhub.html.assets <digest.html> --base-dir .`，把所有本地图像转成 base64 `data:` URI。完成后扫描全部 `<img>`，确认没有相对路径、绝对文件路径或 `http(s)` 图片地址。确认文件已写入后，聊天里只回一两句话，**不要把整篇解读粘到聊天里**。

## 输出格式

以下规则对每次响应都是强制性的。

---

### 规则一 — 纯中文解读

所有解读内容一律用中文撰写，包括：章节摘要、关键点说明、动机分析、前后关联、图表说明文字。

**不翻译的内容：** 原文直接引用（blockquote 保留英文原文）、公式（保留 LaTeX）、代码、表格内的数据/数字/标识符、引用标注、paper ID、URL。

**完成前自查：** 从头到尾扫描，确认所有解读段落均为中文。

---

### 规则二 — 逐节深度解读（每节至少 3–5 段）

对论文每一节，解读必须包含以下**全部**内容：

**(a) 内容概述** — 本节说了什么？覆盖每一个关键论点、发现或设计选择。不得把多页压成一句。

**(b) 关键引文与公式** — 找出本节最重要的原句和公式，用 blockquote 逐字引用原文，公式用 LaTeX。每处引用/公式后用中文解释其含义和重要性。

**(c) 动机与推理** — 作者为何这么做？本节解决了什么问题？方法背后有哪些假设？

**(d) 前后关联** — 本节如何承接前文、为后文铺垫？

(a)–(d) 每项至少一段完整中文段落。讲到本节涉及的图/表/视频时，把它嵌入此处。**方法（Method/Approach）章节有更高的标准，见规则五。**

**深度自查：** 写完每节前自问："若读者只看我对这节的解读，能否完全理解这节说了什么、为何重要、如何运作？"若不能，继续扩展。

---

### 规则三 — HTML 输出结构

最终产物是一个独立、可直接在浏览器打开的 HTML 文件。要求：

- `<!DOCTYPE html>`，`<html lang="zh-CN">`，含 `<meta charset="UTF-8">` 和 `<meta name="viewport" content="width=device-width, initial-scale=1">`。
- `<title>` 用论文标题（可中英并列）。
- 顶部一个论文信息块：标题、作者（保留原文）、来源链接（arxiv abs 页或原 PDF 链接，若有）。
- **公式渲染**：在 `<head>` 引入 MathJax，使行内/独立 LaTeX 能正确显示：
  ```html
  <script>window.MathJax={tex:{inlineMath:[['$','$'],['\\(','\\)']],displayMath:[['$$','$$'],['\\[','\\]']]}};</script>
  <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" async></script>
  ```
- **样式**：在 `<head>` 写内嵌 `<style>`，走干净的学术阅读风格——衬线或无衬线正文皆可、行宽适中（约 720–820px 居中）、清晰的标题层级、blockquote 有左边框、`figure` 居中且 `img` 最大宽度 100%、`figcaption` 字号略小且偏灰、表格有细边框与表头底色。正文用中文友好字体栈，例如：
  ```
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
               "Microsoft YaHei", "Source Han Serif SC", "Noto Serif CJK SC", Georgia, serif;
  ```
- **正文**：按章节顺序组织，每节一个标题 + 该节的中文深度解读；原文引用用 `<blockquote>` 保留英文；图、表、视频嵌入其对应章节（见规则四）。
- 不把解读粘进聊天；聊天只发简短确认。

---

### 规则四 — 图、表、视频在 HTML 中的呈现

- **图**：`<figure><img src="..." alt="..." loading="lazy"><figcaption>图 N：中文说明（可附原 caption）</figcaption></figure>`。ArXiv HTML 图和 PDF 图都使用 Article JSON 中 Producer 已本地化的 `src`/`path`；不得使用 `original_src` 或自行拼接网络热链。最后必须运行资产内嵌工具，把每个 `<img>` 都转换为 base64 `data:` URI（否则下载后的 HTML 可能裂图）。
- **表**：用 `<table>`。arxiv HTML 的表原样保留其结构与数据；PDF 的表据抽取文本重建。表内数据/数字/标识符不翻译，caption 用中文。
- **视频**：保留原始 `<iframe>` embed；无法 embed 时给出可点击的视频链接。
- 每个图/表/视频都应出现在它被讨论的那一节附近，让读者图文对照。

---

### 规则五 — 方法章节必须"自足"（读者无需翻原文即可理解）

其他章节做到"读懂大意"即可，但论文的**方法/模型/算法章节是核心贡献所在**，标准更高：读者只看你的解读就应当能完整地理解所提方法是什么、由哪些部分组成、如何运作、为何这样设计。换句话说，这一节要写成"无需再去翻原文也能复述出这套方法"。要达到这个目标，方法章节的解读必须覆盖以下要点（按需展开，通常需要明显多于 3–5 段）：

**(1) 整体框架与数据流** — 先用一两段讲清方法的总体结构：有哪些模块/组件，它们如何连接，一条输入从进入系统到产生输出要经过哪些步骤。这是读者的"地图"，务必先建立起来。配合架构图（若有）讲解。

**(2) 逐组件拆解** — 对每个关键模块/步骤分别说明：它接收什么输入、做了什么处理、输出什么、在整体中扮演什么角色。不要笼统带过任何一个被论文当作贡献的组件。

**(3) 公式逐符号解释** — 复现方法的核心公式（LaTeX），并**逐一定义其中每一个符号、下标、函数**，用中文讲清它代表什么、量纲/取值含义、为什么出现在这里。读者不应为了看懂某个变量而被迫去翻原文。损失函数要说明每一项分别约束什么、为何相加/加权。

**(4) 训练 / 推理 / 算法流程** — 如果方法涉及训练或运行流程，按步骤讲清楚（必要时用有序列表写成准伪代码式的步骤：初始化 → 每步做什么 → 终止条件 → 如何得到最终输出）。点明论文给出的关键超参数、目标函数、优化方式、采样或推理策略。

**(5) 设计动机与取舍** — 每个非平凡的设计选择，解释作者"为什么这么做"：它解决了前人方法的什么问题？背后有什么假设？换一种做法会有什么后果？这是让读者"真正理解"而非"机械记住"的关键。

**(6) 一个具体例子（强烈建议）** — 若能用一个小例子或具体场景把方法走一遍（给定这样的输入，方法如何一步步得到输出），读者的理解会牢固得多。论文有 running example 时优先复用。

**方法章节自查：** 写完后自问："一个没读过原文的同行，单看我这一节，能不能照着把这套方法讲给别人听、甚至大致复现？"若答案存疑，定位是哪一环（哪个模块没讲透、哪个符号没定义、哪步流程跳了）并补足。

---

### 正确输出片段示例（HTML body 内部）

```html
<section>
  <h2>第三节：Robometer</h2>
  <p>本节介绍 Robometer 的核心架构与训练框架，是全文主要贡献。作者提出一种双目标训练方案，
     同时优化帧级进度损失与轨迹比较偏好损失，使模型能从专家轨迹与次优轨迹中共同学习。</p>

  <blockquote>"Robometer is trained with a dual objective: a frame-level progress loss that
  anchors reward magnitude on expert data, and a trajectory-comparison preference loss that
  imposes global ordering constraints across trajectories of the same task."</blockquote>

  <p>帧级进度损失提供逐帧的密集监督，使奖励值与专家行为良好校准；偏好损失引入全局排序约束作为补充——
     即便某条轨迹的绝对进度难以标注（如失败轨迹），模型仍可通过比较学习哪条整体更优。二者相互强化，
     共同提升泛化能力。</p>

  <figure>
    <img src="attachments/image/2026-08-11/arxiv-example/media-001.png" alt="Robometer 架构图" loading="lazy">
    <figcaption>图 3：Robometer 的双目标训练框架（原文 Figure 3）。</figcaption>
  </figure>

  <p>总训练损失如下：</p>
  <p>$$\mathcal{L} = \mathcal{L}_{\text{pref}} + \mathcal{L}_{\text{prog}} + \mathcal{L}_{\text{succ}}$$</p>
  <p>它由偏好预测损失、帧级进度损失、任务成功预测损失三部分构成，分别负责全局排序、局部进度校准与任务完成判断，
     协同塑造出结构化、可泛化的奖励表示。</p>
</section>
```

---

### 规则六 — 结尾必须加"评价与延伸阅读"一节

逐节解读之后，在 HTML 正文末尾追加一个独立章节（如 `<h2>评价与延伸阅读</h2>`）。前面各节是对论文的<strong>忠实转述</strong>，而这一节是你作为读者的<strong>独立评判</strong>——目的是帮读者形成自己的判断，而不是替作者背书。两点心态：评判要具体、扣住这篇论文本身，不要写放之四海皆准的套话（"多做点实验会更好"对任何论文都成立，等于没说）；同时保持公允、有理有据，必要时说明哪些是你的推测。

这一节包含三块：

**(1) 做得好的地方** — 这篇论文真正的贡献与亮点：想法是否新颖、问题是否重要、论证/实验是否扎实、写作是否清晰、结果是否有说服力。点出最让你信服的一两处，并说明为什么。

**(2) 不足或存疑之处** — 实事求是地指出局限：有没有未经检验的假设？实验是否充分（基线是否齐全、数据集/规模是否有代表性、是否有消融）？方法的可扩展性、泛化性、计算成本如何？结论是否有过度宣称（overclaim）？可复现性如何（代码/数据/超参是否公开）？只列与本文真正相关的问题。

**(3) 可改进方向** — 针对上面的不足，给出具体、可操作的改进建议或后续值得探索的方向。

紧接着再加一个**延伸阅读**小块：列 3–6 篇与本文密切相关的论文，每篇一句话说明"为什么相关 / 和本文什么关系"（前驱工作、被对比的基线、并行的替代方案、后续改进等）。

> ⚠️ **延伸阅读的论文必须真实存在，绝不可编造标题、作者或 ID。** 最稳妥的来源是论文自己的参考文献列表（arxiv HTML 里通常就有），从中挑出最相关的几篇；如需补充更新或更对口的工作，用 `web_search` 核实后再列，并尽量给出可点击链接（arxiv abs 页或 DOI）。拿不准某篇是否存在，就不要列它。

```html
<section>
  <h2>评价与延伸阅读</h2>
  <h3>做得好的地方</h3>
  <p>……（针对本文的具体优点）……</p>
  <h3>不足与存疑</h3>
  <p>……（针对本文的具体局限）……</p>
  <h3>可改进方向</h3>
  <p>……（具体、可操作的建议）……</p>
  <h3>延伸阅读</h3>
  <ul>
    <li><a href="https://arxiv.org/abs/XXXX.XXXXX">论文标题</a>——一句话说明与本文的关系。</li>
  </ul>
</section>
```

---

## 交付前自查

交付文件前确认：
- [ ] 每个主要章节都有 3–5 段中文深度解读，无跳节/并节
- [ ] **方法章节自足**：整体框架、逐组件、公式逐符号定义、流程步骤、设计动机都已讲清——没读过原文的同行单看此节即可复述该方法
- [ ] 论文里的每一张图、每一张表、每一个视频都已出现在 HTML 中并嵌入对应章节（媒体清单逐项核对）
- [ ] **图的来源正确**：所有图、表、视频都来自 Article JSON 的 `metadata.media_manifest`、`metadata.pdf_figures` 或 `attachments`，没有在 skill 中临时抓取或下载
- [ ] 已从 AirHub 根目录运行 `python3 -m airhub.html.assets <digest.html> --base-dir .`；每个 `<img src>` 均为 `data:` URI，不存在本地路径或 `http(s)` 图片热链
- [ ] 公式用 LaTeX 且已引入 MathJax
- [ ] 原文引用用 `<blockquote>` 保留英文，未被翻译
- [ ] `<html lang="zh-CN">` 已设置，标签闭合，文件可作为合法 HTML 打开
- [ ] 文件已存入 Article 对应输出位置或用户指定位置；聊天里没有粘贴全文
- [ ] 结尾有"评价与延伸阅读"一节：含做得好/不足/可改进三块，评判具体且扣住本文
- [ ] 延伸阅读列出的论文均真实存在（来自本文参考文献或经 `web_search` 核实），未编造标题/ID

## Notes

- Producer 是唯一负责来源识别、抓取、下载、缓存、HTML 原文保存、按需 PDF 下载、PDF 裁图和 media manifest 准备的组件。
- Article 带有完整且足够的本地 HTML 原文时，没有 PDF 是正常情况；直接逐节读取该 HTML，不得自行补下载 PDF。
- 本 skill 只负责阅读 Article 包、撰写中文深度解读、组织 HTML，并默认调用本地资产内嵌工具；图片的联网下载与本地化仍只能由 Producer 完成。
- 不要编造图片、表格、视频或参考文献；Article 包没有提供的素材，应在交付说明中如实说明缺失。

## Codex installation note

The repository keeps the source skill in `codex_skills/paper-digest/`. Run
`bash run/install_codex_skill.sh` to install the same folder into
`${CODEX_HOME:-$HOME/.codex}/skills/paper-digest/` when a writable Codex home is
available.
