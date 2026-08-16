from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from airhub.fetchers import registry
from airhub.fetchers.base import Fetcher
from airhub.models import Article, Attachment
from airhub.priority import PriorityProfile, canonical_author, newest_scope_csv
from airhub.producer import run_producer


class UnitPriorityFetcher(Fetcher):
    source_type = "unit-priority"
    light_prepared: list[str] = []
    heavy_prepared: list[str] = []

    def fetch(self, limit: int | None = None) -> list[Article]:
        articles = [
            Article(id="high", type="paper", source=self.source_type, title="High", authors=["Alice Smith"]),
            Article(id="middle", type="paper", source=self.source_type, title="Middle", authors=["Bob Jones"]),
            Article(id="low", type="paper", source=self.source_type, title="Low", authors=["Carol White"]),
        ]
        return articles[:limit] if limit else articles

    def prepare_for_priority(self, article: Article) -> Article:
        self.light_prepared.append(article.id)
        return article

    def prepare(self, article: Article) -> Article:
        self.heavy_prepared.append(article.id)
        article.attachments.append(
            Attachment(type="pdf", path=f"attachments/pdf/{article.metadata['download_date']}/{article.id}.pdf")
        )
        return article


registry.register(UnitPriorityFetcher)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Author", "Institution", "Title"])
        writer.writeheader()
        writer.writerows(rows)


class PriorityTest(unittest.TestCase):
    def test_only_newest_csv_is_loaded_and_names_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "scope" / "old.csv"
            new = root / "scope" / "new.csv"
            write_csv(old, [{"Author": "Old, Author", "Institution": "Old University", "Title": "Old"}])
            write_csv(
                new,
                [
                    {"Author": "Smith, Alice", "Institution": "Example University", "Title": "A"},
                    {"Author": "Smith, Alice", "Institution": "Example University", "Title": "B"},
                    {"Author": "Jones, Bob", "Institution": "Other Lab", "Title": "C"},
                ],
            )
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))
            self.assertEqual(newest_scope_csv(root), new)
            profile = PriorityProfile.from_latest_csv(root)
            self.assertEqual(profile.author_counts[canonical_author("Alice Smith")], 2)
            self.assertNotIn(canonical_author("Old Author"), profile.author_counts)
            self.assertEqual(profile.institution_counts["example university"], 2)

    def test_author_and_institution_frequency_rank_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scope.csv"
            write_csv(
                csv_path,
                [
                    {"Author": "Smith, Alice", "Institution": "Example University", "Title": "A"},
                    {"Author": "Smith, Alice", "Institution": "Example University", "Title": "B"},
                    {"Author": "Jones, Bob", "Institution": "Other Lab", "Title": "C"},
                ],
            )
            profile = PriorityProfile.from_csv(csv_path)
            ranked = profile.rank(
                [
                    Article(id="b", type="paper", source="test", title="B", authors=["Bob Jones"]),
                    Article(
                        id="a",
                        type="paper",
                        source="test",
                        title="A",
                        authors=["Alice Smith"],
                        metadata={"affiliation_source_text": "Example University"},
                    ),
                ]
            )
            self.assertEqual([article.id for article in ranked], ["a", "b"])
            self.assertEqual(ranked[0].metadata["priority"]["score"], 4)

    def test_prioritized_run_prepares_only_daily_top_n_and_keeps_daily_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(parents=True)
            (root / "config" / "sources.json").write_text(
                json.dumps({"sources": [{"type": "unit-priority", "name": "unit", "enabled": True}]}),
                encoding="utf-8",
            )
            (root / "config" / "filters.yaml").write_text("include: {}\nexclude: {}\n", encoding="utf-8")
            write_csv(
                root / "scope" / "library.csv",
                [
                    {"Author": "Smith, Alice", "Institution": "Example University", "Title": "1"},
                    {"Author": "Smith, Alice", "Institution": "Example University", "Title": "2"},
                    {"Author": "Smith, Alice", "Institution": "Example University", "Title": "3"},
                    {"Author": "Jones, Bob", "Institution": "Other Lab", "Title": "4"},
                    {"Author": "Jones, Bob", "Institution": "Other Lab", "Title": "5"},
                ],
            )
            UnitPriorityFetcher.light_prepared = []
            UnitPriorityFetcher.heavy_prepared = []
            stats = run_producer(
                root=root,
                prioritize=True,
                daily_limit=2,
                run_date="2026-08-11",
            )
            self.assertEqual(stats["queued"], 3)
            self.assertEqual(stats["selected"], 2)
            self.assertEqual(UnitPriorityFetcher.light_prepared, ["high", "middle", "low"])
            self.assertEqual(UnitPriorityFetcher.heavy_prepared, ["high", "middle"])
            self.assertFalse((root / "inbox" / "low.json").exists())
            high = json.loads((root / "inbox" / "high.json").read_text(encoding="utf-8"))
            self.assertEqual(high["metadata"]["download_date"], "2026-08-11")
            self.assertIn("attachments/pdf/2026-08-11/", high["attachments"][0]["path"])

            again = run_producer(
                root=root,
                prioritize=True,
                daily_limit=2,
                run_date="2026-08-11",
            )
            self.assertEqual(again["selected"], 0)
            self.assertEqual(UnitPriorityFetcher.heavy_prepared, ["high", "middle"])
            report = json.loads(
                (root / "data" / "priority" / "2026-08-11.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["selected_count"], 2)


if __name__ == "__main__":
    unittest.main()
