"""User-selected completed papers that permanently reinforce priority weights."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .filters import canonical_institution
from .models import Article, utc_now_iso
from .paths import PROJECT_ROOT
from .priority import PriorityProfile, canonical_author
from .storage import ArticleStorage


@dataclass(frozen=True)
class PriorityFeedbackCandidate:
    article_id: str
    title: str
    authors: tuple[str, ...]
    institutions: tuple[str, ...]
    completed_at: str
    already_added: bool


class PriorityFeedbackStore:
    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root.resolve()
        self.path = self.root / "data" / "priority" / "feedback.json"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "updated_at": "", "articles": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"优选反馈文件损坏: {self.path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
            raise ValueError(f"优选反馈文件格式错误: {self.path}")
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload["version"] = 1
        payload["updated_at"] = utc_now_iso()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _article_institutions(article: Article) -> tuple[str, ...]:
        values = {
            canonical_institution(str(value))
            for key in ("institutions", "source_institutions")
            for value in article.metadata.get(key, [])
            if str(value).strip()
        }
        return tuple(sorted(value for value in values if value))

    def list_completed_papers(self) -> list[PriorityFeedbackCandidate]:
        added_ids = {
            str(item.get("article_id", ""))
            for item in self.load().get("articles", [])
            if isinstance(item, dict)
        }
        candidates: list[PriorityFeedbackCandidate] = []
        for article in ArticleStorage(self.root).iter_active():
            html_path = self.root / "attachments" / "html" / f"{article.id}.html"
            if article.type != "paper" or not html_path.is_file():
                continue
            if not (article.status.processed or article.html):
                continue
            candidates.append(
                PriorityFeedbackCandidate(
                    article_id=article.id,
                    title=article.title,
                    authors=tuple(article.authors),
                    institutions=self._article_institutions(article),
                    completed_at=str(article.metadata.get("completed_at", "")),
                    already_added=article.id in added_ids,
                )
            )
        return sorted(
            candidates,
            key=lambda item: (item.completed_at, item.article_id),
            reverse=True,
        )

    def counts(self) -> tuple[Counter[str], Counter[str]]:
        authors: Counter[str] = Counter()
        institutions: Counter[str] = Counter()
        for item in self.load().get("articles", []):
            if not isinstance(item, dict):
                continue
            authors.update(
                str(value)
                for value in item.get("authors", [])
                if str(value).strip()
            )
            institutions.update(
                str(value)
                for value in item.get("institutions", [])
                if str(value).strip()
            )
        return authors, institutions

    def apply_to(self, profile: PriorityProfile) -> PriorityProfile:
        author_counts, institution_counts = self.counts()
        profile.author_counts.update(author_counts)
        profile.institution_counts.update(institution_counts)
        return profile

    def add_articles(self, article_ids: Iterable[str]) -> dict[str, Any]:
        selected = list(dict.fromkeys(str(value) for value in article_ids if str(value)))
        if not selected:
            raise ValueError("没有选择要加入优选频度的已解读文章")
        candidate_map = {
            candidate.article_id: candidate for candidate in self.list_completed_papers()
        }
        missing = [article_id for article_id in selected if article_id not in candidate_map]
        if missing:
            raise ValueError(f"文章不是可用的已解读论文: {', '.join(missing)}")

        payload = self.load()
        existing = {
            str(item.get("article_id", ""))
            for item in payload.get("articles", [])
            if isinstance(item, dict)
        }
        added_ids: list[str] = []
        skipped_ids: list[str] = []
        author_increments = 0
        institution_increments = 0
        for article_id in selected:
            if article_id in existing:
                skipped_ids.append(article_id)
                continue
            candidate = candidate_map[article_id]
            authors = list(
                dict.fromkeys(
                    canonical
                    for value in candidate.authors
                    if (canonical := canonical_author(value))
                )
            )
            institutions = list(candidate.institutions)
            payload["articles"].append(
                {
                    "article_id": article_id,
                    "title": candidate.title,
                    "authors": authors,
                    "institutions": institutions,
                    "added_at": utc_now_iso(),
                }
            )
            existing.add(article_id)
            added_ids.append(article_id)
            author_increments += len(authors)
            institution_increments += len(institutions)
        if added_ids:
            self._save(payload)
        feedback_authors, feedback_institutions = self.counts()
        return {
            "requested": len(selected),
            "added": len(added_ids),
            "skipped": len(skipped_ids),
            "article_ids": added_ids,
            "skipped_ids": skipped_ids,
            "author_increments": author_increments,
            "institution_increments": institution_increments,
            "feedback_articles": len(payload.get("articles", [])),
            "feedback_authors": len(feedback_authors),
            "feedback_institutions": len(feedback_institutions),
        }
