"""Manual and overflow operations for the pending-digest Article queue."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .blog_archive import BlogSiteStore, archive_blog, blog_archive_mode
from .config import load_config, load_settings
from .daily_state import load_daily_state, save_daily_state
from .fetchers import registry
from .fetchers.arxiv import arxiv_base_id, normalize_arxiv_id
from .models import Article, utc_now_iso
from .paths import PROJECT_ROOT, ensure_runtime_dirs, relative_to_root
from .producer import setup_logging
from .storage import ArticleStorage
from .workflow import load_candidate_snapshot, selection_report_path


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def list_manual_files(root: Path = PROJECT_ROOT) -> list[Path]:
    """Return every txt list below manual/, sorted by its relative path."""

    manual_dir = root.resolve() / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    return sorted(
        (path.resolve() for path in manual_dir.rglob("*.txt") if path.is_file()),
        key=lambda path: path.relative_to(manual_dir).as_posix().lower(),
    )


@dataclass(frozen=True)
class ManualEntry:
    kind: str
    value: str
    line_number: int


def read_manual_entries(path: Path) -> list[ManualEntry]:
    """Read a mixed manual list of arXiv IDs/URLs and Blog article URLs."""

    values: list[ManualEntry] = []
    seen: set[tuple[str, str]] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        parsed = urlparse(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            hostname = parsed.hostname.lower()
            if hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
                try:
                    normalized = normalize_arxiv_id(value)
                except ValueError as exc:
                    raise ValueError(f"{path.name} 第 {line_number} 行: {exc}") from exc
                entry = ManualEntry("arxiv", normalized, line_number)
            else:
                entry = ManualEntry("blog", value, line_number)
        else:
            try:
                normalized = normalize_arxiv_id(value)
            except ValueError as exc:
                raise ValueError(
                    f"{path.name} 第 {line_number} 行既不是有效 arXiv 编号，也不是 http(s) Blog 地址: {value}"
                ) from exc
            entry = ManualEntry("arxiv", normalized, line_number)
        key = (entry.kind, entry.value)
        if key not in seen:
            seen.add(key)
            values.append(entry)
    if not values:
        raise ValueError(f"手动列表没有有效的 arXiv 编号或 Blog 地址: {path}")
    return values


def read_manual_arxiv_ids(path: Path) -> list[str]:
    """Compatibility helper returning only arXiv entries from a mixed list."""

    values = [entry.value for entry in read_manual_entries(path) if entry.kind == "arxiv"]
    if not values:
        raise ValueError(f"手动列表没有有效的 arXiv 编号: {path}")
    return values


def _validate_manual_path(root: Path, path: Path) -> Path:
    root = root.resolve()
    manual_dir = (root / "manual").resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(manual_dir)
    except ValueError as exc:
        raise ValueError("手动导入文件必须位于 manual/ 目录") from exc
    if resolved.suffix.lower() != ".txt" or not resolved.is_file():
        raise ValueError("手动导入文件必须是 manual/ 下存在的 txt 文件")
    return resolved


def _arxiv_fetcher(root: Path) -> Any:
    sources = [source for source in load_config(root).sources if source.type == "arxiv"]
    if not sources:
        raise ValueError("config/sources.json 中没有 arXiv 数据源配置")
    source = next((item for item in sources if item.enabled), sources[0])
    return registry.create(source, root)


def _map_articles_by_request(articles: list[Article]) -> tuple[dict[str, Article], dict[str, Article]]:
    exact: dict[str, Article] = {}
    by_base: dict[str, Article] = {}
    for article in articles:
        raw_id = str(article.metadata.get("arxiv_id", ""))
        if not raw_id:
            continue
        identifier = normalize_arxiv_id(raw_id)
        exact[identifier] = article
        by_base.setdefault(arxiv_base_id(identifier), article)
    return exact, by_base


def _stored_arxiv_maps(storage: ArticleStorage) -> tuple[
    dict[str, Article], dict[str, Article], dict[str, Article], dict[str, Article]
]:
    active_exact: dict[str, Article] = {}
    active_base: dict[str, Article] = {}
    cached_exact: dict[str, Article] = {}
    cached_base: dict[str, Article] = {}

    def add(
        article: Article,
        exact: dict[str, Article],
        by_base: dict[str, Article],
    ) -> None:
        raw_id = str(article.metadata.get("arxiv_id", ""))
        if not raw_id:
            return
        identifier = normalize_arxiv_id(raw_id)
        exact[identifier] = article
        base = arxiv_base_id(identifier)
        current = by_base.get(base)
        if current is None or _arxiv_version(article) > _arxiv_version(current):
            by_base[base] = article

    for article in storage.iter_active():
        add(article, active_exact, active_base)
    for path in storage.cache_dir.glob("*.json"):
        try:
            article = Article.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        add(article, cached_exact, cached_base)
    return active_exact, active_base, cached_exact, cached_base


def _arxiv_version(article: Article) -> int:
    match = re.search(r"v(\d+)$", str(article.metadata.get("arxiv_id", "")), re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _lookup_requested_article(
    identifier: str,
    exact: dict[str, Article],
    by_base: dict[str, Article],
) -> Article | None:
    article = exact.get(identifier)
    if article is None and not re.search(r"v\d+$", identifier, re.IGNORECASE):
        article = by_base.get(arxiv_base_id(identifier))
    return article


def import_manual_arxiv_file(
    root: Path,
    path: Path,
    *,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Import a mixed manual txt: activate papers and archive Blog pages."""

    root = root.resolve()
    ensure_runtime_dirs(root)
    path = _validate_manual_path(root, path)
    run_date = run_date or date.today().isoformat()
    manual_entries = read_manual_entries(path)
    requested = [entry.value for entry in manual_entries if entry.kind == "arxiv"]
    requested_blogs = [entry.value for entry in manual_entries if entry.kind == "blog"]
    storage = ArticleStorage(root)
    fetcher = _arxiv_fetcher(root) if requested else None
    logger = setup_logging(root)
    active_exact, active_base, cached_exact, cached_base = _stored_arxiv_maps(storage)
    active_by_request: dict[str, Article] = {}
    cached_by_request: dict[str, Article] = {}
    missing_requests: list[str] = []

    for identifier in requested:
        active = _lookup_requested_article(identifier, active_exact, active_base)
        if active is not None:
            active_by_request[identifier] = active
            continue
        cached = _lookup_requested_article(identifier, cached_exact, cached_base)
        if cached is not None:
            cached_by_request[identifier] = cached
        else:
            missing_requests.append(identifier)

    fetch_error = ""
    try:
        fetched = fetcher.fetch_by_ids(missing_requests) if missing_requests and fetcher else []
    except Exception as exc:
        logger.exception(
            "stage=manual_fetch status=ERROR source_file=%s error=%s", path, exc
        )
        fetched = []
        fetch_error = str(exc)
    exact, by_base = _map_articles_by_request(fetched)
    entries: list[dict[str, Any]] = []
    stored = 0
    already_active = 0
    restored_from_cache = 0
    errors = 0
    imported_ids: list[str] = []
    handled_article_ids: set[str] = set()

    for identifier in requested:
        if identifier in active_by_request:
            article = active_by_request[identifier]
            entries.append(
                {
                    "requested_arxiv_id": identifier,
                    "article_id": article.id,
                    "title": article.title,
                    "status": "already_active",
                }
            )
            already_active += 1
            continue
        article = cached_by_request.get(identifier)
        from_cache = article is not None
        if article is None:
            article = _lookup_requested_article(identifier, exact, by_base)
        if article is None:
            entries.append(
                {
                    "requested_arxiv_id": identifier,
                    "status": "fetch_error",
                    "error": fetch_error or "arXiv 未返回该编号",
                }
            )
            errors += 1
            continue

        if article.id in handled_article_ids:
            entries.append(
                {
                    "requested_arxiv_id": identifier,
                    "article_id": article.id,
                    "title": article.title,
                    "status": "already_active",
                    "reason": "列表中的另一行指向同一 Article",
                }
            )
            already_active += 1
            continue
        handled_article_ids.add(article.id)

        article.metadata["download_date"] = run_date
        article.metadata["selection_strategy"] = "manual"
        article.metadata["manual_source_file"] = relative_to_root(path, root)
        article.metadata["manual_requested_arxiv_id"] = identifier
        storage.cache(article)
        entry = {
            "requested_arxiv_id": identifier,
            "article_id": article.id,
            "title": article.title,
            "status": "cached",
        }
        try:
            if fetcher is None:
                raise RuntimeError("arXiv fetcher 未初始化")
            fetcher.prepare(article)
            storage.cache(article)
            storage.save(article, rebuild_index=False)
            entry["status"] = "saved"
            entry["saved_at"] = utc_now_iso()
            stored += 1
            imported_ids.append(article.id)
            if from_cache:
                restored_from_cache += 1
        except Exception as exc:
            logger.exception(
                "stage=manual_import status=ERROR article=%s error=%s", article.id, exc
            )
            entry["status"] = "prepare_error"
            entry["error"] = str(exc)
            errors += 1
        entries.append(entry)

    blog_store = BlogSiteStore(root)
    blogs_archived = 0
    blogs_catalog_only = 0
    blog_sites_added = 0
    for blog_url in requested_blogs:
        blog_entry: dict[str, Any] = {
            "requested_blog_url": blog_url,
            "status": "blog_site_saved",
        }
        try:
            site, added = blog_store.add(blog_url)
            blog_entry["blog_origin"] = site.origin
            blog_entry["blog_site_added"] = added
            blog_sites_added += int(added)
            archive_mode = blog_archive_mode(blog_url)
            blog_entry["archive_mode"] = archive_mode
            if archive_mode == "catalog_only":
                blog_entry["status"] = "blog_catalog_only"
                blog_entry["reason"] = "GitHub/Hugging Face 链接按策略仅登记主站，不下载网页"
                blogs_catalog_only += 1
            else:
                archived = archive_blog(root, blog_url)
                blog_entry["status"] = "blog_archived"
                blog_entry["snapshot"] = relative_to_root(archived.output_path, root)
                blog_entry["resources_embedded"] = archived.resources_embedded
                blog_entry["optional_resources_removed"] = archived.optional_resources_removed
                blogs_archived += 1
        except Exception as exc:
            logger.exception(
                "stage=manual_blog_archive status=ERROR url=%s error=%s", blog_url, exc
            )
            blog_entry["status"] = "blog_archive_error"
            blog_entry["error"] = str(exc)
            errors += 1
        entries.append(blog_entry)

    storage.rebuild_index()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem)[:80] or "manual"
    report_payload = {
        "version": 2,
        "date": run_date,
        "updated_at": utc_now_iso(),
        "source_file": relative_to_root(path, root),
        "requested_count": len(manual_entries),
        "requested_arxiv_count": len(requested),
        "requested_blog_count": len(requested_blogs),
        "stored_count": stored,
        "blogs_archived_count": blogs_archived,
        "blogs_catalog_only_count": blogs_catalog_only,
        "blog_sites_added_count": blog_sites_added,
        "already_active_count": already_active,
        "restored_from_cache_count": restored_from_cache,
        "errors": errors,
        "articles": entries,
    }
    report_path = _write_json(
        root / "data" / "manual" / f"{timestamp}_{safe_stem}.json", report_payload
    )
    return {
        "requested": len(manual_entries),
        "requested_arxiv": len(requested),
        "requested_blogs": len(requested_blogs),
        "stored": stored,
        "blogs_archived": blogs_archived,
        "blogs_catalog_only": blogs_catalog_only,
        "blog_sites_added": blog_sites_added,
        "already_active": already_active,
        "restored_from_cache": restored_from_cache,
        "errors": errors,
        "article_ids": imported_ids,
        "report": relative_to_root(report_path, root),
    }


