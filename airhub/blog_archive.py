"""Self-contained blog snapshots and the persistent blog-origin catalogue."""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import mimetypes
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse, urlunparse

from .models import utc_now_iso
from .paths import PROJECT_ROOT, relative_to_root


REMOTE_SCHEMES = {"http", "https"}
CATALOG_ONLY_HOST_SUFFIXES = (
    "github.com",
    "github.dev",
    "githubusercontent.com",
    "huggingface.co",
    "hf.co",
)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(['\"])(.*?)\1", re.IGNORECASE
)


class BlogArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlogSite:
    origin: str
    first_seen_at: str
    last_seen_at: str
    sample_url: str


@dataclass(frozen=True)
class BlogArchiveResult:
    url: str
    final_url: str
    origin: str
    output_path: Path
    resources_embedded: int
    optional_resources_removed: int


def canonical_blog_origin(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in REMOTE_SCHEMES or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"无效的 Blog 地址: {url}")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"无效的 Blog 主机名: {url}") from exc
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError(f"Blog 地址不能指向本机: {url}")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError(f"Blog 地址不能指向非公网 IP: {url}")
    port = parsed.port
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    return urlunparse((scheme, netloc, "", "", "", ""))


def blog_archive_mode(url: str) -> str:
    """Choose between a self-contained snapshot and catalogue-only storage."""

    canonical_blog_origin(url)
    hostname = str(urlparse(url).hostname or "").lower().rstrip(".")
    if any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in CATALOG_ONLY_HOST_SUFFIXES
    ):
        return "catalog_only"
    return "self_contained"


