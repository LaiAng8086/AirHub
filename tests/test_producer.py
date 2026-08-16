from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from airhub.fetchers.base import Fetcher
from airhub.fetchers import registry
from airhub.models import Article, ArticleStatus, Attachment
from airhub.producer import run_producer
from airhub.storage import ArticleStorage


class UnitFilterFetcher(Fetcher):
    source_type = "unit-filter"

    def fetch(self, limit: int | None = None) -> list[Article]:
        return [
            Article(id="keep", type="paper", source="unit-filter", title="Useful Robotics"),
            Article(id="drop", type="paper", source="unit-filter", title="Rejected Robotics"),
        ]


registry.register(UnitFilterFetcher)


class ProducerTest(unittest.TestCase):
    @staticmethod
    def _write_config(root: Path, filters: str, sources: list[dict] | None = None) -> None:
        config_dir = root / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "sources.json").write_text(
            json.dumps({"sources": sources or []}), encoding="utf-8"
        )
        (config_dir / "filters.yaml").write_text(filters, encoding="utf-8")

    def test_filtered_articles_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "type": "unit-filter",
                                "name": "unit-filter",
                                "enabled": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "filters.yaml").write_text(
                "exclude:\n"
                "  keywords:\n"
                "    - rejected\n",
                encoding="utf-8",
            )

            stats = run_producer(root=root)

            self.assertEqual(stats["stored"], 1)
            self.assertEqual(stats["filtered"], 1)
            self.assertTrue((root / "inbox" / "keep.json").exists())
            self.assertFalse((root / "inbox" / "drop.json").exists())
            self.assertFalse((root / "metadata" / "articles" / "drop.json").exists())

            index = json.loads((root / "metadata" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in index["articles"]], ["keep"])

    def test_refilter_archives_rejected_and_logs_to_console_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(
                root,
                "include:\n"
                "  countries:\n"
                "    - China\n"
                "  institutions: []\n"
                "unknown_policy: exclude\n",
            )
            (root / "config" / "institution_country.yaml").write_text(
                "tsinghua university: China\n"
                "stanford university: United States\n",
                encoding="utf-8",
            )
            storage = ArticleStorage(root)
            storage.save(
                Article(
                    id="keep-existing",
                    type="paper",
                    source="arxiv",
                    title="Keep",
                    metadata={
                        "affiliation_source": "arxiv_html",
                        "affiliation_source_format": "structured",
                        "affiliation_source_text": "Alice, Tsinghua University",
                    },
                )
            )
            storage.save(
                Article(
                    id="archive-existing",
                    type="paper",
                    source="arxiv",
                    title="Archive",
                    status=ArticleStatus(favorite=True),
                    attachments=[
                        Attachment(type="pdf", path="attachments/pdf/archive-existing.pdf")
                    ],
                    metadata={
                        "affiliation_source": "arxiv_html",
                        "affiliation_source_format": "structured",
                        "affiliation_source_text": "Bob, Stanford University",
                    },
                )
            )

            console = StringIO()
            with redirect_stdout(console):
                stats = run_producer(root=root, refilter_existing=True)

            self.assertEqual(stats["stored"], 1)
            self.assertEqual(stats["filtered"], 1)
            self.assertEqual(stats["archived"], 1)
            self.assertTrue((root / "filtered" / "archive-existing.json").exists())
            self.assertFalse(
                (root / "metadata" / "articles" / "archive-existing.json").exists()
            )
            archived = json.loads(
                (root / "filtered" / "archive-existing.json").read_text(encoding="utf-8")
            )
            self.assertTrue(archived["status"]["favorite"])
            self.assertEqual(archived["attachments"][0]["type"], "pdf")
            self.assertIn("stage=filter_check", console.getvalue())
            self.assertIn("status=ARCHIVED", console.getvalue())
            file_log = (root / "logs" / "producer.log").read_text(encoding="utf-8")
            self.assertIn("stage=filter_check", file_log)
            self.assertIn("status=ARCHIVED", file_log)

    def test_refilter_dry_run_does_not_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(
                root,
                "include:\n"
                "  countries:\n"
                "    - China\n"
                "unknown_policy: exclude\n",
            )
            storage = ArticleStorage(root)
            storage.save(
                Article(
                    id="unknown",
                    type="paper",
                    source="arxiv",
                    title="Unknown",
                    metadata={
                        "affiliation_source": "arxiv_html",
                        "affiliation_source_format": "structured",
                        "affiliation_source_text": "Alice, Unknown Lab",
                    },
                )
            )
            stats = run_producer(root=root, refilter_existing=True, dry_run=True)
            self.assertEqual(stats["filtered"], 1)
            self.assertEqual(stats["archived"], 0)
            self.assertTrue((root / "metadata" / "articles" / "unknown.json").exists())
            self.assertFalse((root / "filtered" / "unknown.json").exists())


if __name__ == "__main__":
    unittest.main()
