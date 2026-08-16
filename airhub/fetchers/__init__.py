"""Built-in fetcher plugins."""

from .arxiv import ArxivFetcher
from .blog import BlogFetcher
from .placeholders import HuggingFaceFetcher, PodcastFetcher
from .registry import registry

for fetcher_cls in (ArxivFetcher, BlogFetcher, HuggingFaceFetcher, PodcastFetcher):
    registry.register(fetcher_cls)

__all__ = [
    "ArxivFetcher",
    "BlogFetcher",
    "HuggingFaceFetcher",
    "PodcastFetcher",
    "registry",
]
