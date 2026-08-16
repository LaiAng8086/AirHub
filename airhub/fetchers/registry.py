"""Fetcher registry."""

from __future__ import annotations

from pathlib import Path
from typing import Type

from airhub.config import SourceConfig

from .base import Fetcher


class FetcherRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, Type[Fetcher]] = {}

    def register(self, cls: Type[Fetcher]) -> Type[Fetcher]:
        self._classes[cls.source_type] = cls
        return cls

    def create(self, source: SourceConfig, root: Path) -> Fetcher:
        if source.type not in self._classes:
            raise KeyError(f"Unsupported source type: {source.type}")
        return self._classes[source.type](source, root)

    @property
    def source_types(self) -> list[str]:
        return sorted(self._classes)


registry = FetcherRegistry()
