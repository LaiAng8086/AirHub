"""Producer orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

from .config import load_config, load_settings
from .fetchers import registry
from .filters import FilterEngine, FilterResult
from .logging_utils import get_producer_logger, setup_producer_logging
from .media.pdf_text import extract_pdf_first_page_text
from .models import Article, utc_now_iso
from .paths import PROJECT_ROOT, ensure_runtime_dirs
from .priority import PriorityProfile
from .storage import ArticleStorage


def emit_progress(level: str, message: str) -> None:
    logger = get_producer_logger()
    if level in {"WARN", "ERROR"}:
        logger.warning(message)
    else:
        logger.info(message)


def setup_logging(root: Path) -> logging.Logger:
    return setup_producer_logging(root)


def _short_values(values: list[str], limit: int = 8) -> str:
    shortened = []
    for value in values[:limit]:
        normalized = str(value).replace("\n", " ")
        shortened.append(normalized if len(normalized) <= 120 else normalized[:117] + "...")
    suffix = f" (+{len(values) - limit} more)" if len(values) > limit else ""
    return repr(shortened) + suffix


def _record_filter_result(
    logger: logging.Logger,
    article: Article,
    result: FilterResult,
) -> None:
    for evaluation in result.evaluations:
        logger.info(
            "stage=filter_check status=DONE article=%s field=%s outcome=%s "
            "configured=%s actual=%s matched=%s",
            article.id,
            evaluation.field,
            evaluation.outcome,
            _short_values(evaluation.configured),
            _short_values(evaluation.actual),
            _short_values(evaluation.matched),
        )
    logger.info(
        "stage=filter status=%s article=%s affiliation_source=%s institutions=%s "
        "countries=%s reasons=%s",
        "INCLUDED" if result.included else "EXCLUDED",
        article.id,
        article.metadata.get("affiliation_source") or "none",
        _short_values(article.metadata.get("institutions", [])),
        _short_values(article.metadata.get("countries", [])),
        _short_values(result.reasons),
    )


def _set_filter_metadata(article: Article, result: FilterResult) -> None:
    article.metadata["filter_trace"] = result.reasons
    article.metadata["filter_audit"] = [
        evaluation.to_dict() for evaluation in result.evaluations
    ]


def _local_pdf_path(article: Article, root: Path) -> Path | None:
    for attachment in article.attachments:
        if attachment.type != "pdf" or not attachment.path:
            continue
        candidate = (root / attachment.path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.exists():
            return candidate
    return None


def _refresh_legacy_affiliation(
    article: Article,
    root: Path,
    logger: logging.Logger,
) -> None:
    metadata = article.metadata
    source_format = str(metadata.get("affiliation_source_format", ""))
    source_text = str(metadata.get("affiliation_source_text", "")).strip()
    if source_format == "structured" and source_text:
        logger.info(
            "stage=refilter_evidence status=REUSED article=%s source=arxiv_html chars=%d",
            article.id,
            len(source_text),
        )
        return

    pdf_path = _local_pdf_path(article, root)
    if pdf_path is None:
        metadata["affiliation_source"] = ""
        metadata["affiliation_source_format"] = ""
        metadata["affiliation_source_text"] = ""
        logger.warning(
            "stage=refilter_evidence status=WARN article=%s local_pdf=false source=none",
            article.id,
        )
        return

    try:
        text = extract_pdf_first_page_text(pdf_path)
    except Exception as exc:
        metadata["affiliation_source"] = ""
        metadata["affiliation_source_format"] = ""
        metadata["affiliation_source_text"] = ""
        logger.warning(
            "stage=refilter_evidence status=WARN article=%s pdf=%s error=%s",
            article.id,
            pdf_path.relative_to(root),
            exc,
        )
        return

    metadata["affiliation_source"] = "pdf_first_page" if text else ""
    metadata["affiliation_source_format"] = "first_page" if text else ""
    metadata["affiliation_source_text"] = text
    logger.info(
        "stage=refilter_evidence status=DONE article=%s source=%s chars=%d pdf=%s",
        article.id,
        metadata["affiliation_source"] or "none",
        len(text),
        pdf_path.relative_to(root),
    )


def _run_existing_refilter(
    root: Path,
    storage: ArticleStorage,
    filter_engine: FilterEngine,
    logger: logging.Logger,
    dry_run: bool,
) -> dict[str, int]:
    stats = {
        "sources": 0,
        "fetched": 0,
        "stored": 0,
        "filtered": 0,
        "archived": 0,
        "errors": 0,
    }
    articles = storage.iter_active()
    logger.info(
        "stage=refilter status=START candidates=%d dry_run=%s", len(articles), dry_run
    )
    for index, article in enumerate(articles, start=1):
        logger.info(
            "stage=refilter_candidate status=START candidate=%d/%d article=%s",
            index,
            len(articles),
            article.id,
        )
        _refresh_legacy_affiliation(article, root, logger)
        result = filter_engine.apply(article)
        _set_filter_metadata(article, result)
        _record_filter_result(logger, article, result)
        if result.included:
            stats["stored"] += 1
            if not dry_run:
                storage.save(article, rebuild_index=False)
            logger.info(
                "stage=refilter_candidate status=KEPT article=%s dry_run=%s",
                article.id,
                dry_run,
            )
            continue

        stats["filtered"] += 1
        if not dry_run:
            storage.archive(article)
            stats["archived"] += 1
        logger.info(
            "stage=refilter_candidate status=%s article=%s reasons=%s",
            "WOULD_ARCHIVE" if dry_run else "ARCHIVED",
            article.id,
            _short_values(result.reasons),
        )

    if not dry_run:
        storage.rebuild_index()
        logger.info("stage=index status=DONE active_count=%d", stats["stored"])
    return stats


def _priority_report_path(root: Path, run_date: str) -> Path:
    return root / "data" / "priority" / f"{run_date}.json"


def _load_priority_history(root: Path, run_date: str) -> list[dict[str, Any]]:
    path = _priority_report_path(root, run_date)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    history = payload.get("selection_history", [])
    return [dict(item) for item in history if isinstance(item, dict)]


def _write_priority_report(
    root: Path,
    run_date: str,
    daily_limit: int,
    profile: PriorityProfile,
    ranked: list[Article],
    history: list[dict[str, Any]],
) -> Path:
    history_ids = {str(item.get("article_id", "")) for item in history}
    queue = []
    for article in ranked:
        priority = dict(article.metadata.get("priority", {}))
        queue.append(
            {
                "rank": priority.get("rank"),
                "article_id": article.id,
                "title": article.title,
                "authors": article.authors,
                "publish_date": article.publish_date,
                "score": priority.get("score", 0),
                "author_score": priority.get("author_score", 0),
                "institution_score": priority.get("institution_score", 0),
                "matched_authors": priority.get("matched_authors", {}),
                "matched_institutions": priority.get("matched_institutions", {}),
                "selected_today": article.id in history_ids,
            }
        )
    payload = {
        "version": 1,
        "date": run_date,
        "updated_at": utc_now_iso(),
        "scope_csv": profile.csv_path.name,
        "scope_rows": profile.row_count,
        "scope_distribution": {
            "authors": dict(profile.author_counts.most_common()),
            "institutions": dict(profile.institution_counts.most_common()),
        },
        "daily_article_limit": daily_limit,
        "selected_count": len(history_ids),
        "selection_history": history,
        "queue": queue,
    }
    path = _priority_report_path(root, run_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _run_prioritized_ingest(
    root: Path,
    config: Any,
    storage: ArticleStorage,
    filter_engine: FilterEngine,
    logger: logging.Logger,
    limit: int | None,
    source_name: str | None,
    daily_limit: int,
    run_date: str,
    dry_run: bool,
) -> dict[str, int]:
    stats = {
        "sources": 0,
        "fetched": 0,
        "queued": 0,
        "selected": 0,
        "stored": 0,
        "filtered": 0,
        "archived": 0,
        "existing_skipped": 0,
        "errors": 0,
    }
    profile = PriorityProfile.from_latest_csv(
        root,
        known_institutions=config.institution_country.keys(),
    )
    logger.info(
        "stage=priority_profile status=DONE csv=%s rows=%d authors=%d institutions=%d",
        profile.csv_path.name,
        profile.row_count,
        len(profile.author_counts),
        len(profile.institution_counts),
    )

    candidates: dict[str, tuple[Article, Any]] = {}
    for source in config.sources:
        if not source.enabled:
            continue
        if source_name and source.name != source_name and source.type != source_name:
            continue
        stats["sources"] += 1
        logger.info("stage=source status=START source=%s type=%s mode=priority", source.name, source.type)
        try:
            fetcher = registry.create(source, root)
            articles = fetcher.fetch(limit=limit)
            stats["fetched"] += len(articles)
        except Exception as exc:
            logger.exception(
                "stage=source status=ERROR source=%s type=%s mode=priority error=%s",
                source.name,
                source.type,
                exc,
            )
            stats["errors"] += 1
            continue
        for article in articles:
            if storage.load_existing(article.id) is not None:
                stats["existing_skipped"] += 1
                logger.info(
                    "stage=priority_candidate status=SKIPPED article=%s reason=already_exists",
                    article.id,
                )
                continue
            article.metadata["download_date"] = run_date
            try:
                fetcher.prepare_for_priority(article)
            except Exception as exc:
                logger.warning(
                    "stage=priority_evidence status=WARN article=%s error=%s",
                    article.id,
                    exc,
                )
                stats["errors"] += 1
            filter_engine.enrich(article)
            candidates.setdefault(article.id, (article, fetcher))
        logger.info(
            "stage=source status=DONE source=%s candidates=%d mode=priority",
            source.name,
            len(articles),
        )

    ranked = profile.rank(article for article, _ in candidates.values())
    stats["queued"] = len(ranked)
    history = _load_priority_history(root, run_date)
    already_selected = {str(item.get("article_id", "")) for item in history}
    remaining = max(0, daily_limit - len(already_selected))
    selected = [article for article in ranked if article.id not in already_selected][:remaining]
    stats["selected"] = len(selected)
    logger.info(
        "stage=priority_queue status=DONE candidates=%d selected=%d previous_today=%d "
        "daily_limit=%d csv=%s",
        len(ranked),
        len(selected),
        len(already_selected),
        daily_limit,
        profile.csv_path.name,
    )

    for article in selected:
        fetcher = candidates[article.id][1]
        status = "error"
        try:
            fetcher.prepare(article)
            result = filter_engine.apply(article)
            _set_filter_metadata(article, result)
            _record_filter_result(logger, article, result)
            if result.included:
                stats["stored"] += 1
                status = "dry_run" if dry_run else "stored"
                if not dry_run:
                    storage.save(article, rebuild_index=False)
                logger.info(
                    "stage=storage status=%s article=%s priority_rank=%s",
                    "DRY_RUN" if dry_run else "DONE",
                    article.id,
                    article.metadata["priority"]["rank"],
                )
            else:
                stats["filtered"] += 1
                status = "filtered"
        except Exception as exc:
            stats["errors"] += 1
            logger.exception(
                "stage=priority_process status=ERROR article=%s error=%s", article.id, exc
            )
        if not dry_run:
            history.append(
                {
                    "article_id": article.id,
                    "rank": article.metadata.get("priority", {}).get("rank"),
                    "score": article.metadata.get("priority", {}).get("score", 0),
                    "status": status,
                    "processed_at": utc_now_iso(),
                }
            )

    if not dry_run:
        storage.rebuild_index()
        report_path = _write_priority_report(
            root, run_date, daily_limit, profile, ranked, history
        )
        logger.info("stage=priority_report status=DONE path=%s", report_path.relative_to(root))
    return stats


def run_producer(
    root: Path = PROJECT_ROOT,
    limit: int | None = None,
    source_name: str | None = None,
    dry_run: bool = False,
    refilter_existing: bool = False,
    prioritize: bool = False,
    daily_limit: int | None = None,
    run_date: str | None = None,
) -> dict[str, int]:
    started = time.monotonic()
    ensure_runtime_dirs(root)
    logger = setup_logging(root)
    run_date = run_date or date.today().isoformat()
    logger.info(
        "stage=environment status=DONE root=%s dry_run=%s refilter_existing=%s prioritize=%s run_date=%s",
        root,
        dry_run,
        refilter_existing,
        prioritize,
        run_date,
    )
    try:
        config = load_config(root)
    except Exception:
        logger.exception("stage=config status=ERROR")
        raise
    logger.info(
        "stage=config status=DONE sources=%d include=%s exclude=%s unknown_policy=%s",
        len(config.sources),
        config.filters["include"],
        config.filters["exclude"],
        config.filters["unknown_policy"],
    )
    storage = ArticleStorage(root)
    filter_engine = FilterEngine(config.filters, config.institution_country)

    if prioritize and refilter_existing:
        raise ValueError("prioritize and refilter_existing cannot be used together")

    if refilter_existing:
        stats = _run_existing_refilter(root, storage, filter_engine, logger, dry_run)
        logger.info(
            "stage=run status=DONE mode=refilter duration_seconds=%.3f stats=%s",
            time.monotonic() - started,
            stats,
        )
        return stats

    if prioritize:
        resolved_daily_limit = daily_limit
        if resolved_daily_limit is None:
            resolved_daily_limit = load_settings(root).daily_article_limit
        if resolved_daily_limit < 1:
            raise ValueError("daily_limit must be at least 1")
        stats = _run_prioritized_ingest(
            root=root,
            config=config,
            storage=storage,
            filter_engine=filter_engine,
            logger=logger,
            limit=limit,
            source_name=source_name,
            daily_limit=resolved_daily_limit,
            run_date=run_date,
            dry_run=dry_run,
        )
        logger.info(
            "stage=run status=DONE mode=priority duration_seconds=%.3f stats=%s",
            time.monotonic() - started,
            stats,
        )
        return stats

    stats = {
        "sources": 0,
        "fetched": 0,
        "stored": 0,
        "filtered": 0,
        "archived": 0,
        "errors": 0,
    }
    for source in config.sources:
        if not source.enabled:
            logger.info(
                "stage=source status=SKIPPED source=%s type=%s reason=disabled",
                source.name,
                source.type,
            )
            continue
        if source_name and source.name != source_name and source.type != source_name:
            logger.info(
                "stage=source status=SKIPPED source=%s type=%s reason=not_selected",
                source.name,
                source.type,
            )
            continue
        stats["sources"] += 1
        source_started = time.monotonic()
        logger.info("stage=source status=START source=%s type=%s", source.name, source.type)
        try:
            fetcher = registry.create(source, root)
            articles = fetcher.fetch(limit=limit)
            stats["fetched"] += len(articles)
            logger.info(
                "stage=source status=FETCHED source=%s candidates=%d duration_seconds=%.3f",
                source.name,
                len(articles),
                time.monotonic() - source_started,
            )
        except Exception as exc:
            logger.exception(
                "stage=source status=ERROR source=%s type=%s error=%s",
                source.name,
                source.type,
                exc,
            )
            stats["errors"] += 1
            continue

        for index, article in enumerate(articles, start=1):
            article.metadata["download_date"] = run_date
            try:
                fetcher.prepare(article)
            except Exception as exc:
                logger.exception(
                    "stage=prepare status=ERROR source=%s article=%s error=%s",
                    source.name,
                    article.id,
                    exc,
                )
                stats["errors"] += 1
                continue
            logger.info(
                "stage=filter status=START source=%s candidate=%d/%d article=%s",
                source.name,
                index,
                len(articles),
                article.id,
            )
            result = filter_engine.apply(article)
            _set_filter_metadata(article, result)
            _record_filter_result(logger, article, result)
            if not result.included:
                stats["filtered"] += 1
                existing = storage.load_active(article.id)
                if existing is not None and not dry_run:
                    storage.archive(article)
                    stats["archived"] += 1
                    logger.info("stage=storage status=ARCHIVED article=%s", article.id)
                continue

            stats["stored"] += 1
            if dry_run:
                logger.info("stage=storage status=DRY_RUN article=%s", article.id)
            else:
                storage.save(article, rebuild_index=False)
                logger.info("stage=storage status=DONE article=%s queue=inbox", article.id)

        logger.info(
            "stage=source status=DONE source=%s duration_seconds=%.3f",
            source.name,
            time.monotonic() - source_started,
        )

    if not dry_run:
        storage.rebuild_index()
        logger.info("stage=index status=DONE stored_this_run=%d", stats["stored"])
    logger.info(
        "stage=run status=DONE mode=ingest duration_seconds=%.3f stats=%s",
        time.monotonic() - started,
        stats,
    )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AirHub producer")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--source", default=None, help="Source name or type to run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prioritize",
        action="store_true",
        help="Manually rank candidates from the newest scope CSV before downloading PDFs",
    )
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=None,
        help="Override config/settings.json for this prioritized run",
    )
    parser.add_argument(
        "--refilter-existing",
        action="store_true",
        help="Re-evaluate active metadata and archive papers rejected by current filters",
    )
    args = parser.parse_args()
    run_producer(
        root=Path(args.root).resolve(),
        limit=args.limit,
        source_name=args.source,
        dry_run=args.dry_run,
        refilter_existing=args.refilter_existing,
        prioritize=args.prioritize,
        daily_limit=args.daily_limit,
    )


if __name__ == "__main__":
    main()
