from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airhub.blog_archive import BlogArchiveResult, BlogSiteStore
from airhub.daily_state import mark_step
from airhub.fetchers import registry
from airhub.fetchers.arxiv import ArxivFetcher
from airhub.fetchers.base import Fetcher
from airhub.models import Article, Attachment
from airhub.queue_management import (
    add_overflow_batch,
    defer_pending_articles,
    import_manual_arxiv_file,
    list_manual_files,
    read_manual_entries,
    read_manual_arxiv_ids,
)
from airhub.storage import ArticleStorage


class UnitQueueFetcher(Fetcher):
    source_type = "unit-queue"
    prepared: list[str] = []

    def fetch(self, limit: int | None = None) -> list[Article]:
        return []

    def prepare(self, article: Article) -> Article:
        type(self).prepared.append(article.id)
        article.attachments.append(
            Attachment(type="html", path=f"attachments/source/{article.id}.html")
        )
        return article


registry.register(UnitQueueFetcher)


def prepare_config(root: Path, source_type: str, daily_limit: int = 2) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    (config / "sources.json").write_text(
        json.dumps(
            {"sources": [{"type": source_type, "name": "unit", "enabled": True}]}
        ),
        encoding="utf-8",
    )
    (config / "settings.json").write_text(
        json.dumps({"daily_article_limit": daily_limit}), encoding="utf-8"
    )
    (config / "filters.yaml").write_text("", encoding="utf-8")


class QueueManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        UnitQueueFetcher.prepared = []

    def test_manual_files_are_recursive_and_ids_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "manual" / "nested"
            nested.mkdir(parents=True)
            second = nested / "b.txt"
            second.write_text(
                "# comment\narXiv:2608.00001v1\nhttps://arxiv.org/pdf/2608.00002.pdf\n",
                encoding="utf-8",
            )
            first = root / "manual" / "a.txt"
            first.write_text("2608.00003\n", encoding="utf-8")

            self.assertEqual(list_manual_files(root), [first.resolve(), second.resolve()])
            self.assertEqual(
                read_manual_arxiv_ids(second), ["2608.00001v1", "2608.00002"]
            )

    def test_manual_import_caches_and_activates_every_returned_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_config(root, "arxiv")
            manual = root / "manual" / "papers.txt"
            manual.parent.mkdir()
            manual.write_text(
                "2608.00001\n2608.00001v1\n2608.00002v2\n", encoding="utf-8"
            )
            returned = [
                Article(
                    id="arxiv-2608.00001v1",
                    type="paper",
                    source="arxiv",
                    title="One",
                    metadata={"arxiv_id": "2608.00001v1"},
                ),
                Article(
                    id="arxiv-2608.00002v2",
                    type="paper",
                    source="arxiv",
                    title="Two",
                    metadata={"arxiv_id": "2608.00002v2"},
                ),
            ]

            def prepare(article: Article) -> Article:
                article.attachments.append(
                    Attachment(type="pdf", path=f"attachments/pdf/{article.id}.pdf")
                )
                return article

            with patch.object(ArxivFetcher, "fetch_by_ids", return_value=returned), patch.object(
                ArxivFetcher, "prepare", side_effect=prepare
            ):
                stats = import_manual_arxiv_file(
                    root, manual, run_date="2026-08-13"
                )

            self.assertEqual(stats["requested"], 3)
            self.assertEqual(stats["stored"], 2)
            self.assertEqual(stats["already_active"], 1)
            self.assertEqual(stats["errors"], 0)
            for article in returned:
                self.assertTrue((root / "inbox" / f"{article.id}.json").is_file())
                self.assertTrue(
                    (root / "data" / "article_cache" / f"{article.id}.json").is_file()
                )
            report = json.loads((root / stats["report"]).read_text(encoding="utf-8"))
            self.assertEqual(report["stored_count"], 2)
            self.assertEqual(
                [item["status"] for item in report["articles"]],
                ["saved", "already_active", "saved"],
            )

    def test_mixed_manual_import_archives_blog_and_adds_its_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_config(root, "arxiv")
            manual = root / "manual" / "mixed.txt"
            manual.parent.mkdir()
            manual.write_text(
                "2608.00001v1\nhttps://Blog.Example/posts/one\n", encoding="utf-8"
            )
            parsed = read_manual_entries(manual)
            self.assertEqual([entry.kind for entry in parsed], ["arxiv", "blog"])
            article = Article(
                id="arxiv-2608.00001v1",
                type="paper",
                source="arxiv",
                title="One",
                metadata={"arxiv_id": "2608.00001v1"},
            )

            def prepare(item: Article) -> Article:
                item.attachments.append(
                    Attachment(type="html", path=f"attachments/source/{item.id}.html")
                )
                return item

            def archive(_root: Path, url: str) -> BlogArchiveResult:
                output = _root / "attachments" / "blog" / "snapshot.html"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("<html></html>", encoding="utf-8")
                return BlogArchiveResult(
                    url=url,
                    final_url=url,
                    origin="https://blog.example",
                    output_path=output,
                    resources_embedded=3,
                    optional_resources_removed=0,
                )

            with (
                patch.object(ArxivFetcher, "fetch_by_ids", return_value=[article]),
                patch.object(ArxivFetcher, "prepare", side_effect=prepare),
                patch("airhub.queue_management.archive_blog", side_effect=archive),
            ):
                stats = import_manual_arxiv_file(root, manual)

            self.assertEqual(stats["requested"], 2)
            self.assertEqual(stats["stored"], 1)
            self.assertEqual(stats["blogs_archived"], 1)
            self.assertEqual(stats["blog_sites_added"], 1)
            self.assertEqual(
                [site.origin for site in BlogSiteStore(root).list()],
                ["https://blog.example"],
            )

    def test_github_and_huggingface_are_catalogued_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manual = root / "manual" / "hubs.txt"
            manual.parent.mkdir()
            manual.write_text(
                "https://github.com/example/repo\n"
                "https://huggingface.co/example/model\n"
                "https://author.github.io/project/\n",
                encoding="utf-8",
            )

            def archive(_root: Path, url: str) -> BlogArchiveResult:
                output = _root / "attachments" / "blog" / "static.html"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("<html></html>", encoding="utf-8")
                return BlogArchiveResult(
                    url=url,
                    final_url=url,
                    origin="https://author.github.io",
                    output_path=output,
                    resources_embedded=1,
                    optional_resources_removed=0,
                )

            with patch("airhub.queue_management.archive_blog", side_effect=archive) as call:
                stats = import_manual_arxiv_file(root, manual)

            self.assertEqual(stats["requested_blogs"], 3)
            self.assertEqual(stats["blogs_catalog_only"], 2)
            self.assertEqual(stats["blogs_archived"], 1)
            call.assert_called_once_with(root.resolve(), "https://author.github.io/project/")
            self.assertEqual(
                [site.origin for site in BlogSiteStore(root).list()],
                ["https://github.com", "https://huggingface.co", "https://author.github.io"],
            )

    def test_overflow_adds_next_daily_limit_in_priority_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_config(root, "unit-queue", daily_limit=2)
            run_date = "2026-08-13"
            articles = [
                Article(
                    id=f"paper-{index}",
                    type="paper",
                    source="unit-queue",
                    title=f"Paper {index}",
                    publish_date=f"2026-08-{14 - index:02d}T00:00:00Z",
                )
                for index in range(1, 5)
            ]
            candidate_path = root / "data" / "candidates" / f"{run_date}.json"
            candidate_path.parent.mkdir(parents=True)
            candidate_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "date": run_date,
                        "limit": 300,
                        "complete": True,
                        "errors": 0,
                        "articles": [article.to_dict() for article in articles],
                    }
                ),
                encoding="utf-8",
            )
            report_path = root / "data" / "selection" / f"{run_date}.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "date": run_date,
                        "strategy": "priority",
                        "selected_count": 1,
                        "stored_count": 1,
                        "errors": 0,
                        "articles": [
                            {
                                "article_id": article.id,
                                "title": article.title,
                                "status": "saved" if index == 1 else "not_selected",
                                "priority": {"rank": index},
                            }
                            for index, article in enumerate(articles, start=1)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mark_step(
                "strategy_applied", root, strategy="priority", run_date=run_date
            )

            stats = add_overflow_batch(root, run_date=run_date)

            self.assertEqual(stats["stored"], 2)
            self.assertEqual(stats["article_ids"], ["paper-2", "paper-3"])
            self.assertEqual(UnitQueueFetcher.prepared, ["paper-2", "paper-3"])
            self.assertTrue((root / "inbox" / "paper-2.json").is_file())
            self.assertTrue((root / "data" / "article_cache" / "paper-3.json").is_file())
            self.assertFalse((root / "inbox" / "paper-4.json").exists())
            updated = json.loads(report_path.read_text(encoding="utf-8"))
            statuses = {item["article_id"]: item["status"] for item in updated["articles"]}
            self.assertEqual(statuses["paper-2"], "saved")
            self.assertEqual(statuses["paper-3"], "saved")
            self.assertEqual(statuses["paper-4"], "not_selected")

    def test_defer_moves_pending_article_to_cache_and_updates_strategy_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = ArticleStorage(root)
            first = Article(
                id="paper-1", type="paper", source="unit-queue", title="One"
            )
            second = Article(
                id="paper-2", type="paper", source="unit-queue", title="Two"
            )
            storage.save(first)
            storage.save(second)
            report_path = root / "data" / "selection" / "2026-08-13.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "date": "2026-08-13",
                        "strategy": "priority",
                        "selected_count": 2,
                        "stored_count": 2,
                        "articles": [
                            {
                                "article_id": "paper-1",
                                "status": "saved",
                                "priority": {"rank": 1},
                            },
                            {
                                "article_id": "paper-2",
                                "status": "saved",
                                "priority": {"rank": 2},
                            },
                            {
                                "article_id": "paper-3",
                                "status": "not_selected",
                                "priority": {"rank": 3},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stats = defer_pending_articles(root, ["paper-2"])

            self.assertEqual(stats["deferred"], 1)
            self.assertFalse((root / "inbox" / "paper-2.json").exists())
            self.assertFalse((root / "metadata" / "articles" / "paper-2.json").exists())
            self.assertTrue((root / "data" / "article_cache" / "paper-2.json").is_file())
            self.assertTrue((root / "inbox" / "paper-1.json").is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            statuses = {item["article_id"]: item["status"] for item in report["articles"]}
            self.assertEqual(statuses["paper-2"], "not_selected")
            self.assertEqual(report["stored_count"], 1)
            deferred_entry = next(
                item for item in report["articles"] if item["article_id"] == "paper-2"
            )
            self.assertEqual(deferred_entry["priority_rank_before_defer"], 2)
            self.assertEqual(deferred_entry["priority"]["rank"], 4)

    def test_deferred_priority_article_is_not_selected_by_next_overflow_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_date = "2026-08-13"
            prepare_config(root, "unit-queue", daily_limit=1)
            articles = [
                Article(
                    id=f"paper-{index}",
                    type="paper",
                    source="unit-queue",
                    title=f"Paper {index}",
                    publish_date=f"2026-08-{14 - index:02d}T00:00:00Z",
                )
                for index in range(1, 5)
            ]
            storage = ArticleStorage(root)
            storage.save(articles[0])
            storage.save(articles[1])
            candidate_path = root / "data" / "candidates" / f"{run_date}.json"
            candidate_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "date": run_date,
                        "limit": 300,
                        "complete": True,
                        "errors": 0,
                        "articles": [article.to_dict() for article in articles],
                    }
                ),
                encoding="utf-8",
            )
            report_path = root / "data" / "selection" / f"{run_date}.json"
            report_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "date": run_date,
                        "strategy": "priority",
                        "selected_count": 2,
                        "stored_count": 2,
                        "errors": 0,
                        "articles": [
                            {
                                "article_id": article.id,
                                "status": "saved" if index <= 2 else "not_selected",
                                "priority": {"rank": index},
                            }
                            for index, article in enumerate(articles, start=1)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mark_step(
                "strategy_applied", root, strategy="priority", run_date=run_date
            )

            defer_pending_articles(root, ["paper-2"], run_date=run_date)
            stats = add_overflow_batch(root, run_date=run_date)

            self.assertEqual(stats["article_ids"], ["paper-3"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            entries = {item["article_id"]: item for item in report["articles"]}
            self.assertEqual(entries["paper-2"]["priority"]["rank"], 5)
            self.assertEqual(entries["paper-2"]["status"], "not_selected")
            self.assertEqual(entries["paper-3"]["status"], "saved")

    def test_defer_changes_current_strategy_existing_entry_to_not_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ArticleStorage(root).save(
                Article(
                    id="older-pending",
                    type="paper",
                    source="unit-queue",
                    title="Older pending",
                )
            )
            report_path = root / "data" / "selection" / "2026-08-13.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "date": "2026-08-13",
                        "strategy": "priority",
                        "selected_count": 0,
                        "stored_count": 0,
                        "articles": [
                            {
                                "article_id": "older-pending",
                                "status": "existing",
                                "priority": {"rank": 7},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            defer_pending_articles(root, ["older-pending"])

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["articles"][0]["status"], "not_selected")
            self.assertEqual(report["selected_count"], 0)
            self.assertEqual(report["stored_count"], 0)


if __name__ == "__main__":
    unittest.main()
