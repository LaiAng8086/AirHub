"""Extract media references from arxiv HTML prepared by the producer."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse


@dataclass
class MediaItem:
    type: str
    src: str
    caption: str = ""
    html: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"type": self.type, "src": self.src}
        if self.caption:
            payload["caption"] = self.caption
        if self.html:
            payload["html"] = self.html
        return payload


FIGURE_RE = re.compile(r"<figure\b.*?</figure>", re.IGNORECASE | re.DOTALL)
IMG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
CAPTION_RE = re.compile(r"<figcaption\b[^>]*>(.*?)</figcaption>", re.IGNORECASE | re.DOTALL)
TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
IFRAME_RE = re.compile(r"<iframe\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>.*?</iframe>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
NON_CONTENT_RE = re.compile(
    r"<(script|style|noscript|svg|head)\b.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
WHITESPACE_RE = re.compile(r"\s+")
AFFILIATION_CLASSES = {
    "ltx_affiliation",
    "ltx_authors",
    "ltx_contact",
    "ltx_creator",
    "ltx_role_affiliation",
}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class _AffiliationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capture_depth = 0
        self.ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "head"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        classes = dict(attrs).get("class", "") or ""
        class_names = set(classes.split())
        if self.capture_depth or class_names & AFFILIATION_CLASSES:
            if tag.lower() not in VOID_TAGS:
                self.capture_depth += 1
            if tag.lower() in {"br", "hr"}:
                self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_depth:
            if tag.lower() in {"script", "style", "noscript", "svg", "head"}:
                self.ignored_depth -= 1
            return
        if self.capture_depth:
            self.capture_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.capture_depth and not self.ignored_depth:
            self.parts.append(data)


def strip_tags(value: str) -> str:
    return html.unescape(TAG_RE.sub(" ", value)).strip()


def extract_visible_text(arxiv_html: str, max_chars: int = 20000) -> str:
    """Extract normalized visible text for server-side affiliation filtering."""

    without_noise = NON_CONTENT_RE.sub(" ", arxiv_html)
    text = strip_tags(without_noise)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_chars]


def extract_affiliation_text(arxiv_html: str, max_chars: int = 20000) -> str:
    """Extract author and affiliation blocks without searching the paper body."""

    parser = _AffiliationParser()
    parser.feed(arxiv_html)
    parser.close()
    return WHITESPACE_RE.sub(" ", " ".join(parser.parts)).strip()[:max_chars]


def resolve_arxiv_asset_url(base_url: str, raw_src: str) -> str:
    """Resolve LaTeXML assets and remove the duplicated article-id segment."""

    resolved = urljoin(base_url.rstrip("/") + "/", html.unescape(raw_src).strip())
    base = urlparse(base_url)
    article_id = base.path.rstrip("/").rsplit("/", 1)[-1]
    if not article_id:
        return resolved
    parsed = urlparse(resolved)
    duplicate = f"/html/{article_id}/{article_id}/"
    corrected_path = parsed.path.replace(duplicate, f"/html/{article_id}/", 1)
    return urlunparse(parsed._replace(path=corrected_path))


def repair_media_manifest_urls(
    manifest: list[dict[str, str]],
    arxiv_id: str,
) -> list[dict[str, str]]:
    """Repair bad URLs already stored by older AirHub cache versions."""

    base_url = f"https://arxiv.org/html/{arxiv_id}"
    repaired: list[dict[str, str]] = []
    for raw_item in manifest:
        item = dict(raw_item)
        src = str(item.get("src", ""))
        if src:
            item["src"] = resolve_arxiv_asset_url(base_url, src)
        repaired.append(item)
    return repaired


def count_document_sections(arxiv_html: str) -> int:
    return len(re.findall(r"<section\b", arxiv_html, re.IGNORECASE))


def extract_media_manifest(arxiv_html: str, base_url: str) -> list[dict[str, str]]:
    items: list[MediaItem] = []
    for figure_match in FIGURE_RE.finditer(arxiv_html):
        figure_html = figure_match.group(0)
        caption_match = CAPTION_RE.search(figure_html)
        caption = strip_tags(caption_match.group(1)) if caption_match else ""
        for image_match in IMG_RE.finditer(figure_html):
            items.append(
                MediaItem(
                    type="image",
                    src=resolve_arxiv_asset_url(base_url, image_match.group(1)),
                    caption=caption,
                )
            )
    for table_match in TABLE_RE.finditer(arxiv_html):
        table_html = table_match.group(0)
        items.append(MediaItem(type="table", src="", html=table_html))
    for iframe_match in IFRAME_RE.finditer(arxiv_html):
        items.append(
            MediaItem(type="video", src=resolve_arxiv_asset_url(base_url, iframe_match.group(1)))
        )
    return [item.to_dict() for item in items]
