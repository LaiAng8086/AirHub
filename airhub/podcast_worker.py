"""可恢复的小宇宙公开音频下载与 Whisper turbo 转录任务。"""

from __future__ import annotations

import argparse
import html as html_module
import json
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import Article, Attachment, utc_now_iso
from .paths import PROJECT_ROOT, ensure_runtime_dirs, relative_to_root
from .podcast_transcript import (
    WHISPER_MODEL,
    WHISPER_UPSTREAM_MODEL,
    TranscriptResult,
    render_dialogue_html,
    transcribe_with_turbo,
    write_transcript_json,
)
from .storage import ArticleStorage
from .xiaoyuzhou import PodcastEpisode, PublicEpisodeDownloader


CURRENT_JOB = Path("data/podcast/jobs/current.json")


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path


def create_podcast_job(root: Path, episodes: list[PodcastEpisode]) -> Path:
    """建立无需参数即可由 run 脚本消费的当前批处理任务。"""

    if not episodes:
        raise ValueError("至少需要选择一个播客单集")
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    work_dir = root / "Work_dirs" / f"INFER_{timestamp}_xiaoyuzhou"
    work_dir.mkdir(parents=True, exist_ok=False)
    payload: dict[str, Any] = {
        "version": 1,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "status": "pending",
        "whisper_model": WHISPER_MODEL,
        "whisper_upstream_model": WHISPER_UPSTREAM_MODEL,
        "execution": "local-nvidia-gpu",
        "slurm_allowed": False,
        "public_download_only": True,
        "work_dir": relative_to_root(work_dir, root),
        "episodes": [
            {
                "episode": episode.to_job_dict(),
                "status": "pending",
                "stage": "queued",
                "error": "",
            }
            for episode in episodes
        ],
    }
    job_path = root / CURRENT_JOB
    _write_json(job_path, payload)
    _write_text(
        work_dir / "summary.txt",
        "AirHub 小宇宙批处理任务\n"
        f"创建时间: {payload['created_at']}\n"
        f"节目数: {len(episodes)}\n"
        f"模型: {WHISPER_UPSTREAM_MODEL}\n"
        "执行位置: 本机 NVIDIA GPU（禁止 Slurm）\n"
        "下载方式: 公开网页、无登录状态\n"
        "状态: pending\n",
    )
    return job_path


