"""Build a paper priority queue from the newest scope CSV file."""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .filters import canonical_institution, normalize_phrase, phrase_in_text
from .models import Article


AUTHOR_HEADERS = ("author", "authors", "作者")
INSTITUTION_HEADER_MARKERS = (
    "assignee",
    "affiliation",
    "company",
    "institute",
    "institution",
    "laboratory",
    "organisation",
    "organization",
    "publisher",
    "university",
    "机构",
    "单位",
    "隶属",
    "公司",
    "大学",
    "研究院",
    "实验室",
)
INSTITUTION_NAME_MARKERS = (
    "academy",
    "college",
    "company",
    "corporation",
    "institute",
    "laboratory",
    "research",
    "technologies",
    "university",
    "公司",
    "大学",
    "研究院",
    "实验室",
)


def newest_scope_csv(root: Path) -> Path:
    paths = [path for path in (root / "scope").glob("*.csv") if path.is_file()]
    if not paths:
        raise FileNotFoundError(f"scope 目录中没有 CSV 文件: {root / 'scope'}")
    return max(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))


def canonical_author(value: str) -> str:
    """Normalize Zotero's ``surname, given`` and feed's ``given surname`` forms."""

    value = unicodedata.normalize("NFKC", value).strip()
    if "," in value:
        surname, given = value.split(",", 1)
        value = f"{given} {surname}"
    return normalize_phrase(value)


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;|；]", value) if item.strip()]


def _looks_like_institution(value: str, known: set[str]) -> bool:
    normalized = canonical_institution(value)
    if not normalized:
        return False
    return normalized in known or any(marker in normalized for marker in INSTITUTION_NAME_MARKERS)


@dataclass
class PriorityProfile:
    csv_path: Path
    row_count: int
    author_counts: Counter[str]
    institution_counts: Counter[str]

    @classmethod
    def from_latest_csv(
        cls,
        root: Path,
        known_institutions: Iterable[str] = (),
    ) -> "PriorityProfile":
        return cls.from_csv(newest_scope_csv(root), known_institutions)

    @classmethod
    def from_csv(
        cls,
        csv_path: Path,
        known_institutions: Iterable[str] = (),
    ) -> "PriorityProfile":
        known = {
            canonical_institution(str(value))
            for value in known_institutions
            if str(value).strip()
        }
        author_counts: Counter[str] = Counter()
        institution_counts: Counter[str] = Counter()
        row_count = 0
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                row_count += 1
                normalized_headers = {
                    str(header).strip().casefold(): str(value or "")
                    for header, value in row.items()
                    if header is not None
                }
                author_value = next(
                    (normalized_headers[key] for key in AUTHOR_HEADERS if key in normalized_headers),
                    "",
                )
                for raw_author in _split_values(author_value):
                    author = canonical_author(raw_author)
                    if author:
                        author_counts[author] += 1

                row_institutions: set[str] = set()
                for header, value in normalized_headers.items():
                    if not value or not any(marker in header for marker in INSTITUTION_HEADER_MARKERS):
                        continue
                    header_is_explicit = "institution" in header or "affiliation" in header
                    for item in _split_values(value):
                        if header_is_explicit or _looks_like_institution(item, known):
                            row_institutions.add(canonical_institution(item))
                    normalized_value = normalize_phrase(value)
                    for institution in known:
                        if phrase_in_text(institution, normalized_value):
                            row_institutions.add(institution)
                institution_counts.update(item for item in row_institutions if item)

        return cls(
            csv_path=csv_path,
            row_count=row_count,
            author_counts=author_counts,
            institution_counts=institution_counts,
        )

    def score(self, article: Article) -> dict[str, object]:
        matched_authors: dict[str, int] = {}
        for display_name in article.authors:
            normalized = canonical_author(display_name)
            count = self.author_counts.get(normalized, 0)
            if count:
                matched_authors[display_name] = count

        article_institutions = {
            canonical_institution(str(value))
            for key in ("institutions", "source_institutions")
            for value in article.metadata.get(key, [])
            if str(value).strip()
        }
        affiliation_text = normalize_phrase(
            str(article.metadata.get("affiliation_source_text", ""))
        )
        matched_institutions: dict[str, int] = {}
        for institution, count in self.institution_counts.items():
            if institution in article_institutions or phrase_in_text(institution, affiliation_text):
                matched_institutions[institution] = count

        author_score = sum(matched_authors.values())
        institution_score = sum(matched_institutions.values())
        return {
            "score": author_score + institution_score,
            "author_score": author_score,
            "institution_score": institution_score,
            "matched_authors": matched_authors,
            "matched_institutions": matched_institutions,
        }

    def rank(self, articles: Iterable[Article]) -> list[Article]:
        ranked = list(articles)
        for article in ranked:
            article.metadata["priority"] = {
                **self.score(article),
                "scope_csv": self.csv_path.name,
            }
        ranked.sort(
            key=lambda article: (
                int(article.metadata["priority"]["score"]),
                int(article.metadata["priority"]["institution_score"]),
                int(article.metadata["priority"]["author_score"]),
                article.publish_date,
                article.id,
            ),
            reverse=True,
        )
        for rank, article in enumerate(ranked, start=1):
            article.metadata["priority"]["rank"] = rank
        return ranked
