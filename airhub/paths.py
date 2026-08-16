"""Project path helpers for AirHub."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


RUNTIME_DIRS = (
    "config",
    "inbox",
    "processing",
    "finished",
    "filtered",
    "attachments/pdf",
    "attachments/html",
    "attachments/source",
    "attachments/image",
    "attachments/blog",
    "attachments/audio",
    "attachments/transcript",
    "metadata/articles",
    "data/priority",
    "data/candidates",
    "data/selection",
    "data/article_cache",
    "data/manual",
    "data/blog/archives",
    "data/podcast/jobs",
    "data/state",
    "manual",
    "logs",
    "cache",
    "cache/xhs",
)


def ensure_runtime_dirs(root: Path = PROJECT_ROOT) -> None:
    """Create the file-system queues and attachment directories."""

    for relative in RUNTIME_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)


def relative_to_root(path: Path, root: Path = PROJECT_ROOT) -> str:
    """Return a stable POSIX-style path relative to the project root."""

    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
