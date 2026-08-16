"""RSS/Atom blog source fetcher."""

from __future__ import annotations

import hashlib
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

from airhub.models import Article, Attachment
from airhub.paths import relative_to_root

from .base import Fetcher


ATOM = "{http://www.w3.org/2005/Atom}"


class BlogFetcher(Fetcher):
    source_type = "blog"

    def fetch(self, limit: int | None = None) -> list[Article]:
        rss_url = str(self.source.options.get("rss", "")).strip()
        if not rss_url:
            self._event("WARN", "fetch", "rss_url_missing=true")
            return []
        self._event("START", "fetch", f"url={rss_url}")
        raw = urllib.request.urlopen(rss_url, timeout=90).read()
        root = ET.fromstring(raw)
        entries = self._atom_entries(root) or self._rss_entries(root)
        if limit:
            entries = entries[:limit]
        articles = [self._entry_to_article(entry, rss_url) for entry in entries]
        self._event("DONE", "fetch", f"entries={len(entries)} response_bytes={len(raw)}")
        return articles

    def prepare(self, article: Article) -> Article:
        article.metadata.setdefault("download_date", date.today().isoformat())
        if self.source.options.get("snapshot", False):
            self._snapshot(article)
        return article

    def _entry_to_article(self, entry: ET.Element, feed_url: str) -> Article:
        title = self._first_text(entry, ["title"])
        url = self._link(entry)
        stable = hashlib.sha1((url or title).encode("utf-8")).hexdigest()[:16]
        published = self._first_text(entry, ["published", "updated", "pubDate", "dc:date"])
        authors = self._authors(entry)
        tags = list(dict.fromkeys(self.source.options.get("tags", []) + self._categories(entry)))
        summary = self._first_text(entry, ["summary", "description", "content"])
        article = Article(
            id=f"blog-{stable}",
            type="blog",
            source=self.source.name or "blog",
            title=re.sub(r"\s+", " ", title).strip(),
            authors=authors,
            publish_date=published,
            url=url,
            tags=[str(item) for item in tags if item],
            metadata={
                "feed_url": feed_url,
                "abstract": re.sub(r"\s+", " ", summary).strip(),
                "media_manifest": [],
                "affiliations": [],
                "institutions": [],
                "countries": [],
            },
        )
        self._event("DONE", "normalize", f"article={article.id} title={article.title!r}")
        return article

    def _snapshot(self, article: Article) -> None:
        try:
            self._event("START", "blog_snapshot", f"article={article.id} url={article.url}")
            data = urllib.request.urlopen(article.url, timeout=90).read()
        except Exception as exc:
            self._event("WARN", "blog_snapshot", f"article={article.id} error={exc}")
            return
        out_path = (
            self.root
            / "attachments"
            / "blog"
            / str(article.metadata["download_date"])
            / f"{article.id}.html"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        article.attachments.append(
            Attachment(type="html_snapshot", path=relative_to_root(out_path, self.root), title="Source snapshot")
        )
        self._event("DONE", "blog_snapshot", f"article={article.id} bytes={len(data)}")

    def _atom_entries(self, root: ET.Element) -> list[ET.Element]:
        if root.tag == f"{ATOM}feed":
            return root.findall(f"{ATOM}entry")
        return []

    def _rss_entries(self, root: ET.Element) -> list[ET.Element]:
        channel = root.find("channel")
        if channel is None:
            return []
        return channel.findall("item")

    def _first_text(self, entry: ET.Element, names: list[str]) -> str:
        for name in names:
            found = entry.find(name)
            if found is None:
                found = entry.find(f"{ATOM}{name}")
            if found is not None and found.text:
                return found.text
        return ""

    def _link(self, entry: ET.Element) -> str:
        atom_link = entry.find(f"{ATOM}link")
        if atom_link is not None and atom_link.attrib.get("href"):
            return atom_link.attrib["href"]
        link = entry.find("link")
        return link.text.strip() if link is not None and link.text else ""

    def _authors(self, entry: ET.Element) -> list[str]:
        names = []
        for author in entry.findall(f"{ATOM}author"):
            name = author.find(f"{ATOM}name")
            if name is not None and name.text:
                names.append(name.text)
        rss_author = entry.find("author")
        if rss_author is not None and rss_author.text:
            names.append(rss_author.text)
        return names

    def _categories(self, entry: ET.Element) -> list[str]:
        values = []
        for category in entry.findall("category") + entry.findall(f"{ATOM}category"):
            value = category.attrib.get("term") or category.text or ""
            if value:
                values.append(value)
        return values
