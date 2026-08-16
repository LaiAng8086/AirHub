"""Staged daily workflow used by the integrated console."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .config import load_config, load_settings
from .daily_state import load_daily_state, mark_step, save_daily_state
from .fetchers import registry
from .filters import FilterEngine
from .html.assets import embed_local_images
from .models import Article, utc_now_iso
from .paths import PROJECT_ROOT, ensure_runtime_dirs
from .priority import PriorityProfile
from .priority_feedback import PriorityFeedbackStore
from .producer import _record_filter_result, _set_filter_metadata, setup_logging
from .storage import ArticleStorage


DISCOVERY_LIMIT = 300
NON_EMBEDDED_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc=[\"'](?!data:)([^\"']+)[\"']",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DiscoveryResult:
    path: Path
    articles: list[Article]
    fetched: int
    cached: bool
    errors: int = 0


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def candidate_snapshot_path(root: Path, run_date: str) -> Path:
    return root / "data" / "candidates" / f"{run_date}.json"


def selection_report_path(root: Path, run_date: str) -> Path:
    return root / "data" / "selection" / f"{run_date}.json"


def _candidate_snapshot_is_complete(
    payload: Any,
    *,
    run_date: str,
    limit: int | None = None,
) -> bool:
    if not isinstance(payload, dict) or payload.get("date") != run_date:
        return False
    if limit is not None and int(payload.get("limit", -1)) != limit:
        return False
    if int(payload.get("errors", 0)) != 0 or payload.get("complete") is False:
        return False
    return isinstance(payload.get("articles"), list)


def _repair_empty_strategy_lock(root: Path, run_date: str, candidate_count: int) -> None:
    """Unlock a no-op strategy run that consumed an earlier failed empty snapshot."""

    if candidate_count <= 0:
        return
    state = load_daily_state(root, run_date)
    if not state.get("steps", {}).get("strategy_applied"):
        return
    report_path = selection_report_path(root, run_date)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    empty_run = (
        int(report.get("candidate_count", -1)) == 0
        and int(report.get("selected_count", -1)) == 0
        and int(report.get("stored_count", -1)) == 0
        and not report.get("articles")
    )
    if not empty_run:
        return
    report["invalidated"] = True
    report["invalidated_reason"] = "empty strategy run used an incomplete candidate snapshot"
    report["invalidated_at"] = utc_now_iso()
    _write_json(report_path, report)
    state["strategy"] = None
    state["steps"]["strategy_applied"] = False
    save_daily_state(state, root)


def update_priority_strategy(
    root: Path = PROJECT_ROOT,
    run_date: str | None = None,
) -> PriorityProfile:
    """Rebuild from the newest scope CSV, then merge persistent user feedback."""

    root = root.resolve()
    run_date = run_date or date.today().isoformat()
    config = load_config(root)
    profile = PriorityProfile.from_latest_csv(
        root,
        known_institutions=config.institution_country.keys(),
    )
    feedback_store = PriorityFeedbackStore(root)
    feedback_authors, feedback_institutions = feedback_store.counts()
    feedback_store.apply_to(profile)
    payload = {
        "version": 1,
        "updated_at": utc_now_iso(),
        "scope_csv": profile.csv_path.name,
        "scope_rows": profile.row_count,
        "authors": dict(profile.author_counts.most_common()),
        "institutions": dict(profile.institution_counts.most_common()),
        "feedback_articles": len(feedback_store.load().get("articles", [])),
        "feedback_authors": dict(feedback_authors.most_common()),
        "feedback_institutions": dict(feedback_institutions.most_common()),
    }
    _write_json(root / "data" / "priority" / "profile.json", payload)
    mark_step(
        "priority_updated",
        root,
        run_date=run_date,
        counts={
            "profile_authors": len(profile.author_counts),
            "profile_institutions": len(profile.institution_counts),
        },
    )
    return profile


def format_priority_frequencies(profile: PriorityProfile) -> str:
    lines = [
        f"最新 CSV：{profile.csv_path.name}（{profile.row_count} 篇历史论文）",
        f"作者频度（{len(profile.author_counts)} 人）：",
    ]
    lines.extend(f"  {name}: {count}" for name, count in profile.author_counts.most_common())
    lines.append(f"机构频度（{len(profile.institution_counts)} 个）：")
    lines.extend(
        f"  {name}: {count}" for name, count in profile.institution_counts.most_common()
    )
    return "\n".join(lines)


def load_priority_strategy(root: Path = PROJECT_ROOT) -> PriorityProfile:
    path = root / "data" / "priority" / "profile.json"
    if not path.exists():
        raise FileNotFoundError("优选策略尚未更新，请先执行菜单 2")
    payload = json.loads(path.read_text(encoding="utf-8"))
    csv_name = str(payload.get("scope_csv", ""))
    if not csv_name:
        raise ValueError("优选策略文件缺少 scope_csv")
    return PriorityProfile(
        csv_path=root / "scope" / csv_name,
        row_count=int(payload.get("scope_rows", 0)),
        author_counts=Counter(
            {str(key): int(value) for key, value in payload.get("authors", {}).items()}
        ),
        institution_counts=Counter(
            {
                str(key): int(value)
                for key, value in payload.get("institutions", {}).items()
            }
        ),
    )


def discover_recent_candidates(
    root: Path = PROJECT_ROOT,
    *,
    limit: int = DISCOVERY_LIMIT,
    run_date: str | None = None,
    force: bool = False,
) -> DiscoveryResult:
    """Fetch only basic metadata and reuse the same day's snapshot."""

    root = root.resolve()
    run_date = run_date or date.today().isoformat()
    ensure_runtime_dirs(root)
    path = candidate_snapshot_path(root, run_date)
    if path.exists() and not force:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if _candidate_snapshot_is_complete(
                payload, run_date=run_date, limit=limit
            ):
                articles = [
                    Article.from_dict(item) for item in payload.get("articles", [])
                ]
                mark_step(
                    "discovered",
                    root,
                    run_date=run_date,
                    counts={
                        "candidates": len(articles),
                        "discovery_cache_hit": True,
                        "discovery_errors": 0,
                    },
                )
                _repair_empty_strategy_lock(root, run_date, len(articles))
                return DiscoveryResult(path, articles, 0, True, 0)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    config = load_config(root)
    logger = setup_logging(root)
    articles_by_id: dict[str, Article] = {}
    errors = 0
    for source in config.sources:
        if not source.enabled or len(articles_by_id) >= limit:
            continue
        remaining = limit - len(articles_by_id)
        try:
            fetcher = registry.create(source, root)
            fetched = fetcher.fetch(limit=remaining)
        except Exception as exc:
            logger.exception(
                "stage=discovery status=ERROR source=%s error=%s", source.name, exc
            )
            errors += 1
            continue
        for article in fetched:
            if len(articles_by_id) >= limit:
                break
            article.metadata["discovery_date"] = run_date
            articles_by_id.setdefault(article.id, article)
    articles = list(articles_by_id.values())
    payload = {
        "version": 2,
        "date": run_date,
        "limit": limit,
        "updated_at": utc_now_iso(),
        "complete": errors == 0,
        "errors": errors,
        "articles": [article.to_dict() for article in articles],
    }
    _write_json(path, payload)
    if errors:
        mark_step(
            "discovered",
            root,
            done=False,
            run_date=run_date,
            counts={
                "candidates": len(articles),
                "discovery_cache_hit": False,
                "discovery_errors": errors,
            },
        )
        raise RuntimeError(
            f"候选抓取未完整成功（错误 {errors}，已获取 {len(articles)} 篇），"
            "失败快照不会被复用，请重新执行菜单 2"
        )
    mark_step(
        "discovered",
        root,
        run_date=run_date,
        counts={
            "candidates": len(articles),
            "discovery_cache_hit": False,
            "discovery_errors": 0,
        },
    )
    _repair_empty_strategy_lock(root, run_date, len(articles))
    return DiscoveryResult(path, articles, len(articles), False, errors)


