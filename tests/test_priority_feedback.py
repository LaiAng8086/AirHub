from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from airhub.models import Article
from airhub.priority import canonical_author
from airhub.priority_feedback import PriorityFeedbackStore
from airhub.storage import ArticleStorage
from airhub.workflow import update_priority_strategy


class PriorityFeedbackTest(unittest.TestCase):
    @staticmethod
    def _completed_article(root: Path) -> Article:
        article = Article(
            id="paper-complete",
            type="paper",
            source="arxiv",
            title="Completed Paper",
            authors=["Alice Smith", "Bob Jones"],
            metadata={"institutions": ["Example University"]},
        )
        html_path = root / "attachments" / "html" / f"{article.id}.html"
        html_path.parent.mkdir(parents=True)
        html_path.write_text("<html></html>", encoding="utf-8")
        ArticleStorage(root).complete(article, html_path.relative_to(root).as_posix())
        return article

    @staticmethod
    def _priority_inputs(root: Path) -> None:
        config = root / "config"
        config.mkdir(parents=True)
        (config / "sources.json").write_text(
            json.dumps({"sources": []}), encoding="utf-8"
        )
        (config / "filters.yaml").write_text(
            "include: {}\nexclude: {}\n", encoding="utf-8"
        )
        csv_path = root / "scope" / "base.csv"
        csv_path.parent.mkdir()
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["Author", "Institution"])
            writer.writeheader()
            writer.writerow(
                {"Author": "Smith, Alice", "Institution": "Example University"}
            )

    def test_completed_article_is_added_once_and_survives_profile_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._priority_inputs(root)
            article = self._completed_article(root)
            store = PriorityFeedbackStore(root)

            candidates = store.list_completed_papers()
            self.assertEqual([item.article_id for item in candidates], [article.id])
            self.assertFalse(candidates[0].already_added)

            first = store.add_articles([article.id])
            second = store.add_articles([article.id])
            self.assertEqual(first["added"], 1)
            self.assertEqual(first["author_increments"], 2)
            self.assertEqual(first["institution_increments"], 1)
            self.assertEqual(second["added"], 0)
            self.assertEqual(second["skipped"], 1)

            profile = update_priority_strategy(root, run_date="2026-08-16")
            rebuilt = update_priority_strategy(root, run_date="2026-08-16")
            self.assertEqual(profile.author_counts[canonical_author("Alice Smith")], 2)
            self.assertEqual(profile.author_counts[canonical_author("Bob Jones")], 1)
            self.assertEqual(profile.institution_counts["example university"], 2)
            self.assertEqual(rebuilt.author_counts, profile.author_counts)
            payload = json.loads(
                (root / "data" / "priority" / "profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["feedback_articles"], 1)

    def test_non_completed_or_non_paper_article_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = ArticleStorage(root)
            storage.save(
                Article(id="pending", type="paper", source="arxiv", title="Pending")
            )
            with self.assertRaisesRegex(ValueError, "不是可用的已解读论文"):
                PriorityFeedbackStore(root).add_articles(["pending"])


if __name__ == "__main__":
    unittest.main()
