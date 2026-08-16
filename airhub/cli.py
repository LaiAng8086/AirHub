"""AirHub 中文集成命令行界面。"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import termios
import tty
import unicodedata
from pathlib import Path
from typing import Callable

from .blog_archive import BlogSite, BlogSiteStore
from .codex_digest import list_digest_candidates
from .config import AppSettings, load_settings, save_settings
from .daily_state import load_daily_state, mark_step
from .paths import PROJECT_ROOT
from .podcast_worker import create_podcast_job
from .priority_feedback import PriorityFeedbackStore
from .queue_management import (
    add_overflow_batch,
    defer_pending_articles,
    import_manual_arxiv_file,
    list_manual_files,
)
from .workflow import (
    apply_daily_strategy,
    clear_runtime_cache,
    collect_article_statuses,
    discover_recent_candidates,
    embed_pending_html,
    format_article_statuses,
    format_priority_frequencies,
    refresh_completion_state,
    update_priority_strategy,
)
from .xiaoyuzhou import (
    XiaoyuzhouAuthClient,
    XiaoyuzhouLoginStatus,
    podcast_download_state,
)
from .xhs import download_xhs_texts, extract_xhs_links
from .xhs_activity import format_xhs_daily_activity, load_xhs_daily_activity


ACTION_LABELS = {
    "settings": "每日文章上限配置",
    "prepare": "优选策略更新与最近 300 篇基本信息抓取",
    "priority": "优选策略筛选与原文保存",
    "fixed": "固定策略筛选与原文保存",
    "paper-digest": "批量选择 Article 并执行 DeepSeek Codex paper-digest",
    "embed-pending": "待嵌入 HTML 补处理",
    "status": "全部文章状态",
    "clear-cache": "缓存清理",
    "manual-import": "手动 arXiv / Blog 列表导入",
    "overflow-add": "按当前策略超量加入",
    "defer-pending": "待解读文章批量退回缓存",
    "xiaoyuzhou-login": "小宇宙登录",
    "xiaoyuzhou-podcast": "小宇宙播客公开下载与 turbo 转录",
    "xhs-download": "小红书帖子文本批量下载",
    "xhs-classify": "小红书缓存 Blog / arXiv 识别",
    "blog-list": "Blog 主站列表查询",
    "blog-delete": "Blog 主站列表批量删除",
    "priority-feedback": "从已解读文章更新优选频度",
}


def _mark(done: bool) -> str:
    return "✓" if done else " "


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def _fit(value: str, width: int) -> str:
    rendered: list[str] = []
    occupied = 0
    for char in value:
        size = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if occupied + size > width:
            if width >= 2 and rendered:
                rendered[-1] = "…"
            break
        rendered.append(char)
        occupied += size
    text = "".join(rendered)
    return text + " " * max(0, width - _display_width(text))


def _panel(title: str, rows: list[str], width: int, height: int) -> list[str]:
    inner = width - 2
    title_text = f" {title} "
    top_remaining = max(0, inner - _display_width(title_text))
    result = ["┌" + title_text + "─" * top_remaining + "┐"]
    padded = rows + [""] * max(0, height - len(rows))
    result.extend(f"│{_fit(row, inner)}│" for row in padded[:height])
    result.append("└" + "─" * inner + "┘")
    return result


def _menu(root: Path, login_status: XiaoyuzhouLoginStatus | None = None) -> str:
    state = refresh_completion_state(root)
    xhs_activity = load_xhs_daily_activity(root)
    steps = state.get("steps", {})
    strategy = state.get("strategy")
    login_status = login_status or XiaoyuzhouLoginStatus(
        False, reason="启动时等待登录状态检查"
    )
    panels = [
        (
            "论文采集",
            [
                f"1. [{_mark(bool(steps.get('settings_updated')))}] 每日文章上限",
                f"2. [{_mark(bool(steps.get('priority_updated') and steps.get('discovered')))}] 优选策略 + 最近 300 篇",
                f"3. [{_mark(strategy == 'priority' and bool(steps.get('strategy_applied')))}] 优选筛选并保存原文",
                f"4. [{_mark(strategy == 'fixed' and bool(steps.get('strategy_applied')))}] 固定筛选并保存原文",
            ],
        ),
        (
            "解读与队列",
            [
                f"5. [{_mark(bool(steps.get('digested')))}] 批量 paper-digest",
                f"6. [{_mark(bool(steps.get('embedded')))}] 补做待嵌入 HTML",
                "9. [ ] 手动导入 arXiv / Blog 列表",
                "10.[ ] 按策略超量加入一批",
                "11.[ ] 待解读文章退回缓存",
                "18.[ ] 已解读论文回灌优选频度",
            ],
        ),
        (
            "播客与系统",
            [
                f"12.[{_mark(login_status.authenticated)}] 小宇宙短信登录",
                "13.[ ] 订阅更新 → 公开音频 → turbo HTML",
                "7. [ ] 全部文章状态",
                "8. [ ] 清除可重建缓存",
                "0.     退出 AirHub",
            ],
        ),
        (
            "XHS 与 Blog",
            [
                (
                    f"今日 XHS {xhs_activity.count} 次 | 上次 "
                    f"{xhs_activity.last_completed_at.split('T', 1)[-1][:8]}"
                    if xhs_activity.count
                    else "今日 XHS 0 次 | 下一次 #1"
                ),
                "14.[ ] 批量下载 XHS 帖子文本",
                "15.[ ] 识别全部 XHS 缓存 → manual",
                "16.[ ] 查询 Blog 主站列表",
                "17.[ ] 按编号批量删除 Blog 主站",
            ],
        ),
    ]
    columns = shutil.get_terminal_size(fallback=(160, 40)).columns
    column_count = 3 if columns >= 132 else 2 if columns >= 88 else 1
    gap = "  "
    panel_width = max(40, (columns - len(gap) * (column_count - 1)) // column_count)
    rendered_panels = [_panel(title, rows, panel_width, 7) for title, rows in panels]
    blocks: list[str] = []
    for offset in range(0, len(rendered_panels), column_count):
        group = rendered_panels[offset : offset + column_count]
        blocks.extend(gap.join(lines) for lines in zip(*group))
    strategy_text = {"priority": "优选策略", "fixed": "固定策略"}.get(strategy, "未选择")
    header = [
        "╔═ AIRHUB / DAILY INTELLIGENCE CONSOLE ═══════════════════════════════════╗",
        f"  日期 {state['date']}  │  今日策略 {strategy_text}  │  小宇宙 {login_status.menu_text}",
        "╚════════════════════════════════════════════════════════════════════════╝",
    ]
    return "\n".join(header + blocks)


def _clear_screen(output: Callable[[str], None]) -> None:
    output("\033[2J\033[H")


def _pause_for_key(
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    prompt = "按任意键返回控制台……"
    if input_fn is input and sys.stdin.isatty():
        output(prompt)
        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setcbreak(descriptor)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
        return
    input_fn(prompt)


def _set_daily_limit(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    current = load_settings(root)
    raw = input_fn(f"请输入新的每日文章上限（当前 {current.daily_article_limit}）：").strip()
    settings = AppSettings.from_dict({"daily_article_limit": raw})
    save_settings(settings, root)
    mark_step("settings_updated", root, counts={"daily_article_limit": settings.daily_article_limit})
    output(f"每日文章上限已设置为 {settings.daily_article_limit}。")
    output("[DONE] 每日文章上限更新")


def _select_digest_articles(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> list[str]:
    candidates = list_digest_candidates(root)
    if not candidates:
        output("当前没有待解读文章。")
        output("[DONE] 待解读列表读取")
        return []

    output(f"待解读文章（共 {len(candidates)} 篇）：")
    for index, candidate in enumerate(candidates, start=1):
        rank_text = (
            f" | 优选排名 {candidate.priority_rank}"
            if candidate.priority_rank is not None
            else ""
        )
        output(
            f"{index}. 添加日期 {candidate.added_date} | {candidate.article_id}"
            f"{rank_text} | {candidate.title}"
        )
    raw = input_fn(
        "请输入要解读的文章编号，多个编号用空格分隔（输入 0 取消）："
    ).strip()
    if not raw or raw == "0":
        output("已取消文章解读。")
        return []
    try:
        selected_indexes = list(dict.fromkeys(int(token) for token in raw.split()))
    except ValueError as exc:
        raise ValueError("文章编号必须是用空格分隔的整数") from exc
    invalid = [index for index in selected_indexes if not 1 <= index <= len(candidates)]
    if invalid:
        raise ValueError(f"文章编号必须在 1–{len(candidates)} 之间: {invalid}")
    selected_candidates = [candidates[index - 1] for index in selected_indexes]
    for selected in selected_candidates:
        output(
            f"已选择：{selected.article_id} | 添加日期 {selected.added_date} | "
            f"{selected.title}"
        )
    output(f"[DONE] 待解读 Article 批量选择（{len(selected_candidates)} 篇）")
    return [
        selected.task.article_path.relative_to(root).as_posix()
        for selected in selected_candidates
    ]


def _run_digest_batch(
    root: Path,
    article_paths: list[str],
    output: Callable[[str], None],
) -> dict[str, object]:
    """Run selected digests sequentially; a failed Article never blocks the rest."""

    environment = os.environ.copy()
    environment["CODEX_PAPER_DIGEST_RETRIES"] = "1"
    succeeded: list[str] = []
    failed: list[dict[str, object]] = []
    total = len(article_paths)
    for index, article_path in enumerate(article_paths, start=1):
        output(f"[INFO] 批量解读 {index}/{total}：{article_path}（失败不重试）")
        try:
            completed = subprocess.run(
                ["bash", "run/run_paper_digest_codex.sh", article_path],
                cwd=root,
                check=False,
                env=environment,
            )
            returncode = int(completed.returncode)
        except OSError as exc:
            returncode = -1
            output(f"[ERROR] {article_path} 调用失败：{exc}；已跳过。")
        if returncode:
            if returncode != -1:
                output(
                    f"[ERROR] {article_path} 调用失败（退出码 {returncode}）；已跳过。"
                )
            failed.append({"article": article_path, "returncode": returncode})
            continue
        succeeded.append(article_path)
        output(f"[DONE] {article_path} 解读完成")
    output(
        f"批量解读结果：选择 {total}，成功 {len(succeeded)}，"
        f"失败并跳过 {len(failed)}。"
    )
    output("[DONE] paper-digest 批量执行")
    return {"selected": total, "succeeded": succeeded, "failed": failed}


def _manual_import_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    files = list_manual_files(root)
    if not files:
        output("manual/ 中没有可导入的 txt 列表。")
        output("[DONE] 手动 arXiv / Blog 列表读取")
        return
    output(f"手动 arXiv / Blog 列表（共 {len(files)} 个）：")
    for index, path in enumerate(files, start=1):
        output(f"{index}. {path.relative_to(root).as_posix()}")
    raw = input_fn("请输入要导入的文件编号（输入 0 取消）：").strip()
    if raw == "0":
        output("已取消手动导入。")
        return
    try:
        selected_index = int(raw)
    except ValueError as exc:
        raise ValueError("文件编号必须是整数") from exc
    if not 1 <= selected_index <= len(files):
        raise ValueError(f"文件编号必须在 1–{len(files)} 之间")
    selected = files[selected_index - 1]
    output(f"开始导入：{selected.relative_to(root).as_posix()}")
    stats = import_manual_arxiv_file(root, selected)
    refresh_completion_state(root)
    output(
        "手动导入结果："
        f"请求 {stats['requested']}（arXiv {stats.get('requested_arxiv', 0)} / "
        f"Blog {stats.get('requested_blogs', 0)}），进入待解读 {stats['stored']}，"
        f"Blog 完整归档 {stats.get('blogs_archived', 0)}，"
        f"GitHub/Hugging Face 仅入列表 {stats.get('blogs_catalog_only', 0)}，"
        f"新增主站 {stats.get('blog_sites_added', 0)}，"
        f"已存在或列表重复 {stats['already_active']}，"
        f"从缓存恢复 {stats['restored_from_cache']}，错误 {stats['errors']}。"
    )
    output(f"报告：{stats['report']}")
    output("[DONE] 手动 arXiv / Blog 列表导入")


def _overflow_add_action(root: Path, output: Callable[[str], None]) -> None:
    stats = add_overflow_batch(root)
    refresh_completion_state(root)
    output(
        f"超量加入结果：当前{ {'priority': '优选', 'fixed': '固定'}.get(stats['strategy'], stats['strategy']) }策略，"
        f"批量上限 {stats['daily_limit']}，进入待解读 {stats['stored']}，"
        f"剩余缓存候选 {stats['remaining']}，错误 {stats['errors']}。"
    )
    output(f"报告：{stats['report']}")
    output("[DONE] 当前策略超量加入")


def _defer_pending_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    candidates = list_digest_candidates(root)
    if not candidates:
        output("当前没有待解读文章。")
        output("[DONE] 待解读列表读取")
        return
    output(f"待解读文章（共 {len(candidates)} 篇）：")
    for index, candidate in enumerate(candidates, start=1):
        rank_text = (
            f" | 优选排名 {candidate.priority_rank}"
            if candidate.priority_rank is not None
            else ""
        )
        output(
            f"{index}. 添加日期 {candidate.added_date} | {candidate.article_id}"
            f"{rank_text} | {candidate.title}"
        )
    raw = input_fn("请输入要退回缓存的文章编号，多个编号用空格分隔（输入 0 取消）：").strip()
    if not raw or raw == "0":
        output("已取消批量退回。")
        return
    tokens = raw.split()
    try:
        indexes = list(dict.fromkeys(int(token) for token in tokens))
    except ValueError as exc:
        raise ValueError("文章编号必须是用空格分隔的整数") from exc
    invalid = [index for index in indexes if not 1 <= index <= len(candidates)]
    if invalid:
        raise ValueError(f"文章编号必须在 1–{len(candidates)} 之间: {invalid}")
    selected_ids = [candidates[index - 1].article_id for index in indexes]
    stats = defer_pending_articles(root, selected_ids)
    refresh_completion_state(root)
    output(f"已将 {stats['deferred']} 篇文章移出待解读并退回缓存：")
    for article_id in stats["article_ids"]:
        output(f"- {article_id}")
    output("[DONE] 待解读文章批量退回缓存")


def _priority_feedback_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    store = PriorityFeedbackStore(root)
    candidates = store.list_completed_papers()
    if not candidates:
        output("当前没有可用于更新优选频度的已解读论文。")
        output("[DONE] 已解读论文列表读取")
        return
    output(f"已解读论文（共 {len(candidates)} 篇；每篇只回灌一次）：")
    for index, candidate in enumerate(candidates, start=1):
        state = "已加入" if candidate.already_added else "可选择"
        institutions = ", ".join(candidate.institutions) or "机构未识别"
        output(
            f"{index}. [{state}] {candidate.article_id} | {candidate.title} | "
            f"作者 {len(candidate.authors)} | {institutions}"
        )
    raw = input_fn(
        "请输入要增加作者/机构权重的论文编号，多个编号用空格分隔（输入 0 取消）："
    ).strip()
    if not raw or raw == "0":
        output("已取消优选频度更新。")
        return
    try:
        indexes = list(dict.fromkeys(int(token) for token in raw.split()))
    except ValueError as exc:
        raise ValueError("论文编号必须是用空格分隔的整数") from exc
    invalid = [index for index in indexes if not 1 <= index <= len(candidates)]
    if invalid:
        raise ValueError(f"论文编号必须在 1–{len(candidates)} 之间: {invalid}")
    selected_ids = [candidates[index - 1].article_id for index in indexes]
    stats = store.add_articles(selected_ids)
    if stats["added"]:
        profile = update_priority_strategy(root)
        output(
            f"频度表已重建：作者 {len(profile.author_counts)}，"
            f"机构 {len(profile.institution_counts)}。"
        )
    output(
        f"回灌结果：选择 {stats['requested']}，新增论文 {stats['added']}，"
        f"已加入跳过 {stats['skipped']}；作者权重 +{stats['author_increments']}，"
        f"机构权重 +{stats['institution_increments']}。"
    )
    for article_id in stats["article_ids"]:
        output(f"- 已加入：{article_id}")
    output("[DONE] 已解读论文作者与机构回灌优选频度")


def _format_strategy_result(stats: dict[str, object], mode: str) -> str:
    if bool(stats.get("skipped")):
        return f"策略未重复执行：{stats.get('reason', '今天已执行该策略')}"
    candidates = int(stats.get("candidates", 0))
    eligible = int(stats.get("eligible", 0))
    selected = int(stats.get("selected", 0))
    stored = int(stats.get("stored", 0))
    existing = int(stats.get("existing", max(0, candidates - eligible)))
    not_selected = int(stats.get("not_selected", max(0, eligible - selected)))
    errors = int(stats.get("errors", 0))
    if mode == "priority":
        return (
            "优选策略结果："
            f"候选 {candidates}，已存在 {existing}，参与优选排名 {eligible}，"
            f"进入每日上限 {selected}，实际保存 {stored}，"
            f"排名后未进入上限 {not_selected}，错误 {errors}。"
            "优选策略是排序机制；参与排名不表示通过了额外的硬筛选。"
        )
    return (
        "固定策略结果："
        f"候选 {candidates}，已存在 {existing}，通过固定筛选 {eligible}，"
        f"进入每日上限 {selected}，实际保存 {stored}，"
        f"通过筛选但未进入上限 {not_selected}，错误 {errors}。"
    )


def _prepare_daily_inputs(root: Path, output: Callable[[str], None]) -> None:
    profile = update_priority_strategy(root)
    discovery = discover_recent_candidates(root)
    output(format_priority_frequencies(profile))
    cache_text = "复用今日快照，未重复联网抓取" if discovery.cached else "新抓取"
    output(
        f"\n候选基本信息：{len(discovery.articles)} 篇（{cache_text}），"
        f"错误 {discovery.errors}，快照 {discovery.path.relative_to(root)}"
    )
    output("[DONE] 优选策略更新与候选抓取")


def _clear_cache_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    answer = input_fn(
        "将清除 cache/ 与 data/candidates/，不会删除论文、附件和解读。输入 CLEAR 确认："
    ).strip()
    if answer != "CLEAR":
        output("已取消缓存清理。")
        return
    stats = clear_runtime_cache(root)
    output(f"已清除 {stats['files']} 个缓存文件，共 {stats['bytes']} 字节。")
    output("[DONE] 可重建缓存清理")


def _xiaoyuzhou_login_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    client = XiaoyuzhouAuthClient(root)
    area_code = input_fn("国际区号（直接回车使用 +86）：").strip() or "+86"
    mobile_phone = input_fn("手机号：").strip()
    client.send_sms_code(mobile_phone, area_code)
    masked = ("*" * max(0, len(mobile_phone) - 4)) + mobile_phone[-4:]
    output(f"验证码已发送至 {area_code} {masked}。")
    output("[DONE] 小宇宙短信验证码发送")
    verify_code = input_fn("短信验证码：").strip()
    status = client.login_with_sms(mobile_phone, verify_code, area_code)
    identity = status.nickname or status.uid or "账号"
    output(f"小宇宙登录成功：{identity}。凭据只用于订阅列表，不参与音频下载。")
    output("[DONE] 小宇宙登录")


def _xiaoyuzhou_podcast_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    client = XiaoyuzhouAuthClient(root)
    status = client.check_login_status(refresh=True)
    if not status.authenticated:
        raise RuntimeError(status.reason or "小宇宙未登录，请先执行 12")
    episodes = client.list_subscription_updates(max_items=100)
    if not episodes:
        output("订阅更新列表为空。")
        output("[DONE] 小宇宙订阅更新列表读取")
        return

    output(f"小宇宙订阅更新（最多 100 条，共 {len(episodes)} 条）：")
    output("编号 | 日期       | 博主                 | 播客                 | 下载状态     | 单集标题")
    output("-" * 120)
    for index, episode in enumerate(episodes, start=1):
        state = podcast_download_state(root, episode)
        output(
            f"{index:>4} | {episode.date:<10} | {episode.author[:18]:<20} | "
            f"{episode.podcast_title[:18]:<20} | {state:<10} | {episode.title}"
        )
    output("[DONE] 小宇宙订阅更新列表读取")
    raw = input_fn(
        "请输入要下载并转录的数字编号，多个编号用空格分隔（回车结束，输入 0 取消）："
    ).strip()
    if not raw or raw == "0":
        output("已取消播客批处理。")
        return
    try:
        indexes = list(dict.fromkeys(int(token) for token in raw.split()))
    except ValueError as exc:
        raise ValueError("播客编号必须是用空格分隔的整数") from exc
    invalid = [index for index in indexes if not 1 <= index <= len(episodes)]
    if invalid:
        raise ValueError(f"播客编号必须在 1–{len(episodes)} 之间：{invalid}")
    selected = [episodes[index - 1] for index in indexes]
    for episode in selected:
        output(
            f"已选择：{episode.date} | {episode.author} | "
            f"{episode.podcast_title} | {episode.title}"
        )
    job_path = create_podcast_job(root, selected)
    output(f"[DONE] 播客批处理任务创建：{job_path.relative_to(root).as_posix()}")
    completed = subprocess.run(
        ["bash", "run/run_xiaoyuzhou_podcast.sh"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"播客批处理存在失败项（退出码 {completed.returncode}），请查看 Work_dirs 报告和 Logs 日志"
        )
    output("[DONE] 小宇宙播客公开下载与 turbo 转录")


def _read_xhs_textarea(
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> str:
    output("┌─ 小红书链接批量输入 ─────────────────────────────────────────────────┐")
    output("│ 可直接用 Shift+Insert 粘贴 Chrome 插件批量提取的多行/长字符串。      │")
    output("│ 链接之外的文字会被忽略；粘贴完成后再输入一个空行提交。              │")
    output("├──────────────────────────────────────────────────────────────────────┤")
    lines: list[str] = []
    while True:
        try:
            value = input_fn("│ ")
        except EOFError:
            break
        if value == "":
            break
        lines.append(value)
    output("└──────────────────────────────────────────────────────────────────────┘")
    return "\n".join(lines)


def _xhs_download_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    output(format_xhs_daily_activity(load_xhs_daily_activity(root)))
    pasted = _read_xhs_textarea(input_fn, output)
    links = extract_xhs_links(pasted)
    if not links:
        output("没有识别到有效的小红书帖子链接，已取消。")
        output("[DONE] XHS 有效链接检查（0 个）")
        return
    output(f"识别到 {len(links)} 个去重后的有效小红书帖子链接。")
    output("[DONE] XHS 有效链接计数确认")
    answer = input_fn(f"确认下载这 {len(links)} 个帖子的纯文本内容？[Y/N]：").strip().lower()
    if answer not in {"y", "yes", "是"}:
        output("已取消 XHS 文本下载。")
        return
    result = download_xhs_texts(
        root,
        links,
        source_text=pasted,
        output=output,
    )
    output(
        f"XHS 文本下载结果：请求 {result.requested}，保存 {result.saved}，"
        f"失败 {result.failed}。"
    )
    output(f"缓存目录：{result.session_dir.relative_to(root).as_posix()}")
    output(f"日志：{result.log_path.relative_to(root).as_posix()}")
    output(
        f"本次为今日 XHS 第 #{result.daily_sequence} 次执行；"
        f"完成时间 {result.completed_at.split('T', 1)[-1][:8]}。"
    )
    output("[DONE] 小红书帖子纯文本批量下载（未下载图片/视频）")


def _xhs_classify_action(root: Path, output: Callable[[str], None]) -> None:
    output(format_xhs_daily_activity(load_xhs_daily_activity(root)))
    completed = subprocess.run(
        ["bash", "run/run_xhs_classify_codex.sh"], cwd=root, check=False
    )
    if completed.returncode:
        raise RuntimeError(
            "XHS 缓存识别存在失败；对应缓存仍已清理，请查看 Logs/ 与最新 Work_dirs/INFER_*_xhs_classifier/summary.txt"
        )
    output("[DONE] 全部 XHS 缓存 Blog / arXiv 识别")


def _render_blog_sites(root: Path, output: Callable[[str], None]) -> list[BlogSite]:
    sites = BlogSiteStore(root).list()
    if not sites:
        output("Blog 主站列表为空。")
        return []
    output(f"当前涉猎的 Blog 主站（共 {len(sites)} 个）：")
    for index, site in enumerate(sites, start=1):
        output(
            f"{index}. {site.origin} | 首次 {site.first_seen_at[:10]} | "
            f"最近 {site.last_seen_at[:10]}"
        )
    return list(sites)


def _blog_list_action(root: Path, output: Callable[[str], None]) -> None:
    _render_blog_sites(root, output)
    output("[DONE] Blog 主站列表查询")


def _blog_delete_action(
    root: Path,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> None:
    sites = _render_blog_sites(root, output)
    if not sites:
        output("[DONE] Blog 主站列表批量删除（0 个）")
        return
    raw = input_fn(
        "请输入要删除的 Blog 数字编号，多个编号用空格分隔（回车结束，输入 0 取消）："
    ).strip()
    if not raw or raw == "0":
        output("已取消 Blog 主站删除。")
        return
    try:
        indexes = list(dict.fromkeys(int(token) for token in raw.split()))
    except ValueError as exc:
        raise ValueError("Blog 编号必须是用空格分隔的整数") from exc
    removed = BlogSiteStore(root).remove_indexes(indexes)
    output(f"已从列表删除 {len(removed)} 个 Blog 主站（历史网页归档保留）：")
    for site in removed:
        output(f"- {site.origin}")
    output("[DONE] Blog 主站列表批量删除")


def execute_action(
    action: str,
    root: Path,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> bool:
    if action == "settings":
        _set_daily_limit(root, input_fn, output)
    elif action == "prepare":
        _prepare_daily_inputs(root, output)
    elif action == "priority":
        stats = apply_daily_strategy("priority", root)
        output(_format_strategy_result(stats, "priority"))
        output("[DONE] 优选策略筛选与原文保存")
    elif action == "fixed":
        stats = apply_daily_strategy("fixed", root)
        output(_format_strategy_result(stats, "fixed"))
        output("[DONE] 固定策略筛选与原文保存")
    elif action == "paper-digest":
        article_paths = _select_digest_articles(root, input_fn, output)
        if not article_paths:
            return True
        _run_digest_batch(root, article_paths, output)
        refresh_completion_state(root)
    elif action == "embed-pending":
        stats = embed_pending_html(root)
        output(f"待嵌入处理结果：{stats}")
        output("[DONE] 待嵌入 HTML 补处理")
    elif action == "status":
        output(format_article_statuses(collect_article_statuses(root)))
        output("[DONE] 全部文章状态读取")
    elif action == "clear-cache":
        _clear_cache_action(root, input_fn, output)
    elif action == "manual-import":
        _manual_import_action(root, input_fn, output)
    elif action == "overflow-add":
        _overflow_add_action(root, output)
    elif action == "defer-pending":
        _defer_pending_action(root, input_fn, output)
    elif action == "xiaoyuzhou-login":
        _xiaoyuzhou_login_action(root, input_fn, output)
    elif action == "xiaoyuzhou-podcast":
        _xiaoyuzhou_podcast_action(root, input_fn, output)
    elif action == "xhs-download":
        _xhs_download_action(root, input_fn, output)
    elif action == "xhs-classify":
        _xhs_classify_action(root, output)
    elif action == "blog-list":
        _blog_list_action(root, output)
    elif action == "blog-delete":
        _blog_delete_action(root, input_fn, output)
    elif action == "priority-feedback":
        _priority_feedback_action(root, input_fn, output)
    elif action == "exit":
        output("已退出 AirHub。")
        return False
    else:
        raise ValueError(f"未知操作: {action}")
    return True


def run_menu(
    root: Path = PROJECT_ROOT,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> None:
    choices = {
        "1": "settings",
        "2": "prepare",
        "3": "priority",
        "4": "fixed",
        "5": "paper-digest",
        "6": "embed-pending",
        "7": "status",
        "8": "clear-cache",
        "9": "manual-import",
        "10": "overflow-add",
        "11": "defer-pending",
        "12": "xiaoyuzhou-login",
        "13": "xiaoyuzhou-podcast",
        "14": "xhs-download",
        "15": "xhs-classify",
        "16": "blog-list",
        "17": "blog-delete",
        "18": "priority-feedback",
        "0": "exit",
    }
    informational = {
        "prepare",
        "paper-digest",
        "status",
        "manual-import",
        "overflow-add",
        "defer-pending",
        "xiaoyuzhou-podcast",
        "xhs-download",
        "xhs-classify",
        "blog-list",
        "blog-delete",
        "priority-feedback",
    }
    last_message = ""
    try:
        login_status = XiaoyuzhouAuthClient(root).check_login_status(refresh=True)
    except Exception as exc:
        login_status = XiaoyuzhouLoginStatus(
            False, reason=f"登录状态检查失败：{str(exc)[:80]}"
        )
    output("[DONE] AirHub 启动时小宇宙登录状态检查")
    while True:
        _clear_screen(output)
        output(_menu(root, login_status))
        if last_message:
            output(last_message)
            last_message = ""
        try:
            choice = input_fn("请输入数字选项：").strip()
        except EOFError:
            output("未收到输入，已退出 AirHub。")
            return
        action = choices.get(choice)
        if action is None:
            last_message = "[ERROR] 无效选项，请输入 0–18。"
            continue
        try:
            if not execute_action(action, root, input_fn, output):
                return
            if action in informational:
                _pause_for_key(input_fn, output)
            if action in {"xiaoyuzhou-login", "xiaoyuzhou-podcast"}:
                login_status = XiaoyuzhouAuthClient(root).check_login_status(refresh=True)
            last_message = f"[DONE] {ACTION_LABELS[action]}"
        except Exception as exc:
            output(f"[ERROR] {exc}")
            _pause_for_key(input_fn, output)


def main() -> None:
    parser = argparse.ArgumentParser(description="AirHub 中文每日处理控制台")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="AirHub 项目根目录")
    parser.add_argument(
        "--action",
        choices=(
            "menu",
            "settings",
            "prepare",
            "priority",
            "fixed",
            "paper-digest",
            "embed-pending",
            "status",
            "clear-cache",
            "manual-import",
            "overflow-add",
            "defer-pending",
            "xiaoyuzhou-login",
            "xiaoyuzhou-podcast",
            "xhs-download",
            "xhs-classify",
            "blog-list",
            "blog-delete",
            "priority-feedback",
        ),
        default="menu",
        help="直接执行一个操作；默认进入数字菜单",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.action == "menu":
        run_menu(root)
    else:
        execute_action(args.action, root)


if __name__ == "__main__":
    main()
