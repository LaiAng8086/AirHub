from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from airhub.blog_archive import (
    BlogArchiveError,
    BlogSiteStore,
    SelfContainedBlogArchiver,
    blog_archive_mode,
    canonical_blog_origin,
)


class FakeBlogArchiver(SelfContainedBlogArchiver):
    def __init__(self):
        super().__init__(timeout=1, retries=1)
        self.responses = {
            "https://blog.example/post": (
                b"""<!DOCTYPE html><html><head>
                <meta http-equiv="Content-Security-Policy" content="img-src 'self'">
                <link rel="stylesheet" href="/style.css">
                <script src="https://cdn.example/missing.js"></script>
                </head><body style="background:url('/bg.png')">
                <img src="/hero.png"><img data-src="/lazy.png">
                <a href="https://external.example/story">source</a>
                </body></html>""",
                "text/html",
                "https://blog.example/post",
            ),
            "https://blog.example/style.css": (
                b".card{background-image:url('/card.png')}",
                "text/css",
                "https://blog.example/style.css",
            ),
            "https://blog.example/bg.png": (b"bg", "image/png", "https://blog.example/bg.png"),
            "https://blog.example/hero.png": (b"hero", "image/png", "https://blog.example/hero.png"),
            "https://blog.example/lazy.png": (b"lazy", "image/png", "https://blog.example/lazy.png"),
            "https://blog.example/card.png": (b"card", "image/png", "https://blog.example/card.png"),
        }

    def _fetch(self, url: str):
        try:
            return self.responses[url]
        except KeyError as exc:
            raise BlogArchiveError(f"missing fixture: {url}") from exc


class BlogArchiveTest(unittest.TestCase):
    def test_archiver_embeds_html_css_images_and_removes_failed_optional_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot.html"
            result = FakeBlogArchiver().archive("https://blog.example/post", output)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("data:text/css", rendered)
            self.assertGreaterEqual(rendered.count("data:image/png;base64,"), 3)
            self.assertIn("removed remote Content-Security-Policy", rendered)
            self.assertIn('src="data:text/plain;base64,"', rendered)
            self.assertIn('href="https://external.example/story"', rendered)
            self.assertNotIn('src="https://', rendered)
            self.assertEqual(result.optional_resources_removed, 1)
            self.assertGreaterEqual(result.resources_embedded, 6)

    def test_blog_site_store_deduplicates_origins_and_deletes_by_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = BlogSiteStore(root)
            first, added_first = store.add("https://Example.COM/post/one")
            second, added_second = store.add("https://example.com/post/two")
            store.add("http://another.example:80/story")
            self.assertTrue(added_first)
            self.assertFalse(added_second)
            self.assertEqual(first.origin, "https://example.com")
            self.assertEqual(second.origin, "https://example.com")
            self.assertEqual(len(store.list()), 2)
            removed = store.remove_indexes([2, 2])
            self.assertEqual([item.origin for item in removed], ["http://another.example"])
            self.assertEqual(len(store.list()), 1)

    def test_canonical_origin_rejects_non_http_urls(self):
        with self.assertRaises(ValueError):
            canonical_blog_origin("file:///etc/passwd")
        with self.assertRaises(ValueError):
            canonical_blog_origin("http://127.0.0.1/private")
        with self.assertRaises(ValueError):
            canonical_blog_origin("https://user:pass@example.com/private")

    def test_archive_mode_skips_code_model_hubs_but_keeps_github_io_static_pages(self):
        self.assertEqual(
            blog_archive_mode("https://github.com/org/repository"), "catalog_only"
        )
        self.assertEqual(
            blog_archive_mode("https://huggingface.co/org/model"), "catalog_only"
        )
        self.assertEqual(
            blog_archive_mode("https://author.github.io/project/"), "self_contained"
        )
        self.assertEqual(
            blog_archive_mode("https://personal.example/posts/one"), "self_contained"
        )


if __name__ == "__main__":
    unittest.main()
