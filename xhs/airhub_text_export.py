"""Text-only AirHub adapter derived from XHS-Downloader 2.11 beta.

This module intentionally uses the upstream public HTML/empty-cookie path. It
does not read browser cookies, authenticate, or discover/download media.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from lxml.etree import HTML
from yaml import safe_load


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 "
    "Safari/537.36 Edg/143.0.0.0"
)
HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "referer": "https://www.xiaohongshu.com/explore",
    "user-agent": USER_AGENT,
}
SUPPORTED_PATH = re.compile(
    r"^/(?:explore/[^/?#]+|discovery/item/[^/?#]+|"
    r"user/profile/[A-Za-z0-9]+/[^/?#]+)"
)
YAML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INITIAL_STATE_PREFIX = "window.__INITIAL_STATE__="


def _safe_id(value: object, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return cleaned[:80] or fallback


def _deep_get(data: object, keys: tuple[object, ...], default=None):
    try:
        current = data
        for key in keys:
            if isinstance(key, int):
                if isinstance(current, dict):
                    current = list(current.values())[key]
                else:
                    current = current[key]
            else:
                current = current[key]
        return current
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def _parse_note_state(content: str) -> dict:
    if not content:
        return {}
    tree = HTML(content)
    scripts = tree.xpath("//script/text()") if tree is not None else []
    script = next(
        (
            value
            for value in reversed(scripts)
            if isinstance(value, str)
            and value.startswith("window.__INITIAL_STATE__")
        ),
        "",
    )
    if not script:
        return {}
    raw = script[len(INITIAL_STATE_PREFIX) :] if script.startswith(INITIAL_STATE_PREFIX) else script
    payload = safe_load(YAML_ILLEGAL.sub("", raw))
    if not isinstance(payload, dict):
        return {}
    note = _deep_get(payload, ("noteData", "data", "noteData")) or _deep_get(
        payload, ("note", "noteDetailMap", -1, "note")
    )
    return note if isinstance(note, dict) else {}


def _format_time(value: object) -> str:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return "未知"
    if milliseconds <= 0:
        return "未知"
    return datetime.fromtimestamp(milliseconds / 1000).strftime("%Y-%m-%d_%H:%M:%S")


def _note_type(note: dict) -> str:
    kind = str(note.get("type") or "")
    images = note.get("imageList")
    count = len(images) if isinstance(images, list) else 0
    if kind == "video" and count == 1:
        return "视频"
    if kind == "video" and count > 1:
        return "图集"
    if kind == "normal" and count > 0:
        return "图文"
    return "未知"


def _extract_note(content: str, source_url: str) -> dict:
    note = _parse_note_state(content)
    if not note:
        return {}
    tags = note.get("tagList")
    tag_text = " ".join(
        str(item.get("name") or "").strip()
        for item in tags or []
        if isinstance(item, dict) and item.get("name")
    )
    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    interact = (
        note.get("interactInfo")
        if isinstance(note.get("interactInfo"), dict)
        else {}
    )
    note_id = str(note.get("noteId") or "").strip()
    if not note_id:
        return {}
    author_id = str(user.get("userId") or "").strip()
    return {
        "收藏数量": interact.get("collectedCount", "-1"),
        "评论数量": interact.get("commentCount", "-1"),
        "分享数量": interact.get("shareCount", "-1"),
        "点赞数量": interact.get("likedCount", "-1"),
        "作品标签": tag_text,
        "作品ID": note_id,
        "作品链接": source_url,
        "作品标题": str(note.get("title") or "").strip(),
        "作品描述": str(note.get("desc") or "").strip(),
        "作品类型": _note_type(note),
        "发布时间": _format_time(note.get("time")),
        "最后更新时间": _format_time(note.get("lastUpdateTime")),
        "作者昵称": str(user.get("nickname") or user.get("nickName") or "").strip(),
        "作者ID": author_id,
        "作者链接": (
            f"https://www.xiaohongshu.com/user/profile/{author_id}"
            if author_id
            else ""
        ),
    }


def _display_title(data: dict, source_title: str) -> str:
    title = str(data.get("作品标题") or "").strip()
    if title:
        return title
    description = str(data.get("作品描述") or "").strip()
    if description:
        return description.splitlines()[0][:120]
    return source_title.strip()[:200] or "（原帖标题未能获取）"


def _text_content(data: dict) -> str:
    fields = (
        "作品链接",
        "作品ID",
        "作品标题",
        "作者昵称",
        "发布时间",
        "最后更新时间",
        "作品标签",
    )
    lines = [f"{key}：{data.get(key, '')}" for key in fields]
    lines.extend(("", "作品描述：", str(data.get("作品描述") or "")))
    return "\n".join(lines).rstrip() + "\n"


def _supported_note_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and host in {"xiaohongshu.com", "www.xiaohongshu.com"}
        and bool(SUPPORTED_PATH.match(parsed.path))
    )


async def _resolve_url(client: httpx.AsyncClient, url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("链接必须使用 HTTP(S)")
    if host == "xhslink.com":
        response = await client.get(url)
        response.raise_for_status()
        url = str(response.url)
    if not _supported_note_url(url):
        raise ValueError("不是受支持的小红书帖子链接")
    return url


async def _fetch_note(client: httpx.AsyncClient, url: str, retries: int = 3) -> dict:
    resolved = await _resolve_url(client, url)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = await client.get(resolved)
            response.raise_for_status()
            data = _extract_note(response.text, resolved)
            if data:
                return data
            raise ValueError("公开页面中未找到帖子正文")
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(attempt)
    raise RuntimeError(str(last_error or "帖子提取失败"))


async def export(input_path: Path, output_dir: Path) -> int:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    link_entries: list[tuple[str, str]] = []
    for item in payload.get("links", []):
        if isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            source_title = str(item.get("source_title", "")).strip()
        else:
            url = str(item).strip()
            source_title = ""
        if url:
            link_entries.append((url, source_title))

    output_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    saved = 0
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies={},
        follow_redirects=True,
        timeout=timeout,
        http2=True,
    ) as client:
        total = len(link_entries)
        for index, (link, source_title) in enumerate(link_entries, start=1):
            try:
                data = await _fetch_note(client, link)
                error = ""
            except Exception as exc:  # keep the batch moving after one bad post
                data = {}
                error = str(exc)
            title = _display_title(data, source_title)
            if not data.get("作品ID"):
                entries.append(
                    {
                        "url": link,
                        "source_title": source_title,
                        "title": title,
                        "status": "failed",
                        "error": error or "未提取到帖子正文",
                    }
                )
                print(
                    f"[PROGRESS] XHS {index}/{total} 失败 | {title} | {link} | "
                    f"{error or '未提取到帖子正文'}",
                    flush=True,
                )
                continue
            note_id = _safe_id(data.get("作品ID"), f"note_{index:03d}")
            output_path = output_dir / f"{index:03d}_{note_id}.txt"
            output_path.write_text(_text_content(data), encoding="utf-8")
            saved += 1
            entries.append(
                {
                    "url": link,
                    "note_id": str(data.get("作品ID", "")),
                    "source_title": source_title,
                    "title": title,
                    "path": output_path.name,
                    "status": "saved",
                }
            )
            print(
                f"[PROGRESS] XHS {index}/{total} 成功 | {title} | {link}",
                flush=True,
            )

    report = {
        "version": 1,
        "requested": len(link_entries),
        "saved": saved,
        "failed": len(link_entries) - saved,
        "media_downloaded": False,
        "cookie_used": False,
        "entries": entries,
    }
    (output_dir / "_download_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"[DONE] XHS 空登录态文本批处理 requested={len(link_entries)} "
        f"saved={saved} failed={len(link_entries) - saved}"
    )
    return 0 if saved else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Export XHS text without media or login")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(export(Path(args.input), Path(args.output))))


if __name__ == "__main__":
    main()
