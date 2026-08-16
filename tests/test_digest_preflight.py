from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from airhub.digest_preflight import validate_digest_package
from airhub.models import Article, Attachment


class DigestPreflightTest(unittest.TestCase):
    def test_accepts_local_source_and_local_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "attachments" / "source" / "paper.html"
            image = root / "attachments" / "image" / "figure.png"
            source.parent.mkdir(parents=True)
            image.parent.mkdir(parents=True)
            source.write_text("<html></html>", encoding="utf-8")
            image.write_bytes(b"PNG")
            article = Article(
                id="paper",
                type="paper",
                source="arxiv",
                title="Paper",
                attachments=[Attachment(type="html", path="attachments/source/paper.html")],
                metadata={
                    "source_html_sufficient": True,
                    "media_manifest": [
                        {"type": "image", "src": "attachments/image/figure.png"},
                        {"type": "table", "html": "<table></table>"},
                    ],
                },
            )

            report = validate_digest_package(article, root)

            self.assertEqual(report.local_images, 1)
            self.assertEqual(report.tables, 1)
            self.assertTrue(report.source_html_sufficient)

    def test_rejects_remote_image_before_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "attachments" / "paper.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"PDF")
            article = Article(
                id="paper",
                type="paper",
                source="arxiv",
                title="Paper",
                attachments=[Attachment(type="pdf", path="attachments/paper.pdf")],
                metadata={
                    "media_manifest": [
                        {"type": "image", "src": "https://arxiv.org/html/paper/figure.png"}
                    ]
                },
            )

            with self.assertRaisesRegex(ValueError, "网络图片未本地化"):
                validate_digest_package(article, root)

    def test_rejects_path_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            article = Article(
                id="paper",
                type="paper",
                source="arxiv",
                title="Paper",
                attachments=[Attachment(type="pdf", path="../outside.pdf")],
            )

            with self.assertRaisesRegex(ValueError, "本地 HTML 原文或 PDF"):
                validate_digest_package(article, root)


if __name__ == "__main__":
    unittest.main()