def _load_job(root: Path, job_path: Path | None) -> tuple[Path, dict[str, Any]]:
    resolved = (job_path or (root / CURRENT_JOB)).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("播客任务文件必须位于 AirHub 项目目录内")
    if not resolved.is_file():
        raise FileNotFoundError(f"没有待执行的播客任务：{resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("episodes"), list):
        raise ValueError("播客任务文件格式无效")
    if payload.get("whisper_model") != WHISPER_MODEL:
        raise ValueError("任务模型不是固定的 Whisper turbo，拒绝执行")
    if payload.get("public_download_only") is not True:
        raise ValueError("任务未声明公开无登录下载，拒绝执行")
    if payload.get("slurm_allowed") is not False:
        raise ValueError("任务没有明确禁止 Slurm，拒绝执行")
    return resolved, payload


def _merged_episode(listed: PodcastEpisode, public: PodcastEpisode) -> PodcastEpisode:
    """展示字段以订阅列表为准，公开页只补全转录所需元数据。"""

    return replace(
        listed,
        pid=listed.pid or public.pid,
        title=listed.title if listed.title != "未命名单集" else public.title,
        podcast_title=(
            listed.podcast_title
            if listed.podcast_title != "未知播客"
            else public.podcast_title
        ),
        author=listed.author if listed.author != "未知主播" else public.author,
        pub_date=listed.pub_date or public.pub_date,
        duration=listed.duration or public.duration,
        shownotes=listed.shownotes or public.shownotes,
        description=listed.description or public.description,
        podcasters=listed.podcasters or public.podcasters,
        raw={},
    )


def _plain_summary(episode: PodcastEpisode) -> str:
    raw = episode.description or episode.shownotes
    return " ".join(html_module.unescape(raw).replace("<br>", "\n").split())[:600]


def _upsert_attachment(article: Article, attachment: Attachment) -> None:
    article.attachments = [
        existing
        for existing in article.attachments
        if not (existing.type == attachment.type or existing.path == attachment.path)
    ]
    article.attachments.append(attachment)


def _article_for_episode(episode: PodcastEpisode) -> Article:
    return Article(
        id=episode.article_id,
        type="podcast_episode",
        source="xiaoyuzhou",
        title=episode.title,
        authors=[episode.author] if episode.author else [],
        publish_date=episode.pub_date,
        url=f"https://www.xiaoyuzhoufm.com/episode/{episode.eid}",
        tags=["播客", "小宇宙", episode.podcast_title],
        summary=_plain_summary(episode),
        metadata={
            "download_date": datetime.now().astimezone().date().isoformat(),
            "podcast": {
                "eid": episode.eid,
                "pid": episode.pid,
                "title": episode.podcast_title,
                "author": episode.author,
                "duration": episode.duration,
                "podcasters": list(episode.podcasters),
            },
            "download": {
                "mode": "public-web",
                "authenticated": False,
                "credentials_available_to_downloader": False,
            },
            "transcription": {
                "model": WHISPER_UPSTREAM_MODEL,
                "requested_model": WHISPER_MODEL,
                "execution": "local-nvidia-gpu",
                "slurm": False,
            },
            "podcast_stage": "queued",
        },
    )


def _completed_article(storage: ArticleStorage, episode: PodcastEpisode) -> Article | None:
    article = storage.load_existing(episode.article_id)
    if article is None or not article.status.processed or not article.html:
        return None
    return article if (storage.root / article.html).is_file() else None


def _process_episode(
    root: Path,
    episode: PodcastEpisode,
    *,
    downloader: PublicEpisodeDownloader,
    transcribe_fn: Callable[..., TranscriptResult],
    output: Callable[[str], None],
) -> Article:
    storage = ArticleStorage(root)
    completed = _completed_article(storage, episode)
    if completed is not None:
        output(f"[DONE] 已完成，跳过重复处理：{episode.title}")
        return completed

    article = storage.start_processing(_article_for_episode(episode))
    article.metadata["podcast_stage"] = "resolving-public-page"
    storage.start_processing(article)

    # downloader 是独立、已清空认证状态的 Session；这里不接收认证客户端。
    public = downloader.fetch_public_episode(episode.eid)
    episode = _merged_episode(episode, public.episode)
    article.title = episode.title
    article.authors = [episode.author] if episode.author else []
    article.publish_date = episode.pub_date
    article.summary = _plain_summary(episode)
    article.metadata["podcast"].update(
        {
            "eid": episode.eid,
            "pid": episode.pid,
            "title": episode.podcast_title,
            "author": episode.author,
            "duration": episode.duration,
            "podcasters": list(episode.podcasters),
        }
    )

    audio_dir = root / "attachments" / "audio" / episode.date
    audio_path = downloader.download_audio(public, audio_dir)
    audio_relative = relative_to_root(audio_path, root)
    _upsert_attachment(
        article,
        Attachment(
            type="audio",
            path=audio_relative,
            title=f"{episode.title} 原始音频",
            metadata={
                "source": "public-web",
                "authenticated": False,
                "media_id": public.media_id,
            },
        ),
    )
    article.metadata["podcast_stage"] = "audio-downloaded"
    article = storage.start_processing(article)
    output(f"[DONE] 公开无登录音频下载：{audio_relative}")

    transcript = transcribe_fn(audio_path, episode, root, output=output)
    transcript_path = (
        root / "attachments" / "transcript" / episode.date / f"{episode.article_id}.json"
    )
    write_transcript_json(transcript_path, transcript)
    transcript_relative = relative_to_root(transcript_path, root)
    _upsert_attachment(
        article,
        Attachment(
            type="transcript",
            path=transcript_relative,
            title=f"{episode.title} Whisper turbo 转录 JSON",
            metadata={"model": WHISPER_UPSTREAM_MODEL},
        ),
    )
    article.metadata["transcription"].update(
        {
            "language": transcript.language,
            "language_probability": transcript.language_probability,
            "gpu_index": transcript.gpu_index,
            "compute_type": transcript.compute_type,
            "speaker_method": transcript.speaker_method,
        }
    )
    article.metadata["podcast_stage"] = "transcribed"
    article = storage.start_processing(article)
    output(f"[DONE] Whisper turbo 本机 GPU 转录：{transcript_relative}")

    html_path = root / "attachments" / "html" / f"{episode.article_id}.html"
    _write_text(html_path, render_dialogue_html(episode, transcript))
    html_relative = relative_to_root(html_path, root)
    _upsert_attachment(
        article,
        Attachment(
            type="html",
            path=html_relative,
            title=f"{episode.title} 对话转录",
            metadata={"format": "dialogue", "speaker_prefix": "name: content"},
        ),
    )
    article.metadata["podcast_stage"] = "completed"
    article.metadata["completed_at"] = utc_now_iso()
    completed = storage.complete(article, html_relative)
    output(f"[DONE] 对话 HTML 与 Article 状态归档：{html_relative}")
    return completed


def _summary_text(job: dict[str, Any]) -> str:
    episodes = job.get("episodes", [])
    completed = sum(item.get("status") == "completed" for item in episodes)
    failed = sum(item.get("status") == "failed" for item in episodes)
    pending = len(episodes) - completed - failed
    polish_success = sum(item.get("polish_status") == "success" for item in episodes)
    polish_failed = sum(item.get("polish_status") == "failed" for item in episodes)
    polish_pending = sum(
        item.get("polish_status") in {"pending", "running"} for item in episodes
    )
    lines = [
        "AirHub 小宇宙批处理任务",
        f"创建时间: {job.get('created_at', '')}",
        f"更新时间: {job.get('updated_at', '')}",
        f"模型: {WHISPER_UPSTREAM_MODEL}",
        "执行位置: 本机 NVIDIA GPU（禁止 Slurm）",
        "下载方式: 公开网页、无登录状态",
        f"状态: {job.get('status', '')}",
        f"总数: {len(episodes)}",
        f"成功: {completed}",
        f"失败: {failed}",
        f"待处理: {pending}",
        f"DeepSeek 润色成功: {polish_success}",
        f"DeepSeek 润色失败: {polish_failed}",
        f"DeepSeek 润色待处理: {polish_pending}",
        "",
        "结果:",
    ]
    for index, item in enumerate(episodes, start=1):
        episode = item.get("episode", {})
        line = f"{index}. [{item.get('status', '')}] {episode.get('title', '')}"
        if item.get("html"):
            line += f" -> {item['html']}"
        if item.get("polish_status"):
            line += f" | 润色={item['polish_status']}"
        if item.get("polished_transcript"):
            line += f" -> {item['polished_transcript']}"
        if item.get("polish_error"):
            line += f" | 润色错误={item['polish_error']}"
        if item.get("error"):
            line += f" | {item['error']}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def process_job(
    root: Path = PROJECT_ROOT,
    *,
    job_path: Path | None = None,
    downloader: PublicEpisodeDownloader | None = None,
    transcribe_fn: Callable[..., TranscriptResult] = transcribe_with_turbo,
    output: Callable[[str], None] = print,
) -> dict[str, Any]:
    """顺序处理整批节目；每项持久化状态，失败后继续下一项。"""

    root = root.resolve()
    ensure_runtime_dirs(root)
    resolved_job, job = _load_job(root, job_path)
    work_dir = (root / str(job.get("work_dir", ""))).resolve()
    if not work_dir.is_relative_to(root) or work_dir == root:
        raise ValueError("任务工作目录无效")
    work_dir.mkdir(parents=True, exist_ok=True)
    summary_path = work_dir / "summary.txt"
    public_downloader = downloader or PublicEpisodeDownloader()

    job["status"] = "running"
    job["started_at"] = job.get("started_at") or utc_now_iso()
    job["updated_at"] = utc_now_iso()
    _write_json(resolved_job, job)
    _write_text(summary_path, _summary_text(job))

    for index, item in enumerate(job["episodes"], start=1):
        if item.get("status") == "completed":
            continue
        episode = PodcastEpisode.from_job_dict(item.get("episode", {}))
        item.update({"status": "running", "stage": "starting", "error": ""})
        job["updated_at"] = utc_now_iso()
        _write_json(resolved_job, job)
        output(f"[INFO] 播客批处理 {index}/{len(job['episodes'])}：{episode.title}")
        try:
            article = _process_episode(
                root,
                episode,
                downloader=public_downloader,
                transcribe_fn=transcribe_fn,
                output=output,
            )
            item.update(
                {
                    "status": "completed",
                    "stage": "completed",
                    "article_id": article.id,
                    "html": article.html,
                    "error": "",
                    "completed_at": utc_now_iso(),
                }
            )
        except Exception as exc:
            item.update(
                {
                    "status": "failed",
                    "stage": "failed",
                    "error": str(exc)[:500],
                    "failed_at": utc_now_iso(),
                }
            )
            trace_path = work_dir / f"error_{index:03d}_{episode.eid}.log"
            _write_text(trace_path, traceback.format_exc())
            output(f"[ERROR] {episode.title}：{exc}；已记录并继续下一项。")
        finally:
            job["updated_at"] = utc_now_iso()
            _write_json(resolved_job, job)
            _write_text(summary_path, _summary_text(job))

    failed = sum(item.get("status") == "failed" for item in job["episodes"])
    job["status"] = "completed_with_errors" if failed else "completed"
    job["finished_at"] = utc_now_iso()
    job["updated_at"] = utc_now_iso()
    _write_json(resolved_job, job)
    _write_text(summary_path, _summary_text(job))
    output(
        f"[DONE] 小宇宙批处理结束：成功 {len(job['episodes']) - failed}，"
        f"失败 {failed}，报告 {relative_to_root(summary_path, root)}"
    )
    return {
        "status": job["status"],
        "total": len(job["episodes"]),
        "succeeded": len(job["episodes"]) - failed,
        "failed": failed,
        "summary": relative_to_root(summary_path, root),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="用公开下载和本机 Whisper turbo 处理当前小宇宙任务"
    )
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="AirHub 项目根目录")
    args = parser.parse_args()
    result = process_job(Path(args.root))
    raise SystemExit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
