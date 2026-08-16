"""Date-scoped workflow state for the integrated AirHub console."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .models import utc_now_iso
from .paths import PROJECT_ROOT, ensure_runtime_dirs


STEP_NAMES = (
    "settings_updated",
    "priority_updated",
    "discovered",
    "strategy_applied",
    "digested",
    "embedded",
)


def _initial_state(root: Path, run_date: str) -> dict[str, Any]:
    steps = {name: False for name in STEP_NAMES}
    strategy: str | None = None
    legacy_report = root / "data" / "priority" / f"{run_date}.json"
    if legacy_report.exists():
        try:
            payload = json.loads(legacy_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("selection_history"):
            strategy = "priority"
            steps["strategy_applied"] = True
    return {
        "version": 1,
        "date": run_date,
        "strategy": strategy,
        "steps": steps,
        "counts": {},
        "updated_at": utc_now_iso(),
    }


def state_path(root: Path = PROJECT_ROOT) -> Path:
    return root / "data" / "state" / "current.json"


def load_daily_state(
    root: Path = PROJECT_ROOT,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Load today's state; a date change starts from an initial state."""

    root = root.resolve()
    run_date = run_date or date.today().isoformat()
    ensure_runtime_dirs(root)
    path = state_path(root)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("date") == run_date:
            steps = payload.setdefault("steps", {})
            for name in STEP_NAMES:
                steps.setdefault(name, False)
            payload.setdefault("strategy", None)
            payload.setdefault("counts", {})
            return payload
    payload = _initial_state(root, run_date)
    save_daily_state(payload, root)
    return payload


def save_daily_state(state: dict[str, Any], root: Path = PROJECT_ROOT) -> Path:
    path = state_path(root.resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now_iso()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def mark_step(
    step: str,
    root: Path = PROJECT_ROOT,
    *,
    done: bool = True,
    strategy: str | None = None,
    counts: dict[str, Any] | None = None,
    run_date: str | None = None,
) -> dict[str, Any]:
    if step not in STEP_NAMES:
        raise ValueError(f"未知每日步骤: {step}")
    state = load_daily_state(root, run_date)
    state["steps"][step] = done
    if strategy is not None:
        state["strategy"] = strategy
    if counts:
        state["counts"].update(counts)
    save_daily_state(state, root)
    return state

