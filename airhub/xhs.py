"""AirHub integration for the bundled no-login XHS text extractor."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .paths import PROJECT_ROOT, relative_to_root
from .xhs_activity import record_xhs_completion


XHS_URL_RE = re.compile(
    r"(?:https?://)?(?:"
    r"www\.xiaohongshu\.com/(?:explore/[^\s\"<>，。；！？、【】《》]+|"
    r"discovery/item/[^\s\"<>，。；！？、【】《》]+|"
    r"user/profile/[a-zA-Z0-9]+/[^\s\"<>，。；！？、【】《》]+)"
    r"|xhslink\.com/[^\s\"<>\\^`{|}，。；！？、【】《》]+)",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = "'\")]}>,，。；！？、【】《》"


@dataclass(frozen=True)
class XHSLinkCandidate:
    url: str
    source_title: str = ""


@dataclass(frozen=True)
class XHSTextDownloadEntry:
    url: str
    title: str
    status: str
    error: str = ""


@dataclass(frozen=True)
class XHSTextDownloadResult:
    session_dir: Path
    requested: int
    saved: int
    failed: int
    log_path: Path
    entries: tuple[XHSTextDownloadEntry, ...] = ()
    daily_sequence: int = 0
    completed_at: str = ""


def extract_xhs_links(text: str) -> list[str]:
    """Mirror XHS-Downloader's supported URL set and preserve paste order."""

    seen: set[str] = set()
    links: list[str] = []
    for match in XHS_URL_RE.finditer(text or ""):
        value = match.group(0).rstrip(TRAILING_PUNCTUATION)
        if not value.lower().startswith(("http://", "https://")):
            value = "https://" + value
        if value not in seen:
            seen.add(value)
            links.append(value)
    return links


def _clean_source_title(line: str) -> str:
    without_links = XHS_URL_RE.sub(" ", line)
    value = re.sub(
        r"(?:小红书)?(?:帖子|作品)?(?:标题|链接|地址)\s*[:：=]",
        " ",
        without_links,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip(
        " \t[]【】()（）:：,，.;；|｜—–-"
    )
    if not value or value.lower() in {"url", "link", "xhs", "小红书"}:
        return ""
    return value[:200]


def extract_xhs_candidates(text: str) -> list[XHSLinkCandidate]:
    """Extract ordered URLs and best-effort titles supplied by clipboard text."""

    seen: set[str] = set()
    result: list[XHSLinkCandidate] = []
    for line in (text or "").splitlines() or [text or ""]:
        matches = list(XHS_URL_RE.finditer(line))
        source_title = _clean_source_title(line) if len(matches) == 1 else ""
        for match in matches:
            value = match.group(0).rstrip(TRAILING_PUNCTUATION)
            if not value.lower().startswith(("http://", "https://")):
                value = "https://" + value
            if value in seen:
                continue
            seen.add(value)
            result.append(XHSLinkCandidate(value, source_title))
    return result


def _timestamped_directory(parent: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    candidate = parent / stamp
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{stamp}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def _ensure_xhs_runtime(root: Path) -> Path:
    python = root / "xhs" / ".venv" / "bin" / "python"
    if python.is_file():
        return python
    completed = subprocess.run(
        ["bash", "run/setup_xhs_text.sh"], cwd=root, check=False
    )
    if completed.returncode or not python.is_file():
        raise RuntimeError(
            "XHS 文本提取环境安装失败，请查看上方安装日志"
        )
    return python


def _stream_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
    output: Callable[[str], None] | None,
) -> int:
    """Mirror child output to the terminal and the persistent log in real time."""

    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            log.write(raw_line)
            log.flush()
            line = raw_line.rstrip("\r\n")
            if output is not None and line:
                output(line)
        return process.wait()


def download_xhs_texts(
    root: Path = PROJECT_ROOT,
    links: Iterable[str] = (),
    *,
    now: datetime | None = None,
    source_text: str = "",
    output: Callable[[str], None] | None = print,
) -> XHSTextDownloadResult:
    """Run the bundled repository with an empty cookie and media disabled."""

    root = root.resolve()
    started_at = datetime.now().astimezone()
    normalized = extract_xhs_links("\n".join(str(item) for item in links))
    if not normalized:
        raise ValueError("没有可下载的有效小红书帖子链接")
    supplied_titles = {
        candidate.url: candidate.source_title
        for candidate in extract_xhs_candidates(source_text)
        if candidate.source_title
    }
    session_dir = _timestamped_directory(root / "cache" / "xhs", now)
    input_path = session_dir / "_links.json"
    report_path = session_dir / "_download_report.json"
    input_path.write_text(
        json.dumps(
            {
                "version": 2,
                "links": [
                    {"url": url, "source_title": supplied_titles.get(url, "")}
                    for url in normalized
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    python = _ensure_xhs_runtime(root)
    log_dir = root / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"xhs_text_{session_dir.name}.log"
    environment = os.environ.copy()
    environment["PIP_CACHE_DIR"] = environment.get(
        "PIP_CACHE_DIR", str(root / "cache" / "pip")
    )
    environment["HF_HOME"] = environment.get(
        "HF_HOME", str(root / "cache" / "huggingface")
    )
    command = [
        str(python),
        str(root / "xhs" / "airhub_text_export.py"),
        "--input",
        str(input_path),
        "--output",
        str(session_dir),
    ]
    returncode = 1
    report: dict = {}
    saved = 0
    failed = len(normalized)
    activity_status = "failed"
    error_text = ""
    try:
        returncode = _stream_command(
            command,
            cwd=root / "xhs",
            environment=environment,
            log_path=log_path,
            output=output,
        )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        saved = int(report.get("saved", 0))
        failed = int(report.get("failed", max(0, len(normalized) - saved)))
        activity_status = "success" if failed == 0 else "partial" if saved else "failed"
        if returncode and saved == 0:
            raise RuntimeError(
                "XHS 文本提取失败；日志：" + relative_to_root(log_path, root)
            )
    except Exception as exc:
        error_text = str(exc)
        activity_status = "failed"
        raise
    finally:
        activity = record_xhs_completion(
            root,
            "download",
            activity_status,
            started_at=started_at,
            details={
                "requested": len(normalized),
                "saved": saved,
                "failed": failed,
                "session": relative_to_root(session_dir, root),
                "log": relative_to_root(log_path, root),
                "returncode": returncode,
                "error": error_text,
            },
        )
    report_entries = []
    for raw in report.get("entries", []):
        if not isinstance(raw, dict):
            continue
        report_entries.append(
            XHSTextDownloadEntry(
                url=str(raw.get("url", "")),
                title=str(raw.get("title") or raw.get("source_title") or "（原帖标题未能获取）"),
                status=str(raw.get("status", "failed")),
                error=str(raw.get("error", "")),
            )
        )
    return XHSTextDownloadResult(
        session_dir=session_dir,
        requested=len(normalized),
        saved=saved,
        failed=failed,
        log_path=log_path,
        entries=tuple(report_entries),
        daily_sequence=activity.count,
        completed_at=activity.last_completed_at,
    )
