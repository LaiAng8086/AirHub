from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from airhub.daily_state import load_daily_state, mark_step
from airhub.fetchers import registry
from airhub.fetchers.base import Fetcher
from airhub.models import Article, Attachment
from airhub.storage import ArticleStorage
from airhub.workflow import (
    apply_daily_strategy,
    clear_runtime_cache,
    collect_article_statuses,
    discover_recent_candidates,
    format_article_statuses,
    update_priority_strategy,
)


class UnitWorkflowFetcher(Fetcher):
    source_type = "unit-workflow"
    fetch_calls = 0
    prepared: list[str] = []

    def fetch(self, limit: int | None = None) -> list[Article]:
        type(self).fetch_calls += 1
        values = [
            Article(
                id="preferred",
                type="paper",
                source=self.source_type,
                title="Rejected but preferred",
                authors=["Alice Smith"],
                publish_date="2026-08-11T10:00:00Z",
            ),
            Article(
                id="fixed",
                type="paper",
                source=self.source_type,
                title="Accepted fixed paper",
                authors=["Bob Jones"],
                publish_date="2026-08-10T10:00:00Z",
            ),
        ]
        return values[:limit] if limit else values

    def prepare(self, article: Article) -> Article:
        type(self).prepared.append(article.id)
        article.attachments.append(
            Attachment(
                type="html",
                path=f"attachments/source/{article.metadata['download_date']}/{article.id}.html",
            )
        )
        return article


registry.register(UnitWorkflowFetcher)


def prepare_root(root: Path, daily_limit: int = 1) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {"type": "unit-workflow", "name": "unit", "enabled": True}
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "config" / "settings.json").write_text(
        json.dumps({"daily_article_limit": daily_limit}), encoding="utf-8"
    )
    (root / "config" / "filters.yaml").write_text(
        "exclude:\n  keywords:\n    - rejected\n", encoding="utf-8"
    )
    scope = root / "scope" / "library.csv"
    scope.parent.mkdir(parents=True)
    with scope.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Author", "Institution"])
        writer.writeheader()
        writer.writerows(
            [
                {"Author": "Smith, Alice", "Institution": "Example University"},
                {"Author": "Smith, Alice", "Institution": "Example University"},
                {"Author": "Jones, Bob", "Institution": "Other Lab"},
            ]
        )


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        UnitWorkflowFetcher.fetch_calls = 0
        UnitWorkflowFetcher.prepared = []

    def test_discovery_reuses_same_day_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            first = discover_recent_candidates(root, run_date="2026-08-11")
            second = discover_recent_candidates(root, run_date="2026-08-11")
            self.assertFalse(first.cached)
            self.assertTrue(second.cached)
            self.assertEqual(UnitWorkflowFetcher.fetch_calls, 1)
            self.assertEqual(len(second.articles), 2)

    def test_discovery_retries_an_incomplete_same_day_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            snapshot = root / "data" / "candidates" / "2026-08-11.json"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "date": "2026-08-11",
                        "limit": 300,
                        "errors": 1,
                        "articles": [],
                    }
                ),
                encoding="utf-8",
            )
            mark_step(
                "strategy_applied",
                root,
                run_date="2026-08-11",
                strategy="priority",
            )
            report = root / "data" / "selection" / "2026-08-11.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(
                    {
                        "candidate_count": 0,
                        "selected_count": 0,
                        "stored_count": 0,
                        "articles": [],
                    }
                ),
                encoding="utf-8",
            )

            result = discover_recent_candidates(root, run_date="2026-08-11")

            self.assertFalse(result.cached)
            self.assertEqual(UnitWorkflowFetcher.fetch_calls, 1)
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertTrue(payload["complete"])
            self.assertEqual(payload["errors"], 0)
            self.assertEqual(len(payload["articles"]), 2)
            state = load_daily_state(root, run_date="2026-08-11")
            self.assertIsNone(state["strategy"])
            self.assertFalse(state["steps"]["strategy_applied"])
            self.assertEqual(state["counts"]["discovery_errors"], 0)
            self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["invalidated"])

    def test_priority_is_separate_from_fixed_filter_and_daily_choice_is_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            update_priority_strategy(root, run_date="2026-08-11")
            discover_recent_candidates(root, run_date="2026-08-11")
            result = apply_daily_strategy("priority", root, run_date="2026-08-11")
            self.assertEqual(result["stored"], 1)
            self.assertEqual(UnitWorkflowFetcher.prepared, ["preferred"])
            with self.assertRaisesRegex(ValueError, "不能再切换"):
                apply_daily_strategy("fixed", root, run_date="2026-08-11")

    def test_fixed_strategy_applies_only_fixed_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            discover_recent_candidates(root, run_date="2026-08-11")
            result = apply_daily_strategy("fixed", root, run_date="2026-08-11")
            self.assertEqual(result["eligible"], 1)
            self.assertEqual(UnitWorkflowFetcher.prepared, ["fixed"])

    def test_daily_state_resets_on_date_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mark_step("discovered", root, run_date="2026-08-11")
            tomorrow = load_daily_state(root, run_date="2026-08-12")
            self.assertFalse(tomorrow["steps"]["discovered"])
            self.assertIsNone(tomorrow["strategy"])

    def test_statuses_are_grouped_and_sorted_by_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            metadata = root / "metadata" / "articles"
            metadata.mkdir(parents=True)
            for article_id, day in (("older", "2026-08-10"), ("newer", "2026-08-11")):
                (metadata / f"{article_id}.json").write_text(
                    json.dumps(
                        Article(
                            id=article_id,
                            type="paper",
                            source="test",
                            title=article_id,
                            metadata={"download_date": day},
                        ).to_dict()
                    ),
                    encoding="utf-8",
                )
            groups = collect_article_statuses(root)
            self.assertEqual([item["id"] for item in groups["待解读"]], ["newer", "older"])
            rendered = format_article_statuses(groups)
            self.assertLess(rendered.index("2026-08-11"), rendered.index("2026-08-10"))

    def test_priority_not_selected_status_explains_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            report = root / "data" / "selection" / "2026-08-11.json"
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps(
                    {
                        "date": "2026-08-11",
                        "strategy": "priority",
                        "articles": [
                            {
                                "article_id": "ranked-only",
                                "title": "Ranked only",
                                "date": "2026-08-11",
                                "status": "not_selected",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            groups = collect_article_statuses(root)

            self.assertEqual(
                [item["id"] for item in groups["优选排名未进入每日上限"]],
                ["ranked-only"],
            )

    def test_status_lists_inactive_article_packages_as_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = ArticleStorage(root)
            storage.cache(
                Article(
                    id="cached-only",
                    type="paper",
                    source="arxiv",
                    title="Cached only",
                    metadata={"download_date": "2026-08-13"},
                )
            )

            groups = collect_article_statuses(root)

            self.assertEqual([item["id"] for item in groups["缓存"]], ["cached-only"])

    def test_clear_cache_preserves_articles_and_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            cache_file = root / "cache" / "arxiv_html" / "entry.json"
            candidate_file = root / "data" / "candidates" / "2026-08-11.json"
            article_file = root / "inbox" / "paper.json"
            attachment = root / "attachments" / "pdf" / "2026-08-11" / "paper.pdf"
            for path in (cache_file, candidate_file, article_file, attachment):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("data", encoding="utf-8")
            stats = clear_runtime_cache(root)
            self.assertEqual(stats["files"], 2)
            self.assertFalse(cache_file.exists())
            self.assertFalse(candidate_file.exists())
            self.assertTrue(article_file.exists())
            self.assertTrue(attachment.exists())


if __name__ == "__main__":
    unittest.main()
