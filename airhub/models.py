"""Core Article data model.

The on-disk JSON shape intentionally follows Design/v1_0.md and keeps a
free-form metadata bag for source-specific fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ArticleStatus:
    processed: bool = False
    read: bool = False
    favorite: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ArticleStatus":
        data = data or {}
        return cls(
            processed=bool(data.get("processed", False)),
            read=bool(data.get("read", False)),
            favorite=bool(data.get("favorite", False)),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "processed": self.processed,
            "read": self.read,
            "favorite": self.favorite,
        }


@dataclass
class Attachment:
    type: str
    path: str
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attachment":
        return cls(
            type=str(data.get("type", "")),
            path=str(data.get("path", "")),
            title=str(data.get("title", "")),
            metadata=dict(data.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "path": self.path}
        if self.title:
            payload["title"] = self.title
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass
class Article:
    id: str
    type: str
    source: str
    title: str
    authors: list[str] = field(default_factory=list)
    publish_date: str = ""
    fetch_date: str = field(default_factory=utc_now_iso)
    url: str = ""
    tags: list[str] = field(default_factory=list)
    status: ArticleStatus = field(default_factory=ArticleStatus)
    attachments: list[Attachment] = field(default_factory=list)
    html: str = ""
    summary: str = ""
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Article":
        return cls(
            id=str(data["id"]),
            type=str(data.get("type", "")),
            source=str(data.get("source", "")),
            title=str(data.get("title", "")),
            authors=[str(item) for item in data.get("authors", [])],
            publish_date=str(data.get("publish_date", "")),
            fetch_date=str(data.get("fetch_date", "")) or utc_now_iso(),
            url=str(data.get("url", "")),
            tags=[str(item) for item in data.get("tags", [])],
            status=ArticleStatus.from_dict(data.get("status")),
            attachments=[
                Attachment.from_dict(item) for item in data.get("attachments", [])
            ],
            html=str(data.get("html", "")),
            summary=str(data.get("summary", "")),
            note=str(data.get("note", "")),
            metadata=dict(data.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "title": self.title,
            "authors": self.authors,
            "publish_date": self.publish_date,
            "fetch_date": self.fetch_date,
            "url": self.url,
            "tags": self.tags,
            "status": self.status.to_dict(),
            "attachments": [item.to_dict() for item in self.attachments],
            "html": self.html,
            "summary": self.summary,
            "note": self.note,
            "metadata": self.metadata,
        }

    def merge_preserving_user_state(self, existing: "Article") -> "Article":
        """Carry viewer/consumer state across producer refreshes."""

        self.status = existing.status
        self.html = existing.html
        self.summary = existing.summary
        self.note = existing.note
        return self
