import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from airhub.config import SourceConfig
from airhub.fetchers.blog import BlogFetcher


class BlogFetcherTest(unittest.TestCase):
    def test_rss_entry_to_article(self):
        xml = """<rss><channel><item>
        <title>Post Title</title>
        <link>https://example.com/post</link>
        <pubDate>Thu, 09 Jul 2026 00:00:00 GMT</pubDate>
        <author>Alice</author>
        <category>AI</category>
        <description>Summary text</description>
        </item></channel></rss>"""
        root = ET.fromstring(xml)
        fetcher = BlogFetcher(
            SourceConfig(type="blog", name="test", options={"tags": ["research"]}),
            Path("."),
        )
        entry = fetcher._rss_entries(root)[0]
        article = fetcher._entry_to_article(entry, "https://example.com/feed.xml")
        self.assertEqual(article.type, "blog")
        self.assertEqual(article.source, "test")
        self.assertEqual(article.title, "Post Title")
        self.assertEqual(article.url, "https://example.com/post")
        self.assertIn("AI", article.tags)
        self.assertIn("research", article.tags)
        self.assertEqual(article.metadata["abstract"], "Summary text")


if __name__ == "__main__":
    unittest.main()
