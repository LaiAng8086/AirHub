from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from airhub.config import SourceConfig
from airhub.fetchers.arxiv import ArxivFetcher, normalize_arxiv_id
from airhub.models import Article


ENTRY = """
<entry xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>http://arxiv.org/abs/2608.00001v1</id>
  <published>2026-08-11T00:00:00Z</published>
  <title>Staged Fetch</title>
  <summary>Abstract.</summary>
  <author><name>Alice Smith</name><arxiv:affiliation>Example University</arxiv:affiliation></author>
  <category term="cs.AI"/>
  <link title="pdf" type="application/pdf" href="https://arxiv.org/pdf/2608.00001v1"/>
</entry>
"""

OAI_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-08-13T00:00:00Z</responseDate>
  <ListRecords>
    <record>
      <header><identifier>oai:arXiv.org:2608.11200</identifier><datestamp>2026-08-12</datestamp></header>
      <metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
          <id>2608.11200</id>
          <version version="v1"><date>Tue, 11 Aug 2026 17:00:00 GMT</date></version>
          <title>AI and Robotics</title><authors>Alice Smith, Bob Jones</authors>
          <categories>cs.RO cs.AI</categories><abstract>Robotics abstract.</abstract>
        </arXivRaw>
      </metadata>
    </record>
    <record>
      <header><identifier>oai:arXiv.org:2608.11201</identifier><datestamp>2026-08-12</datestamp></header>
      <metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
          <id>2608.11201</id>
          <version version="v1"><date>Tue, 11 Aug 2026 18:00:00 GMT</date></version>
          <version version="v2"><date>Wed, 12 Aug 2026 18:00:00 GMT</date></version>
          <title>AI and Vision</title><authors>Carol Lee and Dan Wu</authors>
          <categories>cs.AI cs.CV</categories><abstract>Vision abstract.</abstract>
        </arXivRaw>
      </metadata>
    </record>
    <record>
      <header><identifier>oai:arXiv.org:2608.11202</identifier><datestamp>2026-08-12</datestamp></header>
      <metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
          <id>2608.11202</id>
          <version version="v1"><date>Tue, 11 Aug 2026 19:00:00 GMT</date></version>
          <title>AI Only</title><authors>Eve Chen</authors>
          <categories>cs.AI</categories><abstract>Excluded abstract.</abstract>
        </arXivRaw>
      </metadata>
    </record>
  </ListRecords>
