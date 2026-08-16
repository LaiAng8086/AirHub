"""Prepare and finalize Codex classification jobs for cached XHS note text."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .fetchers.arxiv import normalize_arxiv_id
from .models import utc_now_iso
from .paths import PROJECT_ROOT, relative_to_root
from .xhs_activity import record_xhs_completion


URL_RE = re.compile(r"https?://[^\s<>\"'，。；！？、【】《》]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ").,;:!?]}>，。；！？、【】《》"
TITLE_RE = re.compile(r"^作品标题\s*[:：]\s*(.*)$", re.MULTILINE)
DESCRIPTION_RE = re.compile(r"^作品描述\s*[:：]\s*\n([^\n]+)", re.MULTILINE)


@dataclass(frozen=True)
class XHSClassificationJob:
    job_path: Path
    result_path: Path
    work_dir: Path
    item_count: int


def _unique_directory(parent: Path, stem: str) -> Path:
    path = parent / stem
    suffix = 2
    while path.exists():
        path = parent / f"{stem}_{suffix}"
        suffix += 1
    path.mkdir(parents=True)
    return path


def _xhs_post_title(content: str, path: Path) -> str:
    match = TITLE_RE.search(content)
    title = match.group(1).strip() if match else ""
    if not title:
        description = DESCRIPTION_RE.search(content)
        title = description.group(1).strip() if description else ""
    title = re.sub(r"\s+", " ", title).strip()
    return title[:200] or f"（原帖标题未能获取：{path.stem}）"


def prepare_xhs_classification_job(
    root: Path = PROJECT_ROOT,
    *,
    now: datetime | None = None,
) -> XHSClassificationJob | None:
    root = root.resolve()
    cache_root = root / "cache" / "xhs"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_entries = sorted(cache_root.iterdir(), key=lambda path: path.name)
    if not cache_entries:
        return None
    text_paths = sorted(
        (path for path in cache_root.rglob("*.txt") if path.is_file()),
        key=lambda path: path.relative_to(cache_root).as_posix(),
    )
    items = []
    for index, path in enumerate(text_paths, start=1):
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        urls = [
            match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
            for match in URL_RE.finditer(content)
        ]
        items.append(
            {
                "item_id": f"xhs-{index:05d}",
                "cache_path": relative_to_root(path, root),
                "title": _xhs_post_title(content, path),
                "detected_urls": list(dict.fromkeys(urls)),
                "text": content,
            }
        )
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    work_dir = _unique_directory(
        root / "Work_dirs", f"INFER_{stamp}_xhs_classifier"
    )
    job_path = work_dir / "job.json"
    result_path = work_dir / "classifier_result.json"
    payload = {
        "version": 2,
        "created_at": utc_now_iso(),
        "cache_root": "cache/xhs",
        "cache_entries": [relative_to_root(path, root) for path in cache_entries],
        "result_path": relative_to_root(result_path, root),
        "items": items,
    }
    job_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return XHSClassificationJob(job_path, result_path, work_dir, len(items))


def _safe_blog_url(value: object) -> str:
    url = str(value or "").strip().rstrip(TRAILING_URL_PUNCTUATION)
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"无效 Blog 地址: {value}")
    hostname = parsed.hostname.lower()
    if hostname.endswith("xiaohongshu.com") or hostname == "xhslink.com":
        raise ValueError("分类结果不能把小红书帖子本身作为 Blog 地址")
    if hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        raise ValueError("arXiv 地址必须输出为 arxiv 类型和编号")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Blog 地址不能指向本机")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Blog 地址不能指向非公网 IP")
    return url


def _manual_output_path(root: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = root / "manual" / f"{stamp}.txt"
    suffix = 2
    while path.exists():
        path = path.with_name(f"{stamp}_{suffix}.txt")
        suffix += 1
    return path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取分类 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"分类 JSON 顶层必须是对象: {path}")
    return payload


def _cleanup_job_cache(root: Path, job: dict[str, Any]) -> int:
    cache_root = (root / "cache" / "xhs").resolve()
    removed = 0
    for raw_path in job.get("cache_entries", []):
        path = (root / str(raw_path)).resolve()
        try:
            path.relative_to(cache_root)
        except ValueError as exc:
            raise ValueError(f"拒绝清理 cache/xhs 之外的路径: {path}") from exc
        if path.is_dir():
            shutil.rmtree(path)
            removed += 1
        elif path.exists():
            path.unlink()
            removed += 1
    cache_root.mkdir(parents=True, exist_ok=True)
    return removed


def finalize_xhs_classification_job(
    root: Path,
    job_path: Path,
    result_path: Path,
    *,
    codex_status: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Commit valid lines and always clear the cache entries captured by the job."""

    root = root.resolve()
    job = _load_json(job_path)
    job_items = {
        str(item.get("item_id")): item
        for item in job.get("items", [])
        if isinstance(item, dict) and item.get("item_id")
    }
    outputs: list[str] = []
    seen: set[str] = set()
    failures: list[str] = []
    failure_reasons: dict[str, list[str]] = {item_id: [] for item_id in job_items}
    target_values: dict[str, list[str]] = {item_id: [] for item_id in job_items}
    result_items: dict[str, dict[str, Any]] = {}
    result_error = ""

    def add_failure(item_id: str, reason: object) -> None:
        rendered = str(reason or "分类失败").replace("\n", " ").strip()
        failures.append(f"{item_id}: {rendered}")
        failure_reasons.setdefault(item_id, []).append(rendered)

    try:
        result = _load_json(result_path)
        for item in result.get("items", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("item_id", ""))
            if item_id in result_items:
                add_failure(item_id, "分类结果重复")
                continue
            result_items[item_id] = item
        for item_id in job_items:
            item = result_items.get(item_id)
            if item is None:
                add_failure(item_id, "缺少分类结果")
                continue
            status = str(item.get("status", "")).lower()
            if status != "success":
                add_failure(item_id, item.get("reason") or "分类失败")
                continue
            targets = item.get("targets", [])
            if not isinstance(targets, list) or not targets:
                add_failure(item_id, "success 没有 targets")
                continue
            item_valid = 0
            for target in targets:
                if not isinstance(target, dict):
                    add_failure(item_id, "target 不是对象")
                    continue
                kind = str(target.get("type", "")).strip().lower()
                value = target.get("value")
                try:
                    if kind == "arxiv":
                        normalized = normalize_arxiv_id(str(value or ""))
                    elif kind == "blog":
                        normalized = _safe_blog_url(value)
                    else:
                        raise ValueError(f"未知 target 类型: {kind}")
                except ValueError as exc:
                    add_failure(item_id, exc)
                    continue
                if normalized not in seen:
                    seen.add(normalized)
                    outputs.append(normalized)
                target_values[item_id].append(normalized)
                item_valid += 1
            if not item_valid:
                add_failure(item_id, "没有有效 target")
    except ValueError as exc:
        result_error = str(exc)
        for item_id in job_items:
            add_failure(item_id, result_error)
    finally:
        removed = _cleanup_job_cache(root, job)

    item_details = []
    for item_id, job_item in job_items.items():
        values = target_values.get(item_id, [])
        reasons = failure_reasons.get(item_id, [])
        if values and reasons:
            item_status = "partial"
        elif values:
            item_status = "success"
        else:
            item_status = "failed"
        item_details.append(
            {
                "item_id": item_id,
                "title": str(job_item.get("title") or "（原帖标题未能获取）"),
                "status": item_status,
                "targets": values,
                "reason": "; ".join(reasons),
            }
        )

    manual_path = _manual_output_path(root, now)
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# AirHub XHS 链接识别结果",
        f"# 完成时间: {utc_now_iso()}",
        f"# Codex 退出码: {codex_status}",
        f"# 缓存帖子: {len(job_items)}; 有效条目: {len(outputs)}; 失败: {len(failures)}",
    ]
    for detail in item_details:
        status_label = {
            "success": "成功",
            "partial": "部分成功",
            "failed": "失败",
        }[detail["status"]]
        result_text = ", ".join(detail["targets"]) or detail["reason"] or "无有效结果"
        header.append(
            f"# {status_label}: {detail['item_id']} | {detail['title']} | {result_text}"
        )
    manual_path.write_text(
        "\n".join([*header, *outputs]) + "\n", encoding="utf-8"
    )
    work_dir = job_path.parent
    summary_path = work_dir / "summary.txt"
    summary_path.write_text(
        "\n".join(
            (
                "AirHub XHS classifier summary",
                f"job={relative_to_root(job_path, root)}",
                f"result={relative_to_root(result_path, root)}",
                f"manual={relative_to_root(manual_path, root)}",
                f"codex_status={codex_status}",
                f"items={len(job_items)}",
                f"outputs={len(outputs)}",
                f"failures={len(failures)}",
                f"cache_entries_removed={removed}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    failed_items = sum(detail["status"] == "failed" for detail in item_details)
    partial_items = sum(detail["status"] == "partial" for detail in item_details)
    if codex_status == 0 and failed_items == 0 and partial_items == 0:
        activity_status = "success"
    elif outputs:
        activity_status = "partial"
    else:
        activity_status = "failed"
    try:
        started_at = datetime.fromisoformat(str(job.get("created_at", "")))
    except ValueError:
        started_at = None
    activity = record_xhs_completion(
        root,
        "classify",
        activity_status,
        started_at=started_at,
        details={
            "items": len(job_items),
            "outputs": len(outputs),
            "failed_items": failed_items,
            "partial_items": partial_items,
            "codex_status": codex_status,
            "manual": relative_to_root(manual_path, root),
            "summary": relative_to_root(summary_path, root),
        },
    )
    return {
        "manual": relative_to_root(manual_path, root),
        "summary": relative_to_root(summary_path, root),
        "items": len(job_items),
        "outputs": len(outputs),
        "failures": len(failures),
        "cache_entries_removed": removed,
        "result_error": result_error,
        "item_details": item_details,
        "daily_sequence": activity.count,
        "completed_at": activity.last_completed_at,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare/finalize XHS classification")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--finalize", nargs=2, metavar=("JOB", "RESULT"))
    parser.add_argument("--codex-status", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.prepare:
        job = prepare_xhs_classification_job(root)
        if job is None:
            record_xhs_completion(
                root,
                "classify",
                "empty",
                details={"items": 0, "outputs": 0},
            )
            return
        print(relative_to_root(job.job_path, root))
        print(relative_to_root(job.result_path, root))
        print(job.item_count)
        return
    if args.finalize:
        job_path = Path(args.finalize[0])
        result_path = Path(args.finalize[1])
        if not job_path.is_absolute():
            job_path = root / job_path
        if not result_path.is_absolute():
            result_path = root / result_path
        stats = finalize_xhs_classification_job(
            root,
            job_path.resolve(),
            result_path.resolve(),
            codex_status=args.codex_status,
        )
        total = len(stats["item_details"])
        for index, detail in enumerate(stats["item_details"], start=1):
            status_label = {
                "success": "成功",
                "partial": "部分成功",
                "failed": "失败",
            }[detail["status"]]
            result_text = ", ".join(detail["targets"]) or detail["reason"] or "无有效结果"
            print(
                f"[RESULT] XHS {index}/{total} {status_label} | "
                f"{detail['title']} | {result_text}"
            )
        print(json.dumps(stats, ensure_ascii=False))
        return
    parser.error("必须指定 --prepare 或 --finalize")


if __name__ == "__main__":
    main()
