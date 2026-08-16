"""ArXiv source fetcher."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from airhub.models import Article, Attachment
from airhub.paths import relative_to_root
from airhub.media.arxiv_html import (
    count_document_sections,
    extract_affiliation_text,
    extract_media_manifest,
    extract_visible_text,
    repair_media_manifest_urls,
)
from airhub.media.pdf_figures import extract_pdf_figures
from airhub.media.pdf_text import extract_pdf_first_page_text

from .base import Fetcher


ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_OAI = "https://oaipmh.arxiv.org/oai"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
ARXIV_RAW = "{http://arxiv.org/OAI/arXivRaw/}"
ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
CATEGORY_TOKEN_RE = re.compile(r"cat:[A-Za-z0-9._-]+|AND|OR|[()]", re.IGNORECASE)
MODERN_ARXIV_ID_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?", re.IGNORECASE)
LEGACY_ARXIV_ID_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9.-]*/\d{7}(?:v\d+)?", re.IGNORECASE
)


def normalize_arxiv_id(value: str) -> str:
    """Normalize a bare arXiv identifier or a conventional arXiv URL."""

    normalized = value.strip()
    normalized = re.sub(r"^arxiv:\s*", "", normalized, flags=re.IGNORECASE)
    if normalized.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(normalized)
        path = urllib.parse.unquote(parsed.path).strip("/")
        if path.startswith(("abs/", "pdf/", "html/")):
            normalized = path.split("/", 1)[1]
        else:
            normalized = path
    normalized = normalized.removesuffix(".pdf").strip().strip("/")
    if not (
        MODERN_ARXIV_ID_RE.fullmatch(normalized)
        or LEGACY_ARXIV_ID_RE.fullmatch(normalized)
    ):
        raise ValueError(f"无效的 arXiv 编号: {value!r}")
    return normalized


def arxiv_base_id(value: str) -> str:
    return re.sub(r"v\d+$", "", normalize_arxiv_id(value), flags=re.IGNORECASE)


def _parse_category_query(query: str) -> Any:
    """Parse the category-only boolean subset supported by the OAI fallback."""

    tokens: list[str] = []
    position = 0
    while position < len(query):
        if query[position:].strip() == "":
            break
        match = CATEGORY_TOKEN_RE.match(query, position)
        if match is None or query[position : match.start()].strip():
            raise ValueError(f"OAI fallback does not support arXiv query: {query}")
        token = match.group(0)
        tokens.append(token.upper() if token.upper() in {"AND", "OR"} else token)
        position = match.end()
        while position < len(query) and query[position].isspace():
            position += 1
    if not tokens:
        raise ValueError("OAI fallback requires a category query")

    index = 0

    def parse_atom() -> Any:
        nonlocal index
        if index >= len(tokens):
            raise ValueError(f"Incomplete arXiv category query: {query}")
        token = tokens[index]
        if token == "(":
            index += 1
            node = parse_or()
            if index >= len(tokens) or tokens[index] != ")":
                raise ValueError(f"Unbalanced arXiv category query: {query}")
            index += 1
            return node
        if token.lower().startswith("cat:"):
            index += 1
            return ("cat", token[4:])
        raise ValueError(f"Unexpected token {token!r} in arXiv category query")

    def parse_and() -> Any:
        nonlocal index
        node = parse_atom()
        while index < len(tokens) and tokens[index] == "AND":
            index += 1
            node = ("and", node, parse_atom())
        return node

    def parse_or() -> Any:
        nonlocal index
        node = parse_and()
        while index < len(tokens) and tokens[index] == "OR":
            index += 1
            node = ("or", node, parse_and())
        return node

    parsed = parse_or()
    if index != len(tokens):
        raise ValueError(f"Unexpected token {tokens[index]!r} in arXiv category query")
    return parsed


def _matches_category_query(node: Any, categories: set[str]) -> bool:
    operator = node[0]
    if operator == "cat":
        return node[1].lower() in categories
    if operator == "and":
        return _matches_category_query(node[1], categories) and _matches_category_query(
            node[2], categories
        )
    return _matches_category_query(node[1], categories) or _matches_category_query(
        node[2], categories
    )


def _mandatory_categories(node: Any) -> set[str]:
    operator = node[0]
    if operator == "cat":
        return {node[1]}
    left = _mandatory_categories(node[1])
    right = _mandatory_categories(node[2])
    return left | right if operator == "and" else left & right


def _oai_set_spec(category: str) -> str:
    archive, separator, subject = category.partition(".")
    if not separator or not archive or not subject:
        return ""
    return f"{archive}:{archive}:{subject}"


class ArxivFetcher(Fetcher):
    source_type = "arxiv"

    def fetch(self, limit: int | None = None) -> list[Article]:
        max_results = limit or int(self.source.options.get("max_results", 10))
        query = str(self.source.options.get("query", "cat:cs.AI"))
        try:
            return self._fetch_api(max_results, query)
        except Exception as exc:
            if not self.source.options.get("oai_fallback", True):
                raise
            self._event(
                "WARN",
                "fetch",
                f"backend=api action=fallback error={type(exc).__name__}: {exc}",
            )
            time.sleep(ARXIV_REQUEST_INTERVAL_SECONDS)
            return self._fetch_oai(max_results, query)

    def fetch_by_ids(self, identifiers: list[str]) -> list[Article]:
        """Fetch explicit arXiv records while preserving the requested order."""

        requested = list(dict.fromkeys(normalize_arxiv_id(item) for item in identifiers))
        if not requested:
            return []
        batch_size = max(1, min(50, int(self.source.options.get("id_batch_size", 50))))
        fetched: list[Article] = []
        for offset in range(0, len(requested), batch_size):
            batch = requested[offset : offset + batch_size]
            if offset:
                time.sleep(ARXIV_REQUEST_INTERVAL_SECONDS)
            api_articles: list[Article] = []
            oai_identifiers: list[str]
            try:
                api_articles = self._fetch_api_ids(batch)
                fetched.extend(api_articles)
                exact, bases = self._article_identifier_sets(api_articles)
                oai_identifiers = [
                    identifier
                    for identifier in batch
                    if not self._identifier_is_returned(identifier, exact, bases)
                ]
                if oai_identifiers:
                    self._event(
                        "WARN",
                        "fetch_ids",
                        f"backend=api action=oai_missing missing={len(oai_identifiers)}",
                    )
            except Exception as exc:
                if not self.source.options.get("oai_fallback", True):
                    raise
                self._event(
                    "WARN",
                    "fetch_ids",
                    f"backend=api action=fallback ids={len(batch)} "
                    f"error={type(exc).__name__}: {exc}",
                )
                oai_identifiers = batch
            if oai_identifiers and self.source.options.get("oai_fallback", True):
                time.sleep(ARXIV_REQUEST_INTERVAL_SECONDS)
                for index, identifier in enumerate(oai_identifiers):
                    if index:
                        time.sleep(ARXIV_REQUEST_INTERVAL_SECONDS)
                    article = self._fetch_oai_id(identifier)
                    if article is not None:
                        fetched.append(article)

        exact = {
            normalize_arxiv_id(str(article.metadata.get("arxiv_id", ""))): article
            for article in fetched
            if article.metadata.get("arxiv_id")
        }
        by_base: dict[str, Article] = {}
        for identifier, article in exact.items():
            by_base.setdefault(arxiv_base_id(identifier), article)
        ordered: list[Article] = []
        seen_article_ids: set[str] = set()
        for identifier in requested:
            article = exact.get(identifier)
            if article is None and not re.search(r"v\d+$", identifier, re.IGNORECASE):
                article = by_base.get(arxiv_base_id(identifier))
            if article is not None and article.id not in seen_article_ids:
                ordered.append(article)
                seen_article_ids.add(article.id)
        return ordered

    @staticmethod
    def _article_identifier_sets(articles: list[Article]) -> tuple[set[str], set[str]]:
        exact = {
            normalize_arxiv_id(str(article.metadata.get("arxiv_id", "")))
            for article in articles
            if article.metadata.get("arxiv_id")
        }
        return exact, {arxiv_base_id(identifier) for identifier in exact}

    @staticmethod
    def _identifier_is_returned(
        identifier: str,
        exact: set[str],
        bases: set[str],
    ) -> bool:
        if identifier in exact:
            return True
        return not re.search(r"v\d+$", identifier, re.IGNORECASE) and arxiv_base_id(
            identifier
        ) in bases

    def _fetch_api_ids(self, identifiers: list[str]) -> list[Article]:
        params = {"id_list": ",".join(identifiers), "max_results": len(identifiers)}
        url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
        self._event("START", "fetch_ids", f"backend=api ids={len(identifiers)}")
        raw = self._request(url, timeout=180)
        root = ET.fromstring(raw)
        entries = root.findall(f"{ATOM}entry")
        if any("/api/errors" in self._text(entry, f"{ATOM}id") for entry in entries):
            message = self._text(entries[0], f"{ATOM}summary") if entries else "unknown error"
            raise RuntimeError(f"arXiv API returned an error feed: {message}")
        self._event(
            "DONE",
            "fetch_ids",
            f"backend=api entries={len(entries)} response_bytes={len(raw)}",
        )
        return [
            self._entry_to_article(entry, index=index, total=len(entries))
            for index, entry in enumerate(entries, start=1)
        ]

    def _fetch_oai_id(self, identifier: str) -> Article | None:
        base_id = arxiv_base_id(identifier)
        params = {
            "verb": "GetRecord",
            "identifier": f"oai:arXiv.org:{base_id}",
            "metadataPrefix": "arXivRaw",
        }
        url = f"{ARXIV_OAI}?{urllib.parse.urlencode(params)}"
        self._event("START", "fetch_ids", f"backend=oai id={identifier}")
        raw = self._request(url, timeout=180)
        root = ET.fromstring(raw)
        error = root.find(f".//{OAI}error")
        if error is not None:
            self._event(
                "WARN",
                "fetch_ids",
                f"backend=oai id={identifier} code={error.attrib.get('code', 'unknown')}",
            )
            return None
        record = root.find(f".//{OAI}record")
        if record is None:
            return None
        header = record.find(f"{OAI}header")
        entry = record.find(f"{OAI}metadata/{ARXIV_RAW}arXivRaw")
        if header is None or entry is None or header.attrib.get("status") == "deleted":
            return None
        article = self._oai_entry_to_article(entry, header)
        self._event("DONE", "fetch_ids", f"backend=oai id={identifier}")
        return article

    def _request(self, url: str, *, timeout: int) -> bytes:
        user_agent = str(
            self.source.options.get(
                "user_agent", "AirHub/1.0 (arXiv metadata research client)"
            )
        )
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/atom+xml, application/xml", "User-Agent": user_agent},
        )
        return urllib.request.urlopen(request, timeout=timeout).read()

    def _fetch_api(self, max_results: int, query: str) -> list[Article]:
        url = f"{ARXIV_API}?{urllib.parse.urlencode({'search_query': query, 'sortBy': 'submittedDate', 'sortOrder': 'descending', 'max_results': max_results})}"
        self._event(
            "START", "fetch", f"backend=api max_results={max_results} query={query!r}"
        )
        raw = self._request(url, timeout=60)
        root = ET.fromstring(raw)
        entries = root.findall(f"{ATOM}entry")
        if any("/api/errors" in self._text(entry, f"{ATOM}id") for entry in entries):
            message = self._text(entries[0], f"{ATOM}summary") if entries else "unknown error"
            raise RuntimeError(f"arXiv API returned an error feed: {message}")
        self._event(
            "DONE",
            "fetch",
            f"backend=api entries={len(entries)} response_bytes={len(raw)}",
        )
        return [
            self._entry_to_article(entry, index=index, total=len(entries))
            for index, entry in enumerate(entries, start=1)
        ]

    def _fetch_oai(self, max_results: int, query: str) -> list[Article]:
        parsed_query = _parse_category_query(query)
        mandatory = sorted(_mandatory_categories(parsed_query))
        set_spec = _oai_set_spec(mandatory[0]) if mandatory else ""
        lookback_days = max(1, int(self.source.options.get("oai_lookback_days", 30)))
        start_date = (date.today() - timedelta(days=lookback_days)).isoformat()
        params = {
            "verb": "ListRecords",
            "metadataPrefix": "arXivRaw",
            "from": start_date,
        }
        if set_spec:
            params["set"] = set_spec
        url = f"{ARXIV_OAI}?{urllib.parse.urlencode(params)}"
        articles_by_id: dict[str, Article] = {}
        page = 0
        response_bytes = 0
        max_pages = max(1, int(self.source.options.get("oai_max_pages", 20)))
        self._event(
            "START",
            "fetch",
            f"backend=oai max_results={max_results} from={start_date} set={set_spec or 'all'}",
        )

        while url:
            page += 1
            if page > max_pages:
                raise RuntimeError(f"arXiv OAI exceeded {max_pages} response pages")
            raw = self._request(url, timeout=180)
            response_bytes += len(raw)
            root = ET.fromstring(raw)
            error = root.find(f".//{OAI}error")
            if error is not None:
                raise RuntimeError(
                    f"arXiv OAI error {error.attrib.get('code', 'unknown')}: "
                    f"{(error.text or '').strip()}"
                )
            for record in root.findall(f".//{OAI}record"):
                header = record.find(f"{OAI}header")
                if header is None or header.attrib.get("status") == "deleted":
                    continue
                raw_entry = record.find(f"{OAI}metadata/{ARXIV_RAW}arXivRaw")
                if raw_entry is None:
                    continue
                categories = {
                    item.lower()
                    for item in self._text(raw_entry, f"{ARXIV_RAW}categories").split()
                    if item
                }
                if not _matches_category_query(parsed_query, categories):
                    continue
                article = self._oai_entry_to_article(raw_entry, header)
                articles_by_id[article.id] = article
            token_node = root.find(f".//{OAI}resumptionToken")
            token = (token_node.text or "").strip() if token_node is not None else ""
            if not token:
                break
            time.sleep(ARXIV_REQUEST_INTERVAL_SECONDS)
            url = f"{ARXIV_OAI}?{urllib.parse.urlencode({'verb': 'ListRecords', 'resumptionToken': token})}"

        articles = sorted(
            articles_by_id.values(),
            key=lambda article: (article.publish_date, article.id),
            reverse=True,
        )[:max_results]
        self._event(
            "DONE",
            "fetch",
            f"backend=oai entries={len(articles)} pages={page} response_bytes={response_bytes}",
        )
        return articles

    def _oai_entry_to_article(self, entry: ET.Element, header: ET.Element) -> Article:
        base_id = self._text(entry, f"{ARXIV_RAW}id").strip()
        versions = entry.findall(f"{ARXIV_RAW}version")
        latest = versions[-1] if versions else None
        version = str(latest.attrib.get("version", "")) if latest is not None else ""
        arxiv_id = f"{base_id}{version}"
        raw_date = self._text(latest, f"{ARXIV_RAW}date") if latest is not None else ""
        try:
            publish_date = parsedate_to_datetime(raw_date).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            publish_date = raw_date
        title = re.sub(r"\s+", " ", self._text(entry, f"{ARXIV_RAW}title")).strip()
        summary = re.sub(r"\s+", " ", self._text(entry, f"{ARXIV_RAW}abstract")).strip()
        categories = [
            item
            for item in self._text(entry, f"{ARXIV_RAW}categories").split()
            if item
        ]
        authors_text = re.sub(
            r"\s+", " ", self._text(entry, f"{ARXIV_RAW}authors")
        ).strip()
        authors = [
            item.strip()
            for item in re.split(r"\s*,\s*|\s+and\s+", authors_text)
            if item.strip()
        ]
        article_id = f"arxiv-{arxiv_id.replace('/', '_')}"
        return Article(
            id=article_id,
            type="paper",
            source="arxiv",
            title=title,
            authors=authors,
            publish_date=publish_date,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            tags=categories,
            attachments=[],
            summary="",
            metadata={
                "arxiv_id": arxiv_id,
                "abstract": summary,
                "categories": categories,
                "source_record": {
                    "api_id": f"https://arxiv.org/abs/{arxiv_id}",
                    "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
                },
                "media_manifest": [],
                "affiliation_source": "",
                "affiliation_source_text": "",
                "affiliations": [],
                "institutions": [],
                "countries": [],
                "source_institutions": [],
                "discovery_backend": "oai",
                "oai_datestamp": self._text(header, f"{OAI}datestamp"),
            },
        )

    def _entry_to_article(
        self,
        entry: ET.Element,
        index: int | None = None,
        total: int | None = None,
    ) -> Article:
        arxiv_id_url = self._text(entry, f"{ATOM}id")
        parsed_id_path = urllib.parse.unquote(urllib.parse.urlparse(arxiv_id_url).path)
        arxiv_id = (
            parsed_id_path.split("/abs/", 1)[1]
            if "/abs/" in parsed_id_path
            else arxiv_id_url.rstrip("/").split("/")[-1]
        )
        arxiv_id = arxiv_id.strip("/")
        clean_id = arxiv_id.replace("/", "_")
        title = re.sub(r"\s+", " ", self._text(entry, f"{ATOM}title")).strip()
        author_nodes = entry.findall(f"{ATOM}author")
        authors = [self._text(author, f"{ATOM}name") for author in author_nodes]
        source_institutions = [
            self._text(author, f"{ARXIV}affiliation").strip()
            for author in author_nodes
            if self._text(author, f"{ARXIV}affiliation").strip()
        ]
        publish_date = self._text(entry, f"{ATOM}published")
        summary = re.sub(r"\s+", " ", self._text(entry, f"{ATOM}summary")).strip()
        categories = [item.attrib.get("term", "") for item in entry.findall(f"{ATOM}category")]
        pdf_url = self._pdf_url(entry, arxiv_id)
        article_id = f"arxiv-{clean_id}"
        prefix = f"{index}/{total} " if index is not None and total is not None else ""
        self._event(
            "START",
            "normalize",
            f"candidate={prefix.strip() or '1/1'} article={article_id} title={title!r}",
        )

        metadata: dict[str, Any] = {
            "arxiv_id": arxiv_id,
            "abstract": summary,
            "categories": [item for item in categories if item],
            "source_record": {"api_id": arxiv_id_url, "pdf_url": pdf_url},
            "media_manifest": [],
            "affiliation_source": "",
            "affiliation_source_text": "",
            "affiliations": [],
            "institutions": [],
            "countries": [],
            "source_institutions": source_institutions,
            "discovery_backend": "api",
        }
        if source_institutions:
            metadata["affiliation_source"] = "arxiv_api"
            metadata["affiliation_source_format"] = "structured"
            metadata["affiliation_source_text"] = "; ".join(source_institutions)

        self._event(
            "DONE",
            "normalize",
            f"article={article_id} affiliation_source={metadata['affiliation_source'] or 'none'} "
            f"affiliation_chars={len(metadata['affiliation_source_text'])} attachments=0",
        )

        return Article(
            id=article_id,
            type="paper",
            source="arxiv",
            title=title,
            authors=authors,
            publish_date=publish_date,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            tags=[tag for tag in categories if tag],
            attachments=[],
            summary="",
            metadata=metadata,
        )

    def prepare_for_priority(self, article: Article) -> Article:
        """Fetch light HTML evidence while deliberately leaving the PDF untouched."""

        if self.source.options.get("prepare_arxiv_html", True):
            self._attach_arxiv_html_metadata(article)
        return article

    def prepare(self, article: Article) -> Article:
        """Download attachments only after the producer selected this candidate."""

        article.metadata.setdefault("download_date", date.today().isoformat())
        html_path = None
        if self.source.options.get("prepare_arxiv_html", True):
            self._attach_arxiv_html_metadata(article)
            article.metadata["media_manifest"] = self._localize_media_images(article)
            html_path = self._materialize_arxiv_html(article)

        remote_images = sum(
            item.get("type") == "image"
            and str(item.get("src", "")).startswith(("http://", "https://"))
            for item in article.metadata.get("media_manifest", []) or []
        )
        min_chars = int(self.source.options.get("html_min_content_chars", 8000))
        html_chars = int(article.metadata.get("arxiv_html_content_chars", 0) or 0)
        html_sections = int(article.metadata.get("arxiv_html_section_count", 0) or 0)
        html_sufficient = bool(
            html_path
            and html_chars >= min_chars
            and html_sections >= 3
            and remote_images == 0
        )
        article.metadata["source_html_sufficient"] = html_sufficient
        article.metadata["source_html_remote_images"] = remote_images

        pdf_path = None
        if self.source.options.get("download_pdf", True) and not html_sufficient:
            pdf_url = str(article.metadata.get("source_record", {}).get("pdf_url", ""))
            pdf_path = self._download_pdf(pdf_url, article)
            if pdf_path:
                relative_pdf = relative_to_root(pdf_path, self.root)
                if not any(
                    item.type == "pdf" and item.path == relative_pdf
                    for item in article.attachments
                ):
                    article.attachments.append(
                        Attachment(type="pdf", path=relative_pdf, title="PDF")
                    )
                if self.source.options.get("prepare_pdf_figures", True):
                    article.metadata["pdf_figures"] = self._prepare_pdf_figures(
                        pdf_path, article
                    )
                    if remote_images and article.metadata["pdf_figures"]:
                        article.metadata["media_manifest"] = [
                            item
                            for item in article.metadata.get("media_manifest", []) or []
                            if not (
                                item.get("type") == "image"
                                and str(item.get("src", "")).startswith(
                                    ("http://", "https://")
                                )
                            )
                        ]
                        article.metadata["media_image_fallback"] = "pdf_figures"
                        article.metadata["media_remote_images_replaced"] = remote_images
                        article.metadata["source_html_remote_images"] = 0
                        self._event(
                            "DONE",
                            "media_fallback",
                            f"article={article.id} remote_images={remote_images} "
                            f"pdf_figures={len(article.metadata['pdf_figures'])}",
                        )
        elif html_sufficient:
            self._event(
                "DONE",
                "pdf",
                f"article={article.id} action=skipped reason=html_sufficient "
                f"chars={html_chars} sections={html_sections}",
            )

        if not article.metadata.get("affiliation_source_text") and pdf_path:
            pdf_text = self._extract_pdf_first_page_text(pdf_path, article.id)
            if pdf_text:
                article.metadata["affiliation_source"] = "pdf_first_page"
                article.metadata["affiliation_source_format"] = "first_page"
                article.metadata["affiliation_source_text"] = pdf_text
        return article

    def _attach_arxiv_html_metadata(self, article: Article) -> None:
        if article.metadata.get("arxiv_html_prepared"):
            return
        arxiv_id = str(article.metadata.get("arxiv_id", ""))
        html_payload = self._prepare_arxiv_html(arxiv_id)
        article.metadata["arxiv_html_prepared"] = True
        if not html_payload:
            return
        article.metadata["media_manifest"] = html_payload["media_manifest"]
        article.metadata["arxiv_html_content_chars"] = html_payload.get("content_chars", 0)
        article.metadata["arxiv_html_section_count"] = html_payload.get("section_count", 0)
        if html_payload["affiliation_text"]:
            article.metadata["affiliation_source"] = "arxiv_html"
            article.metadata["affiliation_source_format"] = "structured"
            article.metadata["affiliation_source_text"] = html_payload["affiliation_text"]

    def _download_pdf(self, pdf_url: str, article: Article) -> Path | None:
        article_id = article.id
        date_folder = self._date_folder(article)
        out_path = self.root / "attachments" / "pdf" / date_folder / f"{article_id}.pdf"
        legacy_path = self.root / "attachments" / "pdf" / f"{article_id}.pdf"
        for attachment in article.attachments:
            if attachment.type != "pdf" or not attachment.path:
                continue
            cached_path = Path(attachment.path)
            if not cached_path.is_absolute():
                cached_path = self.root / cached_path
            if cached_path.is_file():
                self._event(
                    "DONE",
                    "pdf",
                    f"article={article_id} action=article_cache_hit "
                    f"path={relative_to_root(cached_path, self.root)}",
                )
                return cached_path
        if out_path.exists():
            self._event(
                "DONE",
                "pdf",
                f"article={article_id} action=cache_hit path={relative_to_root(out_path, self.root)}",
            )
            return out_path
        if legacy_path.exists():
            self._event(
                "DONE",
                "pdf",
                f"article={article_id} action=legacy_cache_hit path={relative_to_root(legacy_path, self.root)}",
            )
            return legacy_path
        try:
            self._event("START", "pdf", f"article={article_id} action=download url={pdf_url}")
            with urllib.request.urlopen(pdf_url, timeout=180) as response:
                data = response.read()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            self._event("DONE", "pdf", f"article={article_id} bytes={len(data)}")
            return out_path
        except Exception as exc:
            self._event("WARN", "pdf", f"article={article_id} action=download error={exc}")
            return None

    def _prepare_pdf_figures(self, pdf_path: Path, article: Article) -> list[dict[str, Any]]:
        article_id = article.id
        out_dir = self.root / "attachments" / "image" / self._date_folder(article) / article_id
        try:
            manifest = extract_pdf_figures(pdf_path, out_dir)
        except ImportError as exc:
            self._event("WARN", "pdf_figures", f"article={article_id} dependency_error={exc}")
            return []
        except Exception as exc:
            self._event("WARN", "pdf_figures", f"article={article_id} error={exc}")
            return []
        for item in manifest:
            item["path"] = relative_to_root(Path(item["path"]), self.root)
        self._event("DONE", "pdf_figures", f"article={article_id} items={len(manifest)}")
        return manifest

    def _localize_media_images(self, article: Article) -> list[dict[str, Any]]:
        arxiv_id = str(article.metadata.get("arxiv_id", ""))
        manifest = repair_media_manifest_urls(
            article.metadata.get("media_manifest", []) or [], arxiv_id
        )
        out_dir = (
            self.root
            / "attachments"
            / "image"
            / self._date_folder(article)
            / article.id
        )
        localized: list[dict[str, Any]] = []
        for index, raw_item in enumerate(manifest, start=1):
            item = dict(raw_item)
            src = str(item.get("src", ""))
            if item.get("type") != "image" or not src.startswith(("http://", "https://")):
                localized.append(item)
                continue
            suffix = Path(urllib.parse.urlparse(src).path).suffix.lower()
            if suffix not in {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}:
                suffix = ".img"
            out_path = out_dir / f"media-{index:03d}{suffix}"
            try:
                if not out_path.exists():
                    self._event(
                        "START", "html_image", f"article={article.id} item={index} url={src}"
                    )
                    with urllib.request.urlopen(src, timeout=180) as response:
                        data = response.read()
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(data)
                item["original_src"] = src
                item["src"] = relative_to_root(out_path, self.root)
                self._event(
                    "DONE",
                    "html_image",
                    f"article={article.id} item={index} path={item['src']}",
                )
            except Exception as exc:
                self._event(
                    "WARN", "html_image", f"article={article.id} item={index} error={exc}"
                )
            localized.append(item)
        return localized

    def _materialize_arxiv_html(self, article: Article) -> Path | None:
        for attachment in article.attachments:
            if attachment.type != "html" or not attachment.path:
                continue
            cached_path = Path(attachment.path)
            if not cached_path.is_absolute():
                cached_path = self.root / cached_path
            if cached_path.is_file():
                self._event(
                    "DONE",
                    "source_html",
                    f"article={article.id} action=article_cache_hit "
                    f"path={relative_to_root(cached_path, self.root)}",
                )
                return cached_path
        arxiv_id = str(article.metadata.get("arxiv_id", ""))
        payload = self._prepare_arxiv_html(arxiv_id, require_raw=True)
        if not payload or not payload.get("raw_path"):
            return None
        raw_path = Path(str(payload["raw_path"]))
        if not raw_path.is_file():
            return None
        out_path = (
            self.root
            / "attachments"
            / "source"
            / self._date_folder(article)
            / f"{article.id}.html"
        )
        if not out_path.exists():
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(raw_path.read_bytes())
        relative = relative_to_root(out_path, self.root)
        if not any(item.type == "html" and item.path == relative for item in article.attachments):
            article.attachments.append(
                Attachment(type="html", path=relative, title="ArXiv HTML 原文")
            )
        article.metadata["arxiv_html_content_chars"] = payload.get("content_chars", 0)
        article.metadata["arxiv_html_section_count"] = payload.get("section_count", 0)
        self._event(
            "DONE",
            "source_html",
            f"article={article.id} path={relative} chars={payload.get('content_chars', 0)}",
        )
        return out_path

    def _prepare_arxiv_html(
        self,
        arxiv_id: str,
        *,
        require_raw: bool = False,
    ) -> dict[str, Any] | None:
        html_url = f"https://arxiv.org/html/{arxiv_id}"
        cache_stem = hashlib.sha1(arxiv_id.encode()).hexdigest()
        html_cache = self.root / "cache" / "arxiv_html" / f"{cache_stem}.json"
        raw_cache = self.root / "cache" / "arxiv_html" / f"{cache_stem}.html"
        if html_cache.exists():
            try:
                cached = json.loads(html_cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            else:
                original_manifest = list(cached.get("media_manifest", []))
                repaired_manifest = repair_media_manifest_urls(original_manifest, arxiv_id)
                if repaired_manifest != original_manifest:
                    cached["media_manifest"] = repaired_manifest
                    html_cache.write_text(
                        json.dumps(cached, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    self._event(
                        "DONE", "arxiv_html", f"article={arxiv_id} action=cache_url_repaired"
                    )
                if raw_cache.exists() or not require_raw:
                    self._event("DONE", "arxiv_html", f"article={arxiv_id} action=cache_hit")
                    return {
                        "media_manifest": repaired_manifest,
                        "affiliation_text": str(cached.get("affiliation_text", "")),
                        "content_chars": int(cached.get("content_chars", 0) or 0),
                        "section_count": int(cached.get("section_count", 0) or 0),
                        "raw_path": str(raw_cache) if raw_cache.exists() else "",
                    }
        try:
            self._event("START", "arxiv_html", f"article={arxiv_id} url={html_url}")
            raw = urllib.request.urlopen(html_url, timeout=90).read().decode("utf-8", errors="replace")
        except Exception as exc:
            self._event("WARN", "arxiv_html", f"article={arxiv_id} unavailable error={exc}")
            return None
        manifest = extract_media_manifest(raw, html_url)
        affiliation_text = extract_affiliation_text(raw)
        content_chars = len(extract_visible_text(raw, max_chars=10_000_000))
        section_count = count_document_sections(raw)
        html_cache.parent.mkdir(parents=True, exist_ok=True)
        raw_cache.write_text(raw, encoding="utf-8")
        html_cache.write_text(
            json.dumps(
                {
                    "url": html_url,
                    "media_manifest": manifest,
                    "affiliation_text": affiliation_text,
                    "content_chars": content_chars,
                    "section_count": section_count,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._event(
            "DONE",
            "arxiv_html",
            f"article={arxiv_id} media_items={len(manifest)} affiliation_chars={len(affiliation_text)} "
            f"content_chars={content_chars} sections={section_count}",
        )
        return {
            "media_manifest": manifest,
            "affiliation_text": affiliation_text,
            "content_chars": content_chars,
            "section_count": section_count,
            "raw_path": str(raw_cache),
        }

    @staticmethod
    def _date_folder(article: Article) -> str:
        value = str(article.metadata.get("download_date", ""))
        return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else date.today().isoformat()

    def _extract_pdf_first_page_text(self, pdf_path: Path, article_id: str) -> str:
        try:
            text = extract_pdf_first_page_text(pdf_path)
        except ImportError as exc:
            self._event("WARN", "pdf_text", f"article={article_id} dependency_error={exc}")
            return ""
        except Exception as exc:
            self._event("WARN", "pdf_text", f"article={article_id} error={exc}")
            return ""
        if text:
            self._event("DONE", "pdf_text", f"article={article_id} chars={len(text)}")
        else:
            self._event("WARN", "pdf_text", f"article={article_id} empty_first_page=true")
        return text

    @staticmethod
    def _text(entry: ET.Element, tag: str) -> str:
        child = entry.find(tag)
        return child.text if child is not None and child.text else ""

    @staticmethod
    def _pdf_url(entry: ET.Element, arxiv_id: str) -> str:
        for link in entry.findall(f"{ATOM}link"):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                return link.attrib.get("href", "")
        return f"https://arxiv.org/pdf/{arxiv_id}"
