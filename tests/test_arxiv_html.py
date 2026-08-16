import unittest

from airhub.media.arxiv_html import (
    extract_affiliation_text,
    extract_media_manifest,
    extract_visible_text,
    repair_media_manifest_urls,
)


class ArxivHtmlTest(unittest.TestCase):
    def test_extract_visible_text_skips_non_content_blocks(self):
        raw = """
        <html>
          <head><title>Hidden Title</title></head>
          <body>
            <style>.hidden { display: none; }</style>
            <script>const affiliation = "Do Not Include University";</script>
            <h1>Paper Title</h1>
            <div class="ltx_authors">Alice, Tsinghua University</div>
          </body>
        </html>
        """
        text = extract_visible_text(raw)
        self.assertIn("Paper Title", text)
        self.assertIn("Tsinghua University", text)
        self.assertNotIn("Do Not Include University", text)
        self.assertNotIn("Hidden Title", text)

    def test_extract_affiliation_text_ignores_document_body(self):
        raw = """
        <html><body>
          <div class="ltx_authors">
            <span class="ltx_personname">Alice</span><br>
            <span class="ltx_affiliation">Massachusetts Institute of Technology</span>
          </div>
          <p>Submit without GitHub. This body mentions Stanford University.</p>
        </body></html>
        """
        text = extract_affiliation_text(raw)
        self.assertIn("Alice", text)
        self.assertIn("Massachusetts Institute of Technology", text)
        self.assertNotIn("Submit", text)
        self.assertNotIn("Stanford University", text)

    def test_extract_affiliation_text_returns_empty_without_author_block(self):
        self.assertEqual(extract_affiliation_text("<p>Stanford University</p>"), "")

    def test_media_url_does_not_duplicate_arxiv_id(self):
        raw = """
        <figure><img src="2608.08021v1/x1.png"><figcaption>Figure 1</figcaption></figure>
        <figure><img src="x2.png"><figcaption>Figure 2</figcaption></figure>
        """
        manifest = extract_media_manifest(raw, "https://arxiv.org/html/2608.08021v1")
        self.assertEqual(
            [item["src"] for item in manifest],
            [
                "https://arxiv.org/html/2608.08021v1/x1.png",
                "https://arxiv.org/html/2608.08021v1/x2.png",
            ],
        )

    def test_old_cached_media_url_is_repaired(self):
        repaired = repair_media_manifest_urls(
            [
                {
                    "type": "image",
                    "src": "https://arxiv.org/html/2608.08021v1/2608.08021v1/x1.png",
                }
            ],
            "2608.08021v1",
        )
        self.assertEqual(
            repaired[0]["src"],
            "https://arxiv.org/html/2608.08021v1/x1.png",
        )


if __name__ == "__main__":
    unittest.main()
