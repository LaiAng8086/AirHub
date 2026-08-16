import json
import tempfile
import unittest
from pathlib import Path

from airhub.models import Article, ArticleStatus, Attachment
from airhub.storage import ArticleStorage


class ModelStorageTest(unittest.TestCase):
    def test_article_round_trip(self):
        article = Article(
            id="arxiv-2401.00001",
            type="paper",
            source="arxiv",
            title="Test Paper",
            authors=["A", "B"],
            tags=["cs.AI"],
            metadata={"countries": ["US"]},
        )
        restored = Article.from_dict(article.to_dict())
        self.assertEqual(restored.id, article.id)
        self.assertEqual(restored.metadata["countries"], ["US"])
        self.assertFalse(restored.status.processed)

    def test_storage_preserves_user_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = ArticleStorage(root)
            existing = Article(
                id="blog-1",
                type="blog",
                source="blog",
                title="Old",
                status=ArticleStatus(processed=True, read=True, favorite=True),
                html="attachments/html/blog-1.html",
            )
            storage.save(existing)
            refreshed = Article(id="blog-1", type="blog", source="blog", title="New")
            storage.save(refreshed)
            data = json.loads((root / "metadata" / "articles" / "blog-1.json").read_text())
            self.assertEqual(data["title"], "New")
            self.assertTrue(data["status"]["processed"])
            self.assertTrue(data["status"]["read"])
            self.assertTrue(data["status"]["favorite"])
            self.assertEqual(data["html"], "attachments/html/blog-1.html")

    def test_archive_and_restore_preserve_state_and_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = ArticleStorage(root)
            existing = Article(
                id="paper-1",
                type="paper",
                source="arxiv",
                title="Old",
                status=ArticleStatus(read=True, favorite=True),
                attachments=[Attachment(type="pdf", path="attachments/pdf/paper-1.pdf")],
            )
            storage.save(existing)
            rejected = Article(id="paper-1", type="paper", source="arxiv", title="Rejected")
            storage.archive(rejected)

            self.assertTrue((root / "filtered" / "paper-1.json").exists())
            self.assertFalse((root / "metadata" / "articles" / "paper-1.json").exists())
            archived = json.loads((root / "filtered" / "paper-1.json").read_text())
            self.assertTrue(archived["status"]["favorite"])
            self.assertEqual(archived["attachments"][0]["type"], "pdf")

            restored = Article(id="paper-1", type="paper", source="arxiv", title="Restored")
            storage.save(restored)
            active = json.loads((root / "metadata" / "articles" / "paper-1.json").read_text())
            self.assertTrue(active["status"]["read"])
            self.assertFalse((root / "filtered" / "paper-1.json").exists())

    def test_defer_preserves_package_in_cache_and_removes_active_queue_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = ArticleStorage(root)
            article = Article(
                id="paper-pending",
                type="paper",
                source="arxiv",
                title="Pending",
                attachments=[Attachment(type="pdf", path="attachments/pdf/pending.pdf")],
            )
            storage.save(article)

            storage.defer(article.id)

            self.assertIsNone(storage.load_active(article.id))
            cached = storage.load_cached(article.id)
            self.assertIsNotNone(cached)
            self.assertEqual(cached.attachments[0].type, "pdf")

    def test_start_processing_uses_standard_queue_and_preserves_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = ArticleStorage(root)
            article = Article(id="podcast-1", type="podcast_episode", source="test", title="P")
            storage.start_processing(article)
            self.assertTrue((root / "processing" / "podcast-1.json").is_file())
            self.assertFalse((root / "inbox" / "podcast-1.json").exists())

            article.attachments.append(
                Attachment(type="audio", path="attachments/audio/podcast-1.mp3")
            )
            storage.start_processing(article)
            restored = storage.load_active("podcast-1")
            self.assertEqual(restored.attachments[0].type, "audio")


if __name__ == "__main__":
    unittest.main()