def _assert_public_network_target(url: str) -> None:
    canonical_blog_origin(url)
    parsed = urlparse(url)
    hostname = str(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlogArchiveError(f"Blog 资源域名解析失败: {hostname}: {exc}") from exc
    if not addresses:
        raise BlogArchiveError(f"Blog 资源域名没有可用地址: {hostname}")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise BlogArchiveError(f"Blog 资源解析到非公网 IP，已拒绝: {url} -> {ip}")


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_network_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BlogSiteStore:
    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root.resolve()
        self.path = self.root / "data" / "blog" / "sites.json"

    def list(self) -> list[BlogSite]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Blog 列表损坏: {self.path}") from exc
        items = payload.get("sites", []) if isinstance(payload, dict) else []
        result = []
        for item in items:
            try:
                result.append(BlogSite(**item))
            except (TypeError, ValueError):
                continue
        return result

    def _save(self, sites: list[BlogSite]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now_iso(),
            "sites": [asdict(item) for item in sites],
        }
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def add(self, url: str) -> tuple[BlogSite, bool]:
        origin = canonical_blog_origin(url)
        now = utc_now_iso()
        sites = self.list()
        for index, current in enumerate(sites):
            if current.origin == origin:
                updated = BlogSite(
                    origin=origin,
                    first_seen_at=current.first_seen_at,
                    last_seen_at=now,
                    sample_url=url,
                )
                sites[index] = updated
                self._save(sites)
                return updated, False
        site = BlogSite(origin, now, now, url)
        sites.append(site)
        self._save(sites)
        return site, True

    def remove_indexes(self, indexes: list[int]) -> list[BlogSite]:
        sites = self.list()
        normalized = list(dict.fromkeys(indexes))
        invalid = [index for index in normalized if not 1 <= index <= len(sites)]
        if invalid:
            raise ValueError(f"Blog 编号必须在 1–{len(sites)} 之间: {invalid}")
        selected = set(normalized)
        removed = [site for index, site in enumerate(sites, 1) if index in selected]
        self._save([site for index, site in enumerate(sites, 1) if index not in selected])
        return removed


class _InlineHTMLParser(HTMLParser):
    RESOURCE_ATTRIBUTES = {
        "audio": {"src"},
        "embed": {"src"},
        "iframe": {"src"},
        "img": {"src"},
        "input": {"src"},
        "object": {"data"},
        "script": {"src"},
        "source": {"src"},
        "track": {"src"},
        "video": {"src", "poster"},
    }
    LAZY_ATTRIBUTES = {"data-src", "data-original", "data-lazy-src"}
    OPTIONAL_TAGS = {"script", "embed", "iframe", "object"}

    def __init__(self, archiver: "SelfContainedBlogArchiver", base_url: str):
        super().__init__(convert_charrefs=False)
        self.archiver = archiver
        self.base_url = base_url
        self.parts: list[str] = []
        self.style_depth = 0

    @staticmethod
    def _serialize_attrs(attrs: list[tuple[str, str | None]]) -> str:
        pieces = []
        for key, value in attrs:
            if value is None:
                pieces.append(key)
            else:
                pieces.append(f'{key}="{html.escape(value, quote=True)}"')
        return (" " + " ".join(pieces)) if pieces else ""

    def _process_attrs(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> list[tuple[str, str | None]]:
        lowered = {key.lower(): value for key, value in attrs}
        if tag == "meta" and str(lowered.get("http-equiv", "")).lower() in {
            "content-security-policy",
            "content-security-policy-report-only",
        }:
            return []
        resource_keys = set(self.RESOURCE_ATTRIBUTES.get(tag, set()))
        if tag == "link":
            rel = set(str(lowered.get("rel") or "").lower().split())
            if rel & {"stylesheet", "icon", "manifest", "preload", "modulepreload"}:
                resource_keys.add("href")
        processed: list[tuple[str, str | None]] = []
        promoted_src: str | None = None
        embedded_any = False
        for key, value in attrs:
            lower_key = key.lower()
            if lower_key in {"integrity", "crossorigin", "nonce"}:
                continue
            if value is None:
                processed.append((key, value))
                continue
            if lower_key == "style":
                value = self.archiver.rewrite_css(value, self.base_url)
            elif lower_key in {"srcset", "data-srcset"}:
                value = self.archiver.inline_srcset(value, self.base_url)
                embedded_any = True
            elif lower_key in resource_keys or lower_key in self.LAZY_ATTRIBUTES:
                required = tag not in self.OPTIONAL_TAGS
                value = self.archiver.inline_resource(value, self.base_url, required)
                embedded_any = embedded_any or value.startswith("data:")
                if tag == "img" and lower_key in self.LAZY_ATTRIBUTES and value.startswith("data:"):
                    promoted_src = value
            processed.append((key, value))
        if tag == "img" and promoted_src:
            current_src = next(
                (value for key, value in processed if key.lower() == "src"), None
            )
            if not current_src or not str(current_src).startswith("data:"):
                processed = [
                    (key, promoted_src if key.lower() == "src" else value)
                    for key, value in processed
                ]
                if not any(key.lower() == "src" for key, _ in processed):
                    processed.append(("src", promoted_src))
        if embedded_any:
            processed = [
                (key, value)
                for key, value in processed
                if key.lower() not in {"integrity", "crossorigin"}
            ]
        return processed

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        processed = self._process_attrs(tag, attrs)
        if tag == "meta" and attrs and not processed:
            self.parts.append("<!-- removed remote Content-Security-Policy -->")
            return
        if tag == "base":
            self.parts.append("<!-- removed base URL for offline snapshot -->")
            return
        self.parts.append(f"<{tag}{self._serialize_attrs(processed)}>")
        if tag == "style":
            self.style_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        processed = self._process_attrs(tag, attrs)
        self.parts.append(f"<{tag}{self._serialize_attrs(processed)}/>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "style" and self.style_depth:
            self.style_depth -= 1
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(
            self.archiver.rewrite_css(data, self.base_url) if self.style_depth else data
        )

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self.parts.append(f"<![{data}]>")

    def render(self) -> str:
        return "".join(self.parts)


class SelfContainedBlogArchiver:
    """Embed statically referenced page resources as data URIs."""

    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 AirHub/1.0"
    )

    def __init__(self, timeout: int = 120, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.cache: dict[str, str] = {}
        self.resources_embedded = 0
        self.optional_resources_removed = 0

    def _fetch(self, url: str) -> tuple[bytes, str, str]:
        _assert_public_network_target(url)
        error: Exception | None = None
        opener = urllib.request.build_opener(_PublicRedirectHandler())
        for _attempt in range(1, self.retries + 1):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": self.USER_AGENT,
                        "Accept": "*/*",
                    },
                )
                with opener.open(request, timeout=self.timeout) as response:
                    data = response.read()
                    final_url = response.geturl()
                    _assert_public_network_target(final_url)
                    mime = response.headers.get_content_type()
                    if mime == "application/octet-stream":
                        guessed, _ = mimetypes.guess_type(final_url)
                        mime = guessed or mime
                    return data, mime, final_url
            except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                error = exc
        raise BlogArchiveError(f"资源下载失败: {url}: {error}")

    @staticmethod
    def _decode(data: bytes, mime: str) -> str:
        for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise BlogArchiveError(f"无法解码文本资源: {mime}")

    @staticmethod
    def _to_data_uri(data: bytes, mime: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime or 'application/octet-stream'};base64,{encoded}"

    def inline_resource(self, value: str, base_url: str, required: bool = True) -> str:
        stripped = value.strip()
        if not stripped or stripped.startswith(("data:", "blob:", "javascript:", "#")):
            return value
        absolute = urljoin(base_url, stripped)
        parsed = urlparse(absolute)
        if parsed.scheme not in REMOTE_SCHEMES:
            return value
        if absolute in self.cache:
            return self.cache[absolute]
        try:
            data, mime, final_url = self._fetch(absolute)
            if mime in {"text/css", "application/css"} or final_url.lower().split("?")[0].endswith(".css"):
                css = self.rewrite_css(self._decode(data, mime), final_url)
                data = css.encode("utf-8")
                mime = "text/css;charset=utf-8"
            uri = self._to_data_uri(data, mime)
        except BlogArchiveError:
            if required:
                raise
            self.optional_resources_removed += 1
            uri = "data:text/plain;base64,"
        self.cache[absolute] = uri
        self.resources_embedded += 1
        return uri

    def rewrite_css(self, css: str, base_url: str) -> str:
        def replace_url(match: re.Match[str]) -> str:
            value = match.group(2).strip()
            if not value or value.startswith(("data:", "#")):
                return match.group(0)
            return f'url("{self.inline_resource(value, base_url, True)}")'

        def replace_import(match: re.Match[str]) -> str:
            return f'@import url("{self.inline_resource(match.group(2), base_url, True)}")'

        rewritten = CSS_IMPORT_RE.sub(replace_import, css)
        return CSS_URL_RE.sub(replace_url, rewritten)

    def inline_srcset(self, value: str, base_url: str) -> str:
        entries = []
        for item in value.split(","):
            parts = item.strip().split()
            if not parts:
                continue
            uri = self.inline_resource(parts[0], base_url, True)
            entries.append(" ".join([uri, *parts[1:]]))
        return ", ".join(entries)

    def archive(self, url: str, output_path: Path) -> BlogArchiveResult:
        canonical_blog_origin(url)
        raw, mime, final_url = self._fetch(url)
        if "html" not in mime and not final_url.lower().split("?")[0].endswith(
            (".html", ".htm")
        ):
            raise BlogArchiveError(f"Blog 地址没有返回 HTML: {url} ({mime})")
        source = self._decode(raw, mime)
        parser = _InlineHTMLParser(self, final_url)
        try:
            parser.feed(source)
            parser.close()
        except (ValueError, BlogArchiveError) as exc:
            raise BlogArchiveError(f"Blog 自包含转换失败: {url}: {exc}") from exc
        rendered = parser.render()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(output_path)
        return BlogArchiveResult(
            url=url,
            final_url=final_url,
            origin=canonical_blog_origin(final_url),
            output_path=output_path,
            resources_embedded=self.resources_embedded,
            optional_resources_removed=self.optional_resources_removed,
        )


def archive_blog(
    root: Path,
    url: str,
    *,
    now: datetime | None = None,
    archiver_factory: Callable[[], SelfContainedBlogArchiver] = SelfContainedBlogArchiver,
) -> BlogArchiveResult:
    root = root.resolve()
    now = now or datetime.now(timezone.utc).astimezone()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    stable = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    output = (
        root
        / "attachments"
        / "blog"
        / now.strftime("%Y-%m-%d")
        / f"blog-{stable}_{stamp}.html"
    )
    suffix = 2
    original = output
    while output.exists():
        output = original.with_name(f"{original.stem}_{suffix}{original.suffix}")
        suffix += 1
    result = archiver_factory().archive(url, output)
    metadata_path = root / "data" / "blog" / "archives" / f"{output.stem}.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "version": 1,
        "archived_at": utc_now_iso(),
        "url": result.url,
        "final_url": result.final_url,
        "origin": result.origin,
        "snapshot": relative_to_root(result.output_path, root),
        "resources_embedded": result.resources_embedded,
        "optional_resources_removed": result.optional_resources_removed,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
