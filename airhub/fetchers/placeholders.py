"""Phase 2 placeholder fetchers for future source types."""

from __future__ import annotations

from airhub.models import Article

from .base import Fetcher


class _PlaceholderFetcher(Fetcher):
    source_type = "placeholder"

    def fetch(self, limit: int | None = None) -> list[Article]:
        self._event("WARN", "fetch", f"type={self.source.type} implemented=false")
        return []


class HuggingFaceFetcher(_PlaceholderFetcher):
    source_type = "huggingface"


class PodcastFetcher(_PlaceholderFetcher):
    source_type = "podcast"