</OAI-PMH>
"""


class ArxivFetcherStagingTest(unittest.TestCase):
    def test_manual_id_normalization_accepts_ids_and_arxiv_urls(self):
        self.assertEqual(normalize_arxiv_id("arXiv:2608.00001v2"), "2608.00001v2")
        self.assertEqual(
            normalize_arxiv_id("https://arxiv.org/pdf/2608.00001.pdf"),
            "2608.00001",
        )
        self.assertEqual(normalize_arxiv_id("hep-th/9901001v1"), "hep-th/9901001v1")
        with self.assertRaisesRegex(ValueError, "无效的 arXiv 编号"):
            normalize_arxiv_id("not-an-id")

    def test_fetch_by_ids_uses_oai_for_records_missing_from_api_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = ArxivFetcher(SourceConfig(type="arxiv"), Path(tmp))
            first = Article(
                id="arxiv-2608.00001v1",
                type="paper",
                source="arxiv",
                title="First",
                metadata={"arxiv_id": "2608.00001v1"},
            )
            second = Article(
                id="arxiv-2608.00002v1",
                type="paper",
                source="arxiv",
                title="Second",
                metadata={"arxiv_id": "2608.00002v1"},
            )
            with patch.object(fetcher, "_fetch_api_ids", return_value=[first]), patch.object(
                fetcher, "_fetch_oai_id", return_value=second
            ) as oai, patch("airhub.fetchers.arxiv.time.sleep") as sleep:
                articles = fetcher.fetch_by_ids(["2608.00001v1", "2608.00002v1"])

            self.assertEqual([article.id for article in articles], [first.id, second.id])
            oai.assert_called_once_with("2608.00002v1")
            sleep.assert_called_once_with(3.0)

    def test_api_rate_limit_falls_back_to_oai_and_preserves_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = ArxivFetcher(
                SourceConfig(
                    type="arxiv",
                    name="arxiv-test",
                    options={
                        "query": "(cat:cs.AI AND cat:cs.CV) OR (cat:cs.AI AND cat:cs.RO)",
                        "oai_lookback_days": 7,
                    },
                ),
                Path(tmp),
            )
            limited = urllib.error.HTTPError(
                "https://export.arxiv.org/api/query", 429, "Rate exceeded", {}, None
            )
            with patch.object(
                fetcher, "_request", side_effect=[limited, OAI_RESPONSE]
            ) as request, patch("airhub.fetchers.arxiv.time.sleep") as sleep:
                articles = fetcher.fetch(limit=300)

            self.assertEqual(
                [article.id for article in articles],
                ["arxiv-2608.11201v2", "arxiv-2608.11200v1"],
            )
            self.assertEqual(articles[0].authors, ["Carol Lee", "Dan Wu"])
            self.assertEqual(articles[0].metadata["discovery_backend"], "oai")
            self.assertIn("set=cs%3Acs%3AAI", request.call_args_list[1].args[0])
            sleep.assert_called_once_with(3.0)

    def test_normalization_is_lightweight_and_reads_api_affiliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = ArxivFetcher(SourceConfig(type="arxiv"), Path(tmp))
            article = fetcher._entry_to_article(ET.fromstring(ENTRY))
            self.assertEqual(article.attachments, [])
            self.assertEqual(article.metadata["source_institutions"], ["Example University"])
            self.assertEqual(article.metadata["affiliation_source"], "arxiv_api")

    def test_api_normalization_preserves_legacy_category_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = ArxivFetcher(SourceConfig(type="arxiv"), Path(tmp))
            entry = ET.fromstring(ENTRY.replace("2608.00001v1", "hep-th/9901001v1"))
            article = fetcher._entry_to_article(entry)
            self.assertEqual(article.metadata["arxiv_id"], "hep-th/9901001v1")
            self.assertEqual(article.id, "arxiv-hep-th_9901001v1")

    def test_pdf_download_uses_date_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fetcher = ArxivFetcher(SourceConfig(type="arxiv"), root)
            article = Article(
                id="arxiv-2608.00001v1",
                type="paper",
                source="arxiv",
                title="Staged Fetch",
                metadata={"download_date": "2026-08-11"},
            )
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b"PDF")):
                path = fetcher._download_pdf("https://example.invalid/paper.pdf", article)
            self.assertEqual(
                path,
                root / "attachments" / "pdf" / "2026-08-11" / "arxiv-2608.00001v1.pdf",
            )
            self.assertEqual(path.read_bytes(), b"PDF")

    def test_sufficient_local_html_skips_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fetcher = ArxivFetcher(
                SourceConfig(
                    type="arxiv",
                    options={
                        "prepare_arxiv_html": True,
                        "download_pdf": True,
                        "html_min_content_chars": 100,
                    },
                ),
                root,
            )
            source_html = root / "cache" / "source.html"
            source_html.parent.mkdir(parents=True)
            source_html.write_text("<html></html>", encoding="utf-8")
            article = Article(
                id="arxiv-2608.00001v1",
                type="paper",
                source="arxiv",
                title="Staged Fetch",
                metadata={
                    "arxiv_id": "2608.00001v1",
                    "arxiv_html_prepared": True,
                    "arxiv_html_content_chars": 500,
                    "arxiv_html_section_count": 4,
                    "media_manifest": [],
                    "source_record": {"pdf_url": "https://example.invalid/paper.pdf"},
                    "download_date": "2026-08-11",
                },
            )
            with patch.object(fetcher, "_materialize_arxiv_html", return_value=source_html), patch.object(
                fetcher, "_download_pdf"
            ) as download_pdf:
                fetcher.prepare(article)
            download_pdf.assert_not_called()
            self.assertTrue(article.metadata["source_html_sufficient"])

    def test_pdf_figures_replace_remote_html_images_when_arxiv_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "attachments" / "paper.pdf"
            figure = root / "attachments" / "figure.png"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"PDF")
            figure.write_bytes(b"PNG")
            fetcher = ArxivFetcher(SourceConfig(type="arxiv"), root)
            article = Article(
                id="arxiv-2608.00001v1",
                type="paper",
                source="arxiv",
                title="Staged Fetch",
                metadata={
                    "arxiv_id": "2608.00001v1",
                    "arxiv_html_prepared": True,
                    "arxiv_html_content_chars": 500,
                    "arxiv_html_section_count": 4,
                    "media_manifest": [
                        {"type": "image", "src": "https://arxiv.org/html/2608.00001v1/x1.png"},
                        {"type": "table", "html": "<table></table>"},
                    ],
                    "source_record": {"pdf_url": "https://example.invalid/paper.pdf"},
                },
            )
            figures = [{"num": 1, "path": str(figure)}]
            with patch.object(fetcher, "_localize_media_images", side_effect=lambda value: value.metadata["media_manifest"]), patch.object(
                fetcher, "_materialize_arxiv_html", return_value=None
            ), patch.object(fetcher, "_download_pdf", return_value=pdf), patch.object(
                fetcher, "_prepare_pdf_figures", return_value=figures
            ):
                fetcher.prepare(article)

            self.assertEqual(article.metadata["media_manifest"], [{"type": "table", "html": "<table></table>"}])
            self.assertEqual(article.metadata["media_image_fallback"], "pdf_figures")
            self.assertEqual(article.metadata["media_remote_images_replaced"], 1)


if __name__ == "__main__":
    unittest.main()