def load_candidate_snapshot(root: Path, run_date: str) -> list[Article]:
    path = candidate_snapshot_path(root, run_date)
    if not path.exists():
        raise FileNotFoundError("尚未抓取今日最近 300 篇基本信息，请先执行菜单 2")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not _candidate_snapshot_is_complete(payload, run_date=run_date):
        raise ValueError("今日候选快照抓取不完整，请重新执行菜单 2")
    return [Article.from_dict(item) for item in payload.get("articles", [])]


def _fetchers_by_type(root: Path) -> dict[str, Any]:
    config = load_config(root)
    return {
        source.type: registry.create(source, root)
        for source in config.sources
        if source.enabled
    }


def _processed_ids_on_date(root: Path, run_date: str) -> set[str]:
    ids: set[str] = set()
    report = root / "data" / "priority" / f"{run_date}.json"
    if report.exists():
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        ids.update(
            str(item.get("article_id"))
            for item in payload.get("selection_history", [])
            if item.get("article_id")
        )
    for queue in ("metadata/articles", "filtered"):
        for path in (root / queue).glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(payload.get("metadata", {}).get("download_date", "")) == run_date:
                ids.add(str(payload.get("id", path.stem)))
    return ids


def apply_daily_strategy(
    mode: str,
    root: Path = PROJECT_ROOT,
    *,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Apply exactly one daily strategy, then prepare only the selected originals."""

    if mode not in {"priority", "fixed"}:
        raise ValueError("策略必须是 priority 或 fixed")
    root = root.resolve()
    run_date = run_date or date.today().isoformat()
    state = load_daily_state(root, run_date)
    chosen = state.get("strategy")
    if chosen and chosen != mode:
        names = {"priority": "优选策略", "fixed": "固定策略"}
        raise ValueError(
            f"今天已执行{names.get(str(chosen), str(chosen))}，不能再切换到{names[mode]}"
        )
    if chosen == mode and state.get("steps", {}).get("strategy_applied"):
        return {"mode": mode, "skipped": True, "reason": "今天已执行该策略"}

    candidates = load_candidate_snapshot(root, run_date)
    config = load_config(root)
    logger = setup_logging(root)
    storage = ArticleStorage(root)
    filter_engine = FilterEngine(config.filters, config.institution_country)
    fetchers = _fetchers_by_type(root)
    existing_ids = {
        article.id for article in storage.iter_active()
    } | {path.stem for path in (root / "filtered").glob("*.json")}
    processed_today = _processed_ids_on_date(root, run_date)
    daily_limit = load_settings(root).daily_article_limit
    remaining = max(0, daily_limit - len(processed_today))
    entries: dict[str, dict[str, Any]] = {}
    eligible: list[Article] = []
    errors = 0

    for article in candidates:
        entry = {
            "article_id": article.id,
            "title": article.title,
            "publish_date": article.publish_date,
            "date": (article.publish_date or run_date)[:10],
            "status": "candidate",
        }
        entries[article.id] = entry
        if article.id in existing_ids:
            entry["status"] = "existing"
            continue
        article.metadata["download_date"] = run_date
        fetcher = fetchers.get(article.source)
        if fetcher is None:
            entry["status"] = "strategy_error"
            entry["error"] = f"没有启用的 Fetcher: {article.source}"
            errors += 1
            continue
        try:
            fetcher.prepare_for_priority(article)
        except Exception as exc:
            logger.warning(
                "stage=strategy_evidence status=WARN mode=%s article=%s error=%s",
                mode,
                article.id,
                exc,
            )
            errors += 1

        if mode == "fixed":
            result = filter_engine.apply(article)
            _set_filter_metadata(article, result)
            _record_filter_result(logger, article, result)
            if not result.included:
                entry["status"] = "filtered"
                entry["reasons"] = result.reasons
                continue
        else:
            filter_engine.enrich(article)
        eligible.append(article)

    if mode == "priority":
        profile = load_priority_strategy(root)
        eligible = profile.rank(eligible)
    else:
        eligible.sort(
            key=lambda article: (article.publish_date, article.fetch_date, article.id),
            reverse=True,
        )

    selected = eligible[:remaining]
    selected_ids = {article.id for article in selected}
    for article in eligible:
        entry = entries[article.id]
        if mode == "priority":
            entry["priority"] = dict(article.metadata.get("priority", {}))
        entry["status"] = "selected" if article.id in selected_ids else "not_selected"

    stored = 0
    save_errors = 0
    for article in selected:
        entry = entries[article.id]
        fetcher = fetchers[article.source]
        try:
            article.metadata["selection_strategy"] = mode
            fetcher.prepare(article)
            storage.save(article, rebuild_index=False)
            entry["status"] = "saved"
            entry["saved_at"] = utc_now_iso()
            stored += 1
        except Exception as exc:
            logger.exception(
                "stage=save_original status=ERROR mode=%s article=%s error=%s",
                mode,
                article.id,
                exc,
            )
            entry["status"] = "save_error"
            entry["error"] = str(exc)
            errors += 1
            save_errors += 1
    storage.rebuild_index()

    report_payload = {
        "version": 1,
        "date": run_date,
        "strategy": mode,
        "updated_at": utc_now_iso(),
        "candidate_count": len(candidates),
        "eligible_count": len(eligible),
        "previously_processed_today": len(processed_today),
        "daily_limit": daily_limit,
        "selected_count": len(selected),
        "stored_count": stored,
        "errors": errors,
        "articles": list(entries.values()),
    }
    report_path = _write_json(selection_report_path(root, run_date), report_payload)
    mark_step(
        "strategy_applied",
        root,
        strategy=mode,
        run_date=run_date,
        counts={
            "eligible": len(eligible),
            "selected": len(selected),
            "stored": stored,
            "save_errors": save_errors,
        },
    )
    return {
        "mode": mode,
        "skipped": False,
        "candidates": len(candidates),
        "existing": sum(entry.get("status") == "existing" for entry in entries.values()),
        "eligible": len(eligible),
        "selected": len(selected),
        "not_selected": max(0, len(eligible) - len(selected)),
        "stored": stored,
        "remaining_before_run": remaining,
        "errors": errors,
        "report": report_path.relative_to(root).as_posix(),
    }


def _article_date(payload: dict[str, Any], fallback: str = "") -> str:
    metadata = payload.get("metadata", {}) or {}
    return str(
        metadata.get("download_date")
        or payload.get("publish_date")
        or payload.get("fetch_date")
        or fallback
    )[:10]


def html_needs_embedding(path: Path) -> bool:
    try:
        return NON_EMBEDDED_IMAGE_RE.search(path.read_text(encoding="utf-8")) is not None
    except OSError:
        return False


def collect_article_statuses(root: Path = PROJECT_ROOT) -> dict[str, list[dict[str, str]]]:
    """Return every known article grouped by its next required action."""

    root = root.resolve()
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    known_ids: set[str] = set()
    metadata_dir = root / "metadata" / "articles"
    for path in metadata_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        article_id = str(payload.get("id", path.stem))
        known_ids.add(article_id)
        html_path = root / "attachments" / "html" / f"{article_id}.html"
        if not html_path.exists():
            status = "待解读"
        elif html_needs_embedding(html_path):
            status = "待嵌入"
        else:
            status = "已完成"
        groups[status].append(
            {
                "id": article_id,
                "title": str(payload.get("title", "")),
                "date": _article_date(payload),
            }
        )

    for report_path in sorted((root / "data" / "selection").glob("*.json"), reverse=True):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in report.get("articles", []):
            article_id = str(item.get("article_id", ""))
            if not article_id or article_id in known_ids:
                continue
            raw_status = str(item.get("status", ""))
            if raw_status in {"selected", "save_error", "strategy_error"}:
                status = "待保存"
            elif raw_status == "filtered":
                status = "固定策略未入选"
            elif raw_status == "not_selected":
                status = (
                    "优选排名未进入每日上限"
                    if report.get("strategy") == "priority"
                    else "固定筛选通过但未进入每日上限"
                )
            else:
                status = "已存在/已处理"
            groups[status].append(
                {
                    "id": article_id,
                    "title": str(item.get("title", "")),
                    "date": str(item.get("date") or report.get("date", ""))[:10],
                }
            )
            known_ids.add(article_id)

    for path in sorted((root / "data" / "article_cache").glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        article_id = str(payload.get("id", path.stem))
        if not article_id or article_id in known_ids:
            continue
        groups["缓存"].append(
            {
                "id": article_id,
                "title": str(payload.get("title", "")),
                "date": _article_date(payload),
            }
        )
        known_ids.add(article_id)

    order = (
        "待保存",
        "待解读",
        "待嵌入",
        "已完成",
        "缓存",
        "固定策略未入选",
        "优选排名未进入每日上限",
        "固定筛选通过但未进入每日上限",
        "已存在/已处理",
    )
    return {
        name: sorted(
            groups.get(name, []),
            key=lambda item: (item.get("date", ""), item.get("id", "")),
            reverse=True,
        )
        for name in order
    }


def format_article_statuses(groups: dict[str, list[dict[str, str]]]) -> str:
    lines: list[str] = []
    for status, items in groups.items():
        lines.append(f"【{status}】{len(items)} 篇")
        current_date = None
        for item in items:
            item_date = item.get("date") or "日期未知"
            if item_date != current_date:
                current_date = item_date
                lines.append(f"  {current_date}")
            lines.append(f"    - {item['id']} | {item.get('title', '')}")
        if not items:
            lines.append("  （无）")
    return "\n".join(lines)


def refresh_completion_state(
    root: Path = PROJECT_ROOT,
    run_date: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    run_date = run_date or date.today().isoformat()
    today_ids: list[str] = []
    for path in (root / "metadata" / "articles").glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _article_date(payload) == run_date:
            today_ids.append(str(payload.get("id", path.stem)))
    html_paths = [root / "attachments" / "html" / f"{article_id}.html" for article_id in today_ids]
    digested = bool(html_paths) and all(path.exists() for path in html_paths)
    embedded = digested and all(not html_needs_embedding(path) for path in html_paths)
    state = load_daily_state(root, run_date)
    state["steps"]["digested"] = digested
    state["steps"]["embedded"] = embedded
    state["counts"]["today_articles"] = len(today_ids)
    state["counts"]["pending_digest"] = sum(not path.exists() for path in html_paths)
    state["counts"]["pending_embed"] = sum(
        path.exists() and html_needs_embedding(path) for path in html_paths
    )
    save_daily_state(state, root)
    return state


def embed_pending_html(root: Path = PROJECT_ROOT) -> dict[str, int]:
    root = root.resolve()
    config = load_config(root)
    embed_config = config.html.get("asset_embed", {}) or {}
    stats = {"files": 0, "embedded": 0, "missing": 0, "skipped": 0}
    for path in sorted((root / "attachments" / "html").glob("*.html")):
        if not html_needs_embedding(path):
            continue
        result = embed_local_images(
            path,
            base_dir=root,
            quality=int(embed_config.get("quality", 84)),
            max_width=int(embed_config.get("max_width", 980)),
        )
        stats["files"] += 1
        for key in ("embedded", "missing", "skipped"):
            stats[key] += int(result.get(key, 0))
    refresh_completion_state(root)
    return stats


def clear_runtime_cache(root: Path = PROJECT_ROOT) -> dict[str, int]:
    """Clear only reproducible caches, never Article queues or attachments."""

    root = root.resolve()
    targets = (root / "cache", root / "data" / "candidates")
    files = 0
    bytes_removed = 0
    for target in targets:
        if not target.exists():
            continue
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                try:
                    bytes_removed += path.stat().st_size
                except OSError:
                    pass
                path.unlink(missing_ok=True)
                files += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        target.mkdir(parents=True, exist_ok=True)
    state = load_daily_state(root)
    state["steps"]["discovered"] = False
    state["counts"].pop("candidates", None)
    state["counts"].pop("discovery_cache_hit", None)
    save_daily_state(state, root)
    return {"files": files, "bytes": bytes_removed}
