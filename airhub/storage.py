"""File-system backed Article queues and metadata indexes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Article
from .paths import PROJECT_ROOT, ensure_runtime_dirs


class ArticleStorage:
    def __init__(self, root: Path = PROJECT_ROOT) -> None:
        self.root = root
        ensure_runtime_dirs(root)
        self.inbox_dir = root / "inbox"
        self.processing_dir = root / "processing"
        self.finished_dir = root / "finished"
        self.filtered_dir = root / "filtered"
        self.metadata_dir = root / "metadata" / "articles"
        self.cache_dir = root / "data" / "article_cache"
        self.index_path = root / "metadata" / "index.json"

    def article_path(self, article_id: str, queue: str = "inbox") -> Path:
        queue_dir = {
            "inbox": self.inbox_dir,
            "processing": self.processing_dir,
            "finished": self.finished_dir,
            "filtered": self.filtered_dir,
            "metadata": self.metadata_dir,
            "cache": self.cache_dir,
        }[queue]
        return queue_dir / f"{article_id}.json"

    def load_existing(self, article_id: str) -> Article | None:
        for queue in ("metadata", "processing", "finished", "inbox", "filtered", "cache"):
            path = self.article_path(article_id, queue)
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    return Article.from_dict(json.load(handle))
        return None

    def load_active(self, article_id: str) -> Article | None:
        for queue in ("metadata", "processing", "finished", "inbox"):
            path = self.article_path(article_id, queue)
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    return Article.from_dict(json.load(handle))
        return None

    def load_cached(self, article_id: str) -> Article | None:
        path = self.article_path(article_id, "cache")
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return Article.from_dict(json.load(handle))

    def cache(self, article: Article) -> Article:
        """Persist a reusable Article package outside the active digest queue."""

        existing = self.load_existing(article.id)
        if existing is not None:
            article.merge_preserving_user_state(existing)
        self._write_json(self.article_path(article.id, "cache"), article.to_dict())
        return article

    def defer(self, article_id: str, rebuild_index: bool = True) -> Article:
        """Move an unfinished active Article back to the reusable cache."""

        article = self.load_active(article_id)
        if article is None:
            raise FileNotFoundError(f"待解读 Article 不存在: {article_id}")
        html_path = self.root / "attachments" / "html" / f"{article.id}.html"
        if article.status.processed or article.html or html_path.exists():
            raise ValueError(f"文章已经完成解读，不能退回缓存: {article.id}")
        self.cache(article)
        for queue in ("inbox", "processing", "metadata"):
            self.article_path(article.id, queue).unlink(missing_ok=True)
        if rebuild_index:
            self.rebuild_index()
        return article

    def save(self, article: Article, rebuild_index: bool = True) -> Article:
        existing = self.load_existing(article.id)
        if existing is not None:
            article.merge_preserving_user_state(existing)

        payload = article.to_dict()
        self._write_json(self.article_path(article.id, "metadata"), payload)
        if not article.status.processed:
            self._write_json(self.article_path(article.id, "inbox"), payload)
        self.article_path(article.id, "filtered").unlink(missing_ok=True)
        if rebuild_index:
            self.rebuild_index()
        return article

    def start_processing(self, article: Article, rebuild_index: bool = True) -> Article:
        """保存 Article 并将队列位置切换到 processing/。

        播客下载与转录是一个可能持续较久的同步任务。先写入 processing/ 可让
        中断后的任务仍进入统一状态视图，并允许下一次运行从已有音频继续。
        """

        existing = self.load_existing(article.id)
        if existing is not None:
            article.merge_preserving_user_state(existing)
            attachments = {
                (attachment.type, attachment.path): attachment
                for attachment in existing.attachments
            }
            attachments.update(
                {
                    (attachment.type, attachment.path): attachment
                    for attachment in article.attachments
                }
            )
            article.attachments = list(attachments.values())
        payload = article.to_dict()
        self._write_json(self.article_path(article.id, "metadata"), payload)
        self._write_json(self.article_path(article.id, "processing"), payload)
        self.article_path(article.id, "inbox").unlink(missing_ok=True)
        self.article_path(article.id, "filtered").unlink(missing_ok=True)
        if rebuild_index:
            self.rebuild_index()
        return article

    def archive(self, article: Article) -> Article:
        existing = self.load_active(article.id)
        if existing is not None:
            article.merge_preserving_user_state(existing)
            attachments = {
                (attachment.type, attachment.path): attachment
                for attachment in existing.attachments
            }
            attachments.update(
                {
                    (attachment.type, attachment.path): attachment
                    for attachment in article.attachments
                }
            )
            article.attachments = list(attachments.values())
        self._write_json(self.article_path(article.id, "filtered"), article.to_dict())
        for queue in ("inbox", "processing", "finished", "metadata"):
            self.article_path(article.id, queue).unlink(missing_ok=True)
        return article

    def complete(self, article: Article, html_path: str) -> Article:
        """Mark a digest complete and move its queue record to finished/."""

        existing = self.load_existing(article.id)
        if existing is not None:
            article.merge_preserving_user_state(existing)
        article.status.processed = True
        article.html = html_path
        payload = article.to_dict()
        self._write_json(self.article_path(article.id, "metadata"), payload)
        self._write_json(self.article_path(article.id, "finished"), payload)
        self.article_path(article.id, "inbox").unlink(missing_ok=True)
        self.article_path(article.id, "processing").unlink(missing_ok=True)
        self.rebuild_index()
        return article

    def iter_active(self) -> list[Article]:
        articles = []
        for path in sorted(self.metadata_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as handle:
                articles.append(Article.from_dict(json.load(handle)))
        return articles

    def rebuild_index(self) -> None:
        articles: list[dict[str, Any]] = []
        for path in sorted(self.metadata_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            articles.append(
                {
                    "id": data.get("id", path.stem),
                    "type": data.get("type", ""),
                    "source": data.get("source", ""),
                    "title": data.get("title", ""),
                    "authors": data.get("authors", []),
                    "publish_date": data.get("publish_date", ""),
                    "fetch_date": data.get("fetch_date", ""),
                    "tags": data.get("tags", []),
                    "status": data.get("status", {}),
                    "url": data.get("url", ""),
                    "html": data.get("html", ""),
                    "summary": data.get("summary", ""),
                    "metadata": {
                        "countries": data.get("metadata", {}).get("countries", []),
                        "institutions": data.get("metadata", {}).get("institutions", []),
                        "download_date": data.get("metadata", {}).get("download_date", ""),
                        "priority": data.get("metadata", {}).get("priority", {}),
                    },
                }
            )
        articles.sort(key=lambda item: item.get("publish_date") or item.get("fetch_date"), reverse=True)
        self._write_json(
            self.index_path,
            {"version": 1, "count": len(articles), "articles": articles},
        )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp.replace(path)
