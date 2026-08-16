"""Fetcher plugin interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from airhub.config import SourceConfig
from airhub.logging_utils import get_producer_logger
from airhub.models import Article


class Fetcher(ABC):
    source_type: str

    def __init__(self, source: SourceConfig, root: Path) -> None:
        self.source = source
        self.root = root
        self.logger = get_producer_logger()

    def _event(self, status: str, stage: str, message: str) -> None:
        payload = f"stage={stage} status={status} source={self.source.name} {message}"
        if status in {"WARN", "ERROR"}:
            self.logger.warning(payload)
        else:
            self.logger.info(payload)

    @abstractmethod
    def fetch(self, limit: int | None = None) -> list[Article]:
        """Fetch and normalize source records into Article objects."""

    def prepare_for_priority(self, article: Article) -> Article:
        """Prepare lightweight evidence used to rank a candidate.

        Implementations must not download heavyweight attachments such as PDFs.
        """

        return article

    def prepare(self, article: Article) -> Article:
        """Prepare attachments for a candidate selected for processing."""

        return article
