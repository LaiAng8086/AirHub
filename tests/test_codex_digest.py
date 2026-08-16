from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from airhub.codex_digest import (
    complete_digest_task,
    list_digest_candidates,
    select_digest_task,
)
from airhub.models import Article


def write_article(root: Path, article: Article, queue: str = "inbox") -> Path:
    path = root / queue / f"{article.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(article.to_dict(), ensure_ascii=False), encoding="utf-8")
    return path


class CodexDigestSelectionTest(unittest.TestCase):
    def test_list_includes_dates_and_resumable_processing_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_article(
                root,
                Article(
                    id="inbox-item",
                    type="paper",
                    source="test",
                    title="Inbox Item",
                    metadata={"download_date": "2026-08-12", "priority": {"rank": 1}},
                ),
            )
            processing = write_article(
                root,
                Article(
                    id="processing-item",
                    type="paper",
                    source="test",
                    title="Processing Item",
                    metadata={"download_date": "2026-08-13", "priority": {"rank": 2}},
                ),
                queue="processing",
            )

            candidates = list_digest_candidates(root)

            self.assertEqual(
                [item.article_id for item in candidates],
                ["processing-item", "inbox-item"],
            )
            self.assertEqual(candidates[0].added_date, "2026-08-13")
            self.assertEqual(candidates[0].priority_rank, 2)
            self.assertEqual(candidates[0].task.article_path, processing.resolve())

    def test_default_selects_latest_run_then_best_priority_rank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_article(
                root,
                Article(
                    id="rank-two",
                    type="paper",
                    source="test",
                    title="Rank Two",
                    metadata={"download_date": "2026-08-11", "priority": {"rank": 2}},
                ),
            )
            write_article(
                root,
                Article(
                    id="rank-one",
                    type="paper",
                    source="test",
                    title="Rank One",
                    metadata={"download_date": "2026-08-11", "priority": {"rank": 1}},
                ),
            )
            write_article(
                root,
                Article(
                    id="older-rank-one",
                    type="paper",
                    source="test",
                    title="Older Rank One",
                    metadata={"download_date": "2026-08-10", "priority": {"rank": 1}},
                ),
            )
            task = select_digest_task(root)
            self.assertEqual(task.article_path.name, "rank-one.json")
            self.assertEqual(task.output_path.name, "rank-one.html")

            task.output_path.parent.mkdir(parents=True)
            task.output_path.write_text("done", encoding="utf-8")
            self.assertEqual(select_digest_task(root).article_path.name, "rank-two.json")

    def test_explicit_processing_article_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_article(
                root,
                Article(id="chosen", type="paper", source="test", title="Chosen"),
                queue="processing",
            )
            task = select_digest_task(root, "processing/chosen.json")
            self.assertEqual(task.article_path, path.resolve())
            self.assertEqual(task.output_path, root / "attachments" / "html" / "chosen.html")

    def test_explicit_article_outside_queues_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_article(
                root,
                Article(id="bad", type="paper", source="test", title="Bad"),
                queue="metadata",
            )
            with self.assertRaisesRegex(ValueError, "inbox/ 或 processing/"):
                select_digest_task(root, str(path))

    def test_complete_moves_article_to_finished_and_records_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            article_path = write_article(
                root,
                Article(id="done", type="paper", source="test", title="Done"),
            )
            output = root / "attachments" / "html" / "done.html"
            output.parent.mkdir(parents=True)
            output.write_text("<html></html>", encoding="utf-8")
            complete_digest_task(root, "inbox/done.json", "attachments/html/done.html")
            self.assertFalse(article_path.exists())
            finished = json.loads(
                (root / "finished" / "done.json").read_text(encoding="utf-8")
            )
            self.assertTrue(finished["status"]["processed"])
            self.assertEqual(finished["html"], "attachments/html/done.html")

    def test_complete_is_idempotent_when_inner_codex_already_finished_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            article = Article(id="done", type="paper", source="test", title="Done")
            article.status.processed = True
            article.html = "attachments/html/done.html"
            write_article(root, article, queue="finished")
            output = root / "attachments" / "html" / "done.html"
            output.parent.mkdir(parents=True)
            output.write_text("<html></html>", encoding="utf-8")

            complete_digest_task(root, "inbox/done.json", article.html)

            self.assertTrue((root / "finished" / "done.json").is_file())

    def test_complete_rejects_inconsistent_finished_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            article = Article(id="done", type="paper", source="test", title="Done")
            article.status.processed = True
            article.html = "attachments/html/other.html"
            write_article(root, article, queue="finished")
            output = root / "attachments" / "html" / "done.html"
            output.parent.mkdir(parents=True)
            output.write_text("<html></html>", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "状态与解读 HTML 不一致"):
                complete_digest_task(
                    root,
                    "inbox/done.json",
                    "attachments/html/done.html",
                )


if __name__ == "__main__":
    unittest.main()
