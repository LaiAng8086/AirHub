"""Select a Producer-prepared Article for non-interactive Codex digestion."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import Article
from .paths import PROJECT_ROOT, relative_to_root
from .storage import ArticleStorage


@dataclass(frozen=True)
class DigestTask:
    article_path: Path
    output_path: Path


@dataclass(frozen=True)
class DigestCandidate:
    task: DigestTask
    article_id: str
    title: str
    added_date: str
    priority_rank: int | None


def _safe_article_id(article_id: str) -> str:
    value = article_id.strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"不安全的 Article ID: {article_id!r}")
    return value


def _load_article(path: Path) -> Article:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 Article JSON: {path}") from exc
    try:
        return Article.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"无效的 Article JSON: {path}") from exc


def _resolve_article_candidate(root: Path, requested: str) -> Path:
    path = Path(requested)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    allowed_dirs = {(root / "inbox").resolve(), (root / "processing").resolve()}
    if path.parent not in allowed_dirs:
        raise ValueError("Article JSON 必须位于 inbox/ 或 processing/")
    if path.suffix.lower() != ".json":
        raise ValueError("Article 文件必须是 JSON")
    return path


def _resolve_requested_article(root: Path, requested: str) -> Path:
    path = _resolve_article_candidate(root, requested)
    if not path.is_file():
        raise FileNotFoundError(f"Article JSON 不存在: {path}")
    return path


def _parse_time(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _task_for_path(root: Path, path: Path) -> tuple[DigestTask, Article]:
    article = _load_article(path)
    article_id = _safe_article_id(article.id)
    output_path = root / "attachments" / "html" / f"{article_id}.html"
    return DigestTask(path, output_path), article


def list_digest_candidates(root: Path = PROJECT_ROOT) -> list[DigestCandidate]:
    """List every prepared Article that still needs its default digest HTML."""

    root = root.resolve()
    candidates: list[tuple[tuple[float | int | str, ...], DigestCandidate]] = []
    seen_ids: set[str] = set()
    for queue in ("processing", "inbox"):
        for path in sorted((root / queue).glob("*.json")):
            try:
                task, article = _task_for_path(root, path.resolve())
            except ValueError:
                continue
            if article.id in seen_ids or task.output_path.exists():
                continue
            seen_ids.add(article.id)
            priority = article.metadata.get("priority", {}) or {}
            raw_rank = priority.get("rank")
            try:
                rank = int(raw_rank)
            except (TypeError, ValueError):
                rank = 0
            added_value = str(
                article.metadata.get("download_date")
                or article.fetch_date
                or article.publish_date
                or ""
            )
            added_date = added_value[:10] or "日期未知"
            added_time = _parse_time(added_value)
            article_time = _parse_time(article.publish_date or article.fetch_date)
            if rank > 0:
                sort_key: tuple[float | int | str, ...] = (
                    0,
                    -added_time,
                    rank,
                    -article_time,
                    article.id,
                )
            else:
                sort_key = (1, -added_time, 0, -article_time, article.id)
            candidate = DigestCandidate(
                task=task,
                article_id=article.id,
                title=article.title,
                added_date=added_date,
                priority_rank=rank if rank > 0 else None,
            )
            candidates.append((sort_key, candidate))
    candidates.sort(key=lambda item: item[0])
    return [item[1] for item in candidates]


def select_digest_task(root: Path = PROJECT_ROOT, requested: str | None = None) -> DigestTask | None:
    root = root.resolve()
    if requested:
        task, _ = _task_for_path(root, _resolve_requested_article(root, requested))
        return task

    candidates = list_digest_candidates(root)
    if not candidates:
        return None
    return candidates[0].task


def complete_digest_task(root: Path, article_value: str, output_value: str) -> None:
    root = root.resolve()
    output_path = Path(output_value)
    if not output_path.is_absolute():
        output_path = root / output_path
    output_path = output_path.resolve()
    html_dir = (root / "attachments" / "html").resolve()
    if output_path.parent != html_dir or output_path.suffix.lower() != ".html":
        raise ValueError("解读 HTML 必须位于 attachments/html/")
    if not output_path.is_file():
        raise FileNotFoundError(f"解读 HTML 不存在: {output_path}")

    article_path = _resolve_article_candidate(root, article_value)
    output_relative = relative_to_root(output_path, root)
    if not article_path.is_file():
        # Older prompts allowed the inner Codex session to call --complete by
        # itself.  Treat an already-consistent finished record as success so
        # the outer runner can recover without spending the subscription quota
        # a second time.
        finished_path = (root / "finished" / article_path.name).resolve()
        if not finished_path.is_file():
            raise FileNotFoundError(f"Article JSON 不存在: {article_path}")
        finished = _load_article(finished_path)
        article_id = _safe_article_id(finished.id)
        if finished_path.name != f"{article_id}.json":
            raise ValueError("finished Article 文件名与 ID 不一致")
        if not finished.status.processed or finished.html != output_relative:
            raise ValueError("finished Article 状态与解读 HTML 不一致")
        return

    article = _load_article(article_path)
    ArticleStorage(root).complete(article, output_relative)


def main() -> None:
    parser = argparse.ArgumentParser(description="Select the next Article for paper-digest")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--article", help="Optional inbox/processing Article JSON")
    parser.add_argument(
        "--complete",
        nargs=2,
        metavar=("ARTICLE_JSON", "OUTPUT_HTML"),
        help="Mark one successfully embedded digest as finished",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.complete:
            complete_digest_task(root, args.complete[0], args.complete[1])
            return
        task = select_digest_task(root, args.article)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    if task is None:
        return
    print(relative_to_root(task.article_path, root))
    print(relative_to_root(task.output_path, root))


if __name__ == "__main__":
    main()
