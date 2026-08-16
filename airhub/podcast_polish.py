"""Prepare, validate, and commit DeepSeek podcast transcript polish jobs."""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Article, Attachment, utc_now_iso
from .paths import PROJECT_ROOT, ensure_runtime_dirs, relative_to_root
from .podcast_transcript import (
    TranscriptResult,
    TranscriptSegment,
    merge_speaker_turns,
    render_dialogue_html,
    speaker_candidates,
)
from .podcast_worker import (
    CURRENT_JOB,
    _summary_text,
    _upsert_attachment,
    _write_json,
    _write_text,
)
from .storage import ArticleStorage
from .xiaoyuzhou import PodcastEpisode


POLISH_SKILL = "podcast-transcript-polisher"
MAX_CHUNK_CHARS = 12_000
MAX_CHUNK_SEGMENTS = 80
CONTEXT_SEGMENTS = 3
PLAIN_TEXT_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
PLAIN_TEXT_MARKDOWN_RE = re.compile(
    r"```|!\[[^\]]*\]\(|\[[^\]]+\]\([^)]+\)|^\s{0,3}#{1,6}\s",
    re.MULTILINE,
)


@dataclass(frozen=True)
class PodcastPolishJob:
    manifest_path: Path
    work_dir: Path
    task_count: int


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取{label} JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON 顶层必须是对象：{path}")
    return payload


def _project_path(root: Path, relative: object, *, must_exist: bool = False) -> Path:
    path = (root / str(relative or "")).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"任务路径必须位于 AirHub 项目内：{relative}")
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"任务文件不存在：{path}")
    return path


def _unique_directory(parent: Path, stem: str) -> Path:
    path = parent / stem
    suffix = 2
    while path.exists():
        path = parent / f"{stem}_{suffix}"
        suffix += 1
    path.mkdir(parents=True)
    return path


def _source_transcript(article: Article, root: Path) -> tuple[Path, dict[str, Any], str]:
    candidates = [item for item in article.attachments if item.type == "transcript"]
    if not candidates:
        raise ValueError(f"Article 缺少原始 Whisper transcript attachment：{article.id}")
    path = _project_path(root, candidates[-1].path, must_exist=True)
    raw = path.read_bytes()
    payload = _load_json(path, "Whisper transcript")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError(f"Whisper transcript 没有可润色片段：{path}")
    return path, payload, hashlib.sha256(raw).hexdigest()


def _plain_context(episode: PodcastEpisode) -> str:
    raw = "\n".join(
        value for value in (episode.title, episode.description, episode.shownotes) if value
    )
    plain = html_module.unescape(re.sub(r"<[^>]+>", " ", raw))
    return " ".join(plain.split())[:4_000]


