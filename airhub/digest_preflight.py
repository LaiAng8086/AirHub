"""Prepare and validate an Article package before invoking paper-digest."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .codex_digest import _load_article, _resolve_requested_article
from .config import load_config
from .fetchers import registry
from .models import Article, Attachment
from .paths import PROJECT_ROOT, relative_to_root
from .storage import ArticleStorage


REMOTE_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class PackageReport:
    article_id: str
    attachments: int
    local_images: int
    tables: int
    videos: int
    pdf_figures: int
    source_html_sufficient: bool


def _is_remote(value: str) -> bool:
    return urlparse(value).scheme.lower() in REMOTE_SCHEMES or value.startswith("//")


def _local_path(root: Path, value: str) -> Path | None:
    if not value or _is_remote(value):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _deduplicate_attachments(article: Article) -> None:
    unique: dict[tuple[str, str], Attachment] = {}
    for attachment in article.attachments:
        unique[(attachment.type, attachment.path)] = attachment
    article.attachments = list(unique.values())


def validate_digest_package(article: Article, root: Path) -> PackageReport:
    """Require a complete local source and local image assets for the digest skill."""

    root = root.resolve()
    missing_attachments: list[str] = []
    local_source_types: set[str] = set()
    for attachment in article.attachments:
        local = _local_path(root, attachment.path)
        if local is None or not local.is_file():
            missing_attachments.append(attachment.path or "<empty>")
        elif attachment.type in {"html", "pdf"}:
            local_source_types.add(attachment.type)

    local_images = 0
    remote_images: list[str] = []
    missing_images: list[str] = []
    tables = 0
    videos = 0
    for item in article.metadata.get("media_manifest", []) or []:
        item_type = str(item.get("type", ""))
        if item_type == "image":
            src = str(item.get("src", ""))
            if _is_remote(src):
                remote_images.append(src)
                continue
            local = _local_path(root, src)
            if local is None or not local.is_file():
                missing_images.append(src or "<empty>")
            else:
                local_images += 1
        elif item_type == "table":
            tables += 1
        elif item_type == "video":
            videos += 1

    pdf_figures = 0
    missing_pdf_figures: list[str] = []
    for item in article.metadata.get("pdf_figures", []) or []:
        value = str(item.get("path") or item.get("src") or "")
        local = _local_path(root, value)
        if local is None or not local.is_file():
            missing_pdf_figures.append(value or "<empty>")
        else:
            pdf_figures += 1

    problems: list[str] = []
    if not local_source_types:
        problems.append("缺少可读取的本地 HTML 原文或 PDF")
    if missing_attachments:
        problems.append(f"附件不存在: {', '.join(missing_attachments[:5])}")
    if remote_images:
        problems.append(f"仍有 {len(remote_images)} 张网络图片未本地化")
    if missing_images:
        problems.append(f"有 {len(missing_images)} 张本地图片不存在")
    if missing_pdf_figures:
        problems.append(f"有 {len(missing_pdf_figures)} 张 PDF 图不存在")
    if problems:
        raise ValueError("；".join(problems))

    return PackageReport(
        article_id=article.id,
        attachments=len(article.attachments),
        local_images=local_images,
        tables=tables,
        videos=videos,
        pdf_figures=pdf_figures,
        source_html_sufficient=bool(article.metadata.get("source_html_sufficient")),
    )


def _fetcher_for_article(article: Article, root: Path):
    sources = [
        source
        for source in load_config(root).sources
        if source.enabled and source.type == article.source
    ]
    if not sources:
        raise ValueError(f"没有启用的 Fetcher 可以准备来源: {article.source}")
    return registry.create(sources[0], root)


def prepare_digest_package(
    article_value: str,
    root: Path = PROJECT_ROOT,
) -> PackageReport:
    """Let the Producer refresh one package, persist it, then validate it."""

    root = root.resolve()
    article_path = _resolve_requested_article(root, article_value)
    article = _load_article(article_path)
    fetcher = _fetcher_for_article(article, root)
    article = fetcher.prepare(article)
    _deduplicate_attachments(article)
    report = validate_digest_package(article, root)

    payload = article.to_dict()
    storage = ArticleStorage(root)
    storage._write_json(storage.article_path(article.id, "metadata"), payload)
    storage._write_json(article_path, payload)
    storage.rebuild_index()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare an Article for paper-digest")
    parser.add_argument("article", help="inbox/ or processing/ Article JSON")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    args = parser.parse_args()
    try:
        report = prepare_digest_package(args.article, Path(args.root))
    except (FileNotFoundError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    print(
        "[DONE] paper-digest 素材预检 "
        f"article={report.article_id} attachments={report.attachments} "
        f"images={report.local_images} tables={report.tables} videos={report.videos} "
        f"pdf_figures={report.pdf_figures} html_sufficient={report.source_html_sufficient}"
    )


if __name__ == "__main__":
    main()