def _priority_overflow_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    raw_rank = (item.get("priority") or {}).get("rank")
    try:
        rank = int(raw_rank)
    except (TypeError, ValueError):
        rank = 10**12
    return rank, str(item.get("article_id", ""))


def add_overflow_batch(
    root: Path = PROJECT_ROOT,
    *,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Activate the next strategy-ordered batch beyond the daily limit."""

    root = root.resolve()
    run_date = run_date or date.today().isoformat()
    state = load_daily_state(root, run_date)
    strategy = str(state.get("strategy") or "")
    if strategy not in {"priority", "fixed"} or not state.get("steps", {}).get(
        "strategy_applied"
    ):
        raise ValueError("今天尚未执行优选或固定策略，不能超量加入")
    report_path = selection_report_path(root, run_date)
    if not report_path.is_file():
        raise FileNotFoundError("今日策略报告不存在，不能超量加入")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("invalidated"):
        raise ValueError("今日策略报告已失效，请重新执行策略")
    candidates = {article.id: article for article in load_candidate_snapshot(root, run_date)}
    storage = ArticleStorage(root)
    pending_statuses = {"not_selected", "selected", "save_error"}
    pool = [
        item
        for item in report.get("articles", [])
        if str(item.get("status", "")) in pending_statuses
    ]
    if strategy == "priority":
        pool.sort(key=_priority_overflow_sort_key)
    else:
        pool.sort(
            key=lambda item: (
                candidates.get(str(item.get("article_id", ""))).publish_date
                if candidates.get(str(item.get("article_id", "")))
                else str(item.get("publish_date", "")),
                candidates.get(str(item.get("article_id", ""))).fetch_date
                if candidates.get(str(item.get("article_id", "")))
                else "",
                str(item.get("article_id", "")),
            ),
            reverse=True,
        )
    daily_limit = load_settings(root).daily_article_limit
    eligible_pool: list[dict[str, Any]] = []
    skipped_existing = 0
    for entry in pool:
        article_id = str(entry.get("article_id", ""))
        if storage.load_active(article_id) is not None:
            entry["status"] = "existing"
            entry["overflow_checked_at"] = utc_now_iso()
            skipped_existing += 1
        else:
            eligible_pool.append(entry)
    selected_entries = eligible_pool[:daily_limit]
    fetchers = {
        source.type: registry.create(source, root)
        for source in load_config(root).sources
        if source.enabled
    }
    logger = setup_logging(root)
    stored = 0
    errors = 0
    selected_ids: list[str] = []
    batch_entries: list[dict[str, Any]] = []

    for entry in selected_entries:
        article_id = str(entry.get("article_id", ""))
        article = storage.load_cached(article_id) or candidates.get(article_id)
        if article is None:
            entry["status"] = "save_error"
            entry["error"] = "候选快照与 Article 缓存均不存在"
            errors += 1
            batch_entries.append({"article_id": article_id, "status": "save_error"})
            continue
        fetcher = fetchers.get(article.source)
        if fetcher is None:
            entry["status"] = "save_error"
            entry["error"] = f"没有启用的 Fetcher: {article.source}"
            errors += 1
            batch_entries.append({"article_id": article_id, "status": "save_error"})
            continue

        article.metadata["download_date"] = run_date
        article.metadata["selection_strategy"] = strategy
        article.metadata["overflow_added_at"] = utc_now_iso()
        if strategy == "priority":
            article.metadata["priority"] = dict(entry.get("priority") or {})
        storage.cache(article)
        try:
            fetcher.prepare(article)
            storage.cache(article)
            storage.save(article, rebuild_index=False)
            entry["status"] = "saved"
            entry["saved_at"] = utc_now_iso()
            entry["added_via"] = "overflow"
            entry.pop("error", None)
            stored += 1
            selected_ids.append(article.id)
            batch_entries.append({"article_id": article.id, "status": "saved"})
        except Exception as exc:
            logger.exception(
                "stage=overflow_add status=ERROR article=%s error=%s", article.id, exc
            )
            entry["status"] = "save_error"
            entry["error"] = str(exc)
            errors += 1
            batch_entries.append(
                {"article_id": article.id, "status": "save_error", "error": str(exc)}
            )

    storage.rebuild_index()
    batch = {
        "added_at": utc_now_iso(),
        "daily_limit": daily_limit,
        "selected_count": len(selected_entries),
        "stored_count": stored,
        "errors": errors,
        "article_ids": selected_ids,
        "articles": batch_entries,
    }
    report.setdefault("overflow_batches", []).append(batch)
    report["selected_count"] = sum(
        str(item.get("status", "")) in {"selected", "save_error", "saved"}
        for item in report.get("articles", [])
    )
    report["stored_count"] = sum(
        str(item.get("status", "")) == "saved"
        for item in report.get("articles", [])
    )
    report["errors"] = int(report.get("errors", 0)) + errors
    report["updated_at"] = utc_now_iso()
    _write_json(report_path, report)
    state["counts"]["selected"] = int(report.get("selected_count", 0))
    state["counts"]["stored"] = int(report.get("stored_count", 0))
    state["counts"]["overflow_batches"] = len(report["overflow_batches"])
    state["counts"]["overflow_stored"] = sum(
        int(item.get("stored_count", 0)) for item in report["overflow_batches"]
    )
    save_daily_state(state, root)
    return {
        "strategy": strategy,
        "daily_limit": daily_limit,
        "selected": len(selected_entries),
        "stored": stored,
        "errors": errors,
        "skipped_existing": skipped_existing,
        "remaining": sum(
            str(item.get("status", "")) in pending_statuses
            for item in report.get("articles", [])
        ),
        "article_ids": selected_ids,
        "report": relative_to_root(report_path, root),
    }


def _update_reports_after_defer(root: Path, article_ids: list[str], now: str) -> None:
    remaining = set(article_ids)
    report_paths = sorted((root / "data" / "selection").glob("*.json"), reverse=True)
    for report_path in report_paths:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        changed = False
        matching: dict[str, dict[str, Any]] = {}
        for item in report.get("articles", []):
            article_id = str(item.get("article_id", ""))
            if article_id not in remaining or item.get("status") not in {
                "saved",
                "existing",
            }:
                continue
            matching[article_id] = item

        tail_rank = 0
        if report.get("strategy") == "priority":
            for item in report.get("articles", []):
                raw_rank = (item.get("priority") or {}).get("rank")
                try:
                    tail_rank = max(tail_rank, int(raw_rank))
                except (TypeError, ValueError):
                    continue

        for article_id in article_ids:
            item = matching.get(article_id)
            if item is None:
                continue
            previous_status = str(item.get("status", ""))
            item["status"] = "not_selected"
            item["deferred_at"] = now
            item["deferred_reason"] = "manual"
            if report.get("strategy") == "priority":
                priority = dict(item.get("priority") or {})
                previous_rank = priority.get("rank")
                tail_rank += 1
                priority["rank"] = tail_rank
                item["priority"] = priority
                item["priority_rank_before_defer"] = previous_rank
                item["priority_moved_to_tail_at"] = now
            if previous_status == "saved":
                report["stored_count"] = max(0, int(report.get("stored_count", 0)) - 1)
                report["selected_count"] = max(
                    0, int(report.get("selected_count", 0)) - 1
                )
            remaining.remove(article_id)
            changed = True
        if changed:
            report["updated_at"] = now
            _write_json(report_path, report)
        if not remaining:
            break

    manual_paths = sorted((root / "data" / "manual").glob("*.json"), reverse=True)
    for report_path in manual_paths:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        changed = False
        for item in report.get("articles", []):
            if str(item.get("article_id", "")) not in article_ids:
                continue
            if item.get("status") not in {"saved", "already_active"}:
                continue
            item["status"] = "cached"
            item["deferred_at"] = now
            changed = True
        if changed:
            report["updated_at"] = now
            _write_json(report_path, report)


def defer_pending_articles(
    root: Path,
    article_ids: list[str],
    *,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Atomically validate, then move selected pending Articles back to cache."""

    root = root.resolve()
    unique_ids = list(dict.fromkeys(article_ids))
    if not unique_ids:
        raise ValueError("没有选择要移出待解读状态的文章")
    storage = ArticleStorage(root)
    for article_id in unique_ids:
        article = storage.load_active(article_id)
        html_path = root / "attachments" / "html" / f"{article_id}.html"
        if article is None:
            raise FileNotFoundError(f"待解读 Article 不存在: {article_id}")
        if article.status.processed or article.html or html_path.exists():
            raise ValueError(f"文章不是待解读状态: {article_id}")

    deferred: list[str] = []
    for article_id in unique_ids:
        storage.defer(article_id, rebuild_index=False)
        deferred.append(article_id)
    storage.rebuild_index()
    now = utc_now_iso()
    _update_reports_after_defer(root, deferred, now)
    state = load_daily_state(root, run_date)
    current_report_path = selection_report_path(root, str(state.get("date", "")))
    if current_report_path.is_file():
        try:
            current_report = json.loads(current_report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current_report = {}
        if current_report:
            state["counts"]["selected"] = int(current_report.get("selected_count", 0))
            state["counts"]["stored"] = int(current_report.get("stored_count", 0))
            save_daily_state(state, root)
    return {
        "deferred": len(deferred),
        "article_ids": deferred,
        "cache_paths": [
            relative_to_root(storage.article_path(article_id, "cache"), root)
            for article_id in deferred
        ],
    }