def _normalise_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for index, raw in enumerate(payload.get("segments", []), start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Whisper transcript 第 {index} 个片段不是对象")
        text = str(raw.get("text", "")).strip()
        if not text:
            raise ValueError(f"Whisper transcript 第 {index} 个片段文本为空")
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Whisper transcript 第 {index} 个片段时间无效") from exc
        normalised.append(
            {
                "segment_id": f"seg-{index:06d}",
                "start": start,
                "end": max(start, end),
                "source_speaker": str(raw.get("speaker", "")).strip() or "说话人",
                "text": text,
            }
        )
    return normalised


def _chunk_segments(
    segments: list[dict[str, Any]],
    *,
    max_chars: int,
    max_segments: int,
) -> list[list[dict[str, Any]]]:
    if max_chars <= 0 or max_segments <= 0:
        raise ValueError("播客润色分块限制必须为正整数")
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for segment in segments:
        size = len(str(segment["text"])) + len(str(segment["source_speaker"]))
        if current and (len(current) >= max_segments or chars + size > max_chars):
            chunks.append(current)
            current = []
            chars = 0
        current.append(segment)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def _episode_item(job: dict[str, Any], article_id: str) -> dict[str, Any] | None:
    for item in job.get("episodes", []):
        if not isinstance(item, dict):
            continue
        episode = item.get("episode", {})
        if isinstance(episode, dict):
            try:
                candidate = PodcastEpisode.from_job_dict(episode).article_id
            except (TypeError, ValueError):
                continue
            if candidate == article_id:
                return item
    return None


def _write_job_state(root: Path, job_path: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = utc_now_iso()
    _write_json(job_path, job)
    work_dir = _project_path(root, job.get("work_dir", ""))
    _write_text(work_dir / "summary.txt", _summary_text(job))


def prepare_podcast_polish_job(
    root: Path = PROJECT_ROOT,
    *,
    now: datetime | None = None,
    max_chars: int = MAX_CHUNK_CHARS,
    max_segments: int = MAX_CHUNK_SEGMENTS,
) -> PodcastPolishJob | None:
    """Create bounded skill tasks for completed episodes in the current batch."""

    root = root.resolve()
    ensure_runtime_dirs(root)
    job_path = root / CURRENT_JOB
    if not job_path.is_file():
        return None
    job = _load_json(job_path, "小宇宙当前任务")
    work_dir = _project_path(root, job.get("work_dir", ""))
    work_dir.mkdir(parents=True, exist_ok=True)
    storage = ArticleStorage(root)
    episodes: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d_%H%M%S")
    polish_dir: Path | None = None
    for item in job.get("episodes", []):
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        try:
            episode = PodcastEpisode.from_job_dict(item.get("episode", {}))
        except (TypeError, ValueError):
            continue
        article = storage.load_existing(episode.article_id)
        if article is None or not article.status.processed or not article.html:
            continue
        podcast_metadata = article.metadata.get("podcast", {})
        if isinstance(podcast_metadata, dict):
            enriched_episode = episode.to_job_dict()
            enriched_episode.update(
                {
                    "title": article.title or episode.title,
                    "podcast_title": podcast_metadata.get("title") or episode.podcast_title,
                    "author": podcast_metadata.get("author") or episode.author,
                    "duration": podcast_metadata.get("duration") or episode.duration,
                    "podcasters": podcast_metadata.get("podcasters") or list(episode.podcasters),
                }
            )
            episode = PodcastEpisode.from_job_dict(enriched_episode)
        html_path = _project_path(root, article.html)
        if not html_path.is_file():
            continue
        try:
            transcript_path, transcript, source_sha = _source_transcript(article, root)
        except (FileNotFoundError, ValueError):
            continue
        previous = article.metadata.get("podcast_polish", {})
        if (
            isinstance(previous, dict)
            and previous.get("status") == "success"
            and previous.get("source_sha256") == source_sha
        ):
            item["polish_status"] = "success"
            continue

        if polish_dir is None:
            polish_dir = _unique_directory(work_dir, f"deepseek_polish_{stamp}")
        source_segments = _normalise_segments(transcript)
        chunks = _chunk_segments(
            source_segments,
            max_chars=max_chars,
            max_segments=max_segments,
        )
        episode_entry = {
            "article_id": article.id,
            "episode": episode.to_job_dict(),
            "source_transcript": relative_to_root(transcript_path, root),
            "source_sha256": source_sha,
            "html": article.html,
            "chunk_ids": [],
            "status": "pending",
            "error": "",
        }
        episode_index = len(episodes) + 1
        for chunk_index, chunk in enumerate(chunks, start=1):
            task_id = f"episode-{episode_index:03d}-chunk-{chunk_index:04d}"
            chunk_id = f"chunk-{chunk_index:04d}"
            chunk_start = source_segments.index(chunk[0])
            chunk_end = chunk_start + len(chunk)
            task_dir = polish_dir / task_id
            task_dir.mkdir(parents=True)
            chunk_job_path = task_dir / "job.json"
            result_path = task_dir / "result.json"
            chunk_payload = {
                "version": 1,
                "chunk_id": chunk_id,
                "article_id": article.id,
                "episode": episode.to_job_dict(),
                "participant_candidates": speaker_candidates(episode),
                "terminology_context": _plain_context(episode),
                "context_before": source_segments[
                    max(0, chunk_start - CONTEXT_SEGMENTS) : chunk_start
                ],
                "segments": chunk,
                "context_after": source_segments[
                    chunk_end : chunk_end + CONTEXT_SEGMENTS
                ],
                "result_path": relative_to_root(result_path, root),
            }
            _write_json(chunk_job_path, chunk_payload)
            task = {
                "task_id": task_id,
                "article_id": article.id,
                "chunk_id": chunk_id,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
                "job_path": relative_to_root(chunk_job_path, root),
                "result_path": relative_to_root(result_path, root),
                "status": "pending",
                "error": "",
            }
            tasks.append(task)
            episode_entry["chunk_ids"].append(task_id)
        episodes.append(episode_entry)
        item.update(
            {
                "polish_status": "pending",
                "polish_stage": "prepared",
                "polish_error": "",
                "polish_chunks": len(chunks),
            }
        )

    if not tasks or polish_dir is None:
        _write_job_state(root, job_path, job)
        return None

    manifest_path = polish_dir / "manifest.json"
    manifest = {
        "version": 1,
        "created_at": utc_now_iso(),
        "skill": POLISH_SKILL,
        "job_path": relative_to_root(job_path, root),
        "work_dir": relative_to_root(polish_dir, root),
        "episodes": episodes,
        "tasks": tasks,
        "status": "pending",
    }
    _write_json(manifest_path, manifest)
    job["status"] = "polish_pending"
    job["podcast_polish_manifest"] = relative_to_root(manifest_path, root)
    _write_job_state(root, job_path, job)
    return PodcastPolishJob(manifest_path, polish_dir, len(tasks))


def _load_manifest(root: Path, manifest_path: Path) -> tuple[Path, dict[str, Any]]:
    resolved = manifest_path if manifest_path.is_absolute() else root / manifest_path
    resolved = resolved.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("播客润色 manifest 必须是项目内的现有文件")
    manifest = _load_json(resolved, "播客润色 manifest")
    if manifest.get("skill") != POLISH_SKILL or not isinstance(manifest.get("tasks"), list):
        raise ValueError("播客润色 manifest 格式无效")
    return resolved, manifest


def next_podcast_polish_task(root: Path, manifest_path: Path) -> dict[str, Any] | None:
    _, manifest = _load_manifest(root.resolve(), manifest_path)
    tasks = manifest["tasks"]
    for index, task in enumerate(tasks, start=1):
        if isinstance(task, dict) and task.get("status") == "pending":
            return {**task, "progress_index": index, "progress_total": len(tasks)}
    return None


def _plain_field(value: object, label: str, *, max_length: int) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"{label}不能为空")
    if len(rendered) > max_length:
        raise ValueError(f"{label}过长")
    if PLAIN_TEXT_TAG_RE.search(rendered):
        raise ValueError(f"{label}不能包含 HTML")
    if PLAIN_TEXT_MARKDOWN_RE.search(rendered):
        raise ValueError(f"{label}不能包含 Markdown")
    return rendered


def _validate_result(job: dict[str, Any], result: dict[str, Any]) -> list[str]:
    if set(result) != {"version", "chunk_id", "segments", "notes"}:
        raise ValueError("结果顶层字段必须严格为 version/chunk_id/segments/notes")
    if result.get("version") != 1 or result.get("chunk_id") != job.get("chunk_id"):
        raise ValueError("结果 version 或 chunk_id 与任务不一致")
    source_segments = job.get("segments", [])
    result_segments = result.get("segments")
    if not isinstance(result_segments, list):
        raise ValueError("结果 segments 必须是数组")
    source_ids = [str(item.get("segment_id", "")) for item in source_segments]
    result_ids = [
        str(item.get("segment_id", ""))
        for item in result_segments
        if isinstance(item, dict)
    ]
    if len(result_ids) != len(result_segments) or result_ids != source_ids:
        raise ValueError("结果 segment_id 必须完整、唯一并保持原顺序")

    source_chars = sum(len(str(item.get("text", "")).strip()) for item in source_segments)
    corrected_chars = 0
    for index, (source, corrected) in enumerate(zip(source_segments, result_segments), start=1):
        if set(corrected) != {"segment_id", "speaker", "text"}:
            raise ValueError(f"第 {index} 个结果片段字段无效")
        _plain_field(corrected.get("speaker"), f"第 {index} 个 speaker", max_length=80)
        text = _plain_field(corrected.get("text"), f"第 {index} 个 text", max_length=30_000)
        source_length = len(str(source.get("text", "")).strip())
        if len(text) > max(400, source_length * 3):
            raise ValueError(f"第 {index} 个结果片段相对原文异常膨胀")
        corrected_chars += len(text)
    if source_chars and not 0.5 <= corrected_chars / source_chars <= 1.8:
        raise ValueError("纠正后文本总长度与原文差异过大")

    notes = result.get("notes")
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        raise ValueError("结果 notes 必须是字符串数组")
    return [note.strip() for note in notes if note.strip()][:100]


def accept_podcast_polish_task(
    root: Path,
    manifest_path: Path,
    task_id: str,
    *,
    codex_status: int,
) -> dict[str, Any]:
    """Validate one skill result and persist accepted/failed task state."""

    root = root.resolve()
    resolved, manifest = _load_manifest(root, manifest_path)
    task = next(
        (
            item
            for item in manifest["tasks"]
            if isinstance(item, dict) and item.get("task_id") == task_id
        ),
        None,
    )
    if task is None:
        raise ValueError(f"manifest 中不存在任务：{task_id}")
    if task.get("status") != "pending":
        return task
    error = ""
    notes: list[str] = []
    if codex_status != 0:
        error = f"DeepSeek Codex 返回 status={codex_status}"
    else:
        try:
            job = _load_json(
                _project_path(root, task.get("job_path"), must_exist=True),
                "润色分块任务",
            )
            result = _load_json(
                _project_path(root, task.get("result_path"), must_exist=True),
                "润色分块结果",
            )
            notes = _validate_result(job, result)
        except (FileNotFoundError, ValueError) as exc:
            error = str(exc)
    task["status"] = "failed" if error else "accepted"
    task["error"] = error[:500]
    task["notes"] = notes
    task["accepted_at"] = utc_now_iso()
    manifest["status"] = "running"
    manifest["updated_at"] = utc_now_iso()
    _write_json(resolved, manifest)
    return task


def _transcript_result(payload: dict[str, Any], segments: list[TranscriptSegment]) -> TranscriptResult:
    return TranscriptResult(
        language=str(payload.get("language", "")),
        language_probability=float(payload.get("language_probability", 0.0) or 0.0),
        duration=float(payload.get("duration", 0.0) or 0.0),
        gpu_index=int(payload.get("gpu_index", 0) or 0),
        compute_type=str(payload.get("compute_type", "")),
        speaker_method=f"{payload.get('speaker_method', 'unknown')}+deepseek-text-review",
        segments=tuple(segments),
    )


def _commit_episode(
    root: Path,
    storage: ArticleStorage,
    job: dict[str, Any],
    manifest: dict[str, Any],
    episode_entry: dict[str, Any],
) -> None:
    article_id = str(episode_entry["article_id"])
    article = storage.load_existing(article_id)
    item = _episode_item(job, article_id)
    if article is None or item is None:
        raise ValueError(f"无法定位待提交的播客 Article：{article_id}")
    episode = PodcastEpisode.from_job_dict(
        episode_entry.get("episode", item.get("episode", {}))
    )
    raw_path = _project_path(root, episode_entry.get("source_transcript"), must_exist=True)
    if hashlib.sha256(raw_path.read_bytes()).hexdigest() != episode_entry.get("source_sha256"):
        raise ValueError(f"原始 Whisper transcript 已变化：{article_id}")
    raw = _load_json(raw_path, "Whisper transcript")
    originals = _normalise_segments(raw)
    source_by_id = {segment["segment_id"]: segment for segment in originals}
    corrected: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    task_by_id = {
        str(task.get("task_id")): task
        for task in manifest["tasks"]
        if isinstance(task, dict)
    }
    for task_id in episode_entry.get("chunk_ids", []):
        task = task_by_id[str(task_id)]
        result = _load_json(
            _project_path(root, task.get("result_path"), must_exist=True),
            "润色分块结果",
        )
        for segment in result["segments"]:
            corrected[str(segment["segment_id"])] = segment
        notes.extend(str(note).strip() for note in task.get("notes", []) if str(note).strip())
    if list(corrected) != list(source_by_id):
        raise ValueError(f"合并后的 segment 覆盖或顺序无效：{article_id}")

    polished_segments = [
        TranscriptSegment(
            start=float(source_by_id[segment_id]["start"]),
            end=float(source_by_id[segment_id]["end"]),
            text=str(corrected[segment_id]["text"]).strip(),
            speaker=str(corrected[segment_id]["speaker"]).strip(),
        )
        for segment_id in source_by_id
    ]
    merged = merge_speaker_turns(polished_segments)
    transcript = _transcript_result(raw, merged)
    unique_notes = list(dict.fromkeys(notes))[:100]
    completed_at = utc_now_iso()
    polished_payload = transcript.to_dict()
    polished_payload.update(
        {
            "version": 1,
            "source_transcript": relative_to_root(raw_path, root),
            "source_sha256": episode_entry["source_sha256"],
            "polish_skill": POLISH_SKILL,
            "polished_at": completed_at,
            "notes": unique_notes,
        }
    )
    polished_path = (
        root
        / "attachments"
        / "transcript"
        / episode.date
        / f"{article_id}_deepseek.json"
    )
    _write_json(polished_path, polished_payload)
    html_path = _project_path(root, article.html)
    rendered = render_dialogue_html(
        episode,
        transcript,
        polished=True,
        polish_notes=unique_notes,
    )
    _write_text(html_path, rendered)
    polished_relative = relative_to_root(polished_path, root)
    _upsert_attachment(
        article,
        Attachment(
            type="transcript_polished",
            path=polished_relative,
            title=f"{episode.title} DeepSeek 校订转录 JSON",
            metadata={
                "skill": POLISH_SKILL,
                "source_sha256": episode_entry["source_sha256"],
            },
        ),
    )
    article.metadata["podcast_polish"] = {
        "status": "success",
        "skill": POLISH_SKILL,
        "source_sha256": episode_entry["source_sha256"],
        "chunks": len(episode_entry.get("chunk_ids", [])),
        "polished_at": completed_at,
        "notes": unique_notes,
    }
    article.metadata["podcast_stage"] = "completed-polished"
    storage.complete(article, article.html)
    episode_entry.update({"status": "success", "polished_at": completed_at, "error": ""})
    item.update(
        {
            "polish_status": "success",
            "polish_stage": "completed-polished",
            "polish_error": "",
            "polished_transcript": polished_relative,
            "polished_at": completed_at,
            "stage": "completed-polished",
        }
    )


def _record_episode_failure(
    root: Path,
    storage: ArticleStorage,
    job: dict[str, Any],
    episode_entry: dict[str, Any],
    error: str,
) -> None:
    article_id = str(episode_entry["article_id"])
    episode_entry.update({"status": "failed", "error": error[:500]})
    item = _episode_item(job, article_id)
    if item is not None:
        item.update(
            {
                "polish_status": "failed",
                "polish_stage": "failed",
                "polish_error": error[:500],
            }
        )
    article = storage.load_existing(article_id)
    if article is not None and article.status.processed and article.html:
        article.metadata["podcast_polish"] = {
            "status": "failed",
            "skill": POLISH_SKILL,
            "source_sha256": episode_entry.get("source_sha256", ""),
            "error": error[:500],
            "failed_at": utc_now_iso(),
        }
        # Re-save Article metadata while retaining its last valid draft HTML.
        storage.complete(article, article.html)


def finalize_podcast_polish_job(root: Path, manifest_path: Path) -> dict[str, Any]:
    """Commit only fully accepted episodes; never replace HTML on partial failure."""

    root = root.resolve()
    resolved, manifest = _load_manifest(root, manifest_path)
    job_path = _project_path(root, manifest.get("job_path"), must_exist=True)
    job = _load_json(job_path, "小宇宙当前任务")
    storage = ArticleStorage(root)
    task_by_id = {
        str(task.get("task_id")): task
        for task in manifest["tasks"]
        if isinstance(task, dict)
    }
    succeeded = 0
    failed = 0
    for episode_entry in manifest.get("episodes", []):
        if not isinstance(episode_entry, dict):
            continue
        tasks = [
            task_by_id.get(str(task_id), {})
            for task_id in episode_entry.get("chunk_ids", [])
        ]
        invalid = [task for task in tasks if task.get("status") != "accepted"]
        if invalid:
            reasons = [str(task.get("error") or task.get("status") or "未处理") for task in invalid]
            _record_episode_failure(root, storage, job, episode_entry, "；".join(reasons))
            failed += 1
            continue
        try:
            _commit_episode(root, storage, job, manifest, episode_entry)
            succeeded += 1
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            _record_episode_failure(root, storage, job, episode_entry, str(exc))
            failed += 1

    manifest["status"] = "completed_with_errors" if failed else "completed"
    manifest["finished_at"] = utc_now_iso()
    manifest["summary"] = {"episodes_succeeded": succeeded, "episodes_failed": failed}
    _write_json(resolved, manifest)
    worker_failed = any(item.get("status") == "failed" for item in job.get("episodes", []))
    job["status"] = "completed_with_errors" if worker_failed or failed else "completed"
    job["polish_finished_at"] = utc_now_iso()
    _write_job_state(root, job_path, job)
    return {
        "status": manifest["status"],
        "succeeded": succeeded,
        "failed": failed,
        "manifest": relative_to_root(resolved, root),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="准备并收尾小宇宙 DeepSeek 转录润色任务")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--prepare", action="store_true")
    operation.add_argument("--next", metavar="MANIFEST")
    operation.add_argument("--accept", nargs=2, metavar=("MANIFEST", "TASK_ID"))
    operation.add_argument("--finalize", metavar="MANIFEST")
    parser.add_argument("--codex-status", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.prepare:
            prepared = prepare_podcast_polish_job(root)
            if prepared is not None:
                print(relative_to_root(prepared.manifest_path, root))
                print(prepared.task_count)
            return
        if args.next:
            task = next_podcast_polish_task(root, Path(args.next))
            if task is not None:
                print(task["task_id"])
                print(task["job_path"])
                print(task["result_path"])
                print(task["progress_index"])
                print(task["progress_total"])
            return
        if args.accept:
            result = accept_podcast_polish_task(
                root,
                Path(args.accept[0]),
                args.accept[1],
                codex_status=args.codex_status,
            )
            print(result["status"])
            if result.get("error"):
                print(result["error"])
            return
        result = finalize_podcast_polish_job(root, Path(args.finalize))
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
