"""Persistent per-day execution history for AirHub XHS actions."""

from __future__ import annotations

import argparse
import fcntl
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT


ACTION_LABELS = {
    "download": "帖子文本下载",
    "classify": "缓存链接识别",
}
STATUS_LABELS = {
    "success": "成功",
    "partial": "部分成功",
    "failed": "失败",
    "empty": "无待处理缓存",
}


@dataclass(frozen=True)
class XHSDailyActivity:
    day: str
    count: int
    last_completed_at: str
    last_action: str
    last_status: str
    events: tuple[dict[str, Any], ...]

    @property
    def next_sequence(self) -> int:
        return self.count + 1


def _local_datetime(value: datetime | None = None) -> datetime:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        return current.astimezone()
    return current.astimezone()


def _state_path(root: Path, day: str) -> Path:
    return root.resolve() / "data" / "xhs" / "daily_runs" / f"{day}.json"


def _empty_activity(day: str) -> XHSDailyActivity:
    return XHSDailyActivity(day, 0, "", "", "", ())


def load_xhs_daily_activity(
    root: Path = PROJECT_ROOT,
    *,
    on_date: date | str | None = None,
) -> XHSDailyActivity:
    if isinstance(on_date, date):
        day = on_date.isoformat()
    elif on_date:
        day = str(on_date)
    else:
        day = _local_datetime().date().isoformat()
    path = _state_path(root, day)
    if not path.is_file():
        return _empty_activity(day)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"XHS 每日执行流水损坏: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events", []), list):
        raise ValueError(f"XHS 每日执行流水格式错误: {path}")
    events = tuple(item for item in payload.get("events", []) if isinstance(item, dict))
    last = events[-1] if events else {}
    return XHSDailyActivity(
        day=day,
        count=len(events),
        last_completed_at=str(last.get("completed_at", "")),
        last_action=str(last.get("action", "")),
        last_status=str(last.get("status", "")),
        events=events,
    )


def record_xhs_completion(
    root: Path,
    action: str,
    status: str,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> XHSDailyActivity:
    """Append one completed action and return the updated local-day history."""

    if action not in ACTION_LABELS:
        raise ValueError(f"未知 XHS 动作: {action}")
    if status not in STATUS_LABELS:
        raise ValueError(f"未知 XHS 执行状态: {status}")
    completed = _local_datetime(completed_at)
    started = _local_datetime(started_at or completed)
    day = completed.date().isoformat()
    path = _state_path(root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".daily_runs.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = load_xhs_daily_activity(root, on_date=day)
        event = {
            "sequence": current.next_sequence,
            "action": action,
            "action_label": ACTION_LABELS[action],
            "status": status,
            "started_at": started.isoformat(timespec="seconds"),
            "completed_at": completed.isoformat(timespec="seconds"),
            "details": details or {},
        }
        events = [*current.events, event]
        payload = {
            "version": 1,
            "date": day,
            "count": len(events),
            "last_completed_at": event["completed_at"],
            "events": events,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return load_xhs_daily_activity(root, on_date=day)


def format_xhs_daily_activity(activity: XHSDailyActivity) -> str:
    if not activity.count:
        return "今日 XHS 尚未执行；下一次为 #1。"
    try:
        completed = datetime.fromisoformat(activity.last_completed_at).astimezone()
        completed_text = completed.strftime("%H:%M:%S")
    except ValueError:
        completed_text = activity.last_completed_at or "未知"
    action = ACTION_LABELS.get(activity.last_action, activity.last_action or "未知动作")
    status = STATUS_LABELS.get(activity.last_status, activity.last_status or "未知")
    return (
        f"今日 XHS 已执行 {activity.count} 次；上次 #{activity.count} "
        f"{action} 于 {completed_text} 完成（{status}）；下一次 #{activity.next_sequence}。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Show today's XHS execution history")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--print-status", action="store_true")
    args = parser.parse_args()
    if not args.print_status:
        parser.error("必须指定 --print-status")
    print(format_xhs_daily_activity(load_xhs_daily_activity(Path(args.root))))


if __name__ == "__main__":
    main()
