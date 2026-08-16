"""Article filtering, including institution and country filters."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .models import Article


FILTER_FIELDS = ("keywords", "tags", "sources", "types", "countries", "institutions")
LOCATION_FIELDS = ("countries", "institutions")
COUNTRY_ALIASES = {
    "america": "United States",
    "china": "China",
    "cn": "China",
    "england": "United Kingdom",
    "great britain": "United Kingdom",
    "japan": "Japan",
    "jp": "Japan",
    "pr china": "China",
    "prc": "China",
    "switzerland": "Switzerland",
    "uk": "United Kingdom",
    "u k": "United Kingdom",
    "united kingdom": "United Kingdom",
    "united states": "United States",
    "united states of america": "United States",
    "us": "United States",
    "usa": "United States",
    "u s": "United States",
    "u s a": "United States",
}
INSTITUTION_ALIASES = {
    "mit": "massachusetts institute of technology",
    "m i t": "massachusetts institute of technology",
    "university of mit": "massachusetts institute of technology",
    "stanford": "stanford university",
    "university of stanford": "stanford university",
    "uc berkeley": "university of california berkeley",
    "university of berkeley": "university of california berkeley",
}


def normalize_phrase(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def canonical_country(value: str) -> str:
    normalized = normalize_phrase(value)
    return COUNTRY_ALIASES.get(normalized, value.strip())


def canonical_institution(value: str) -> str:
    normalized = normalize_phrase(value)
    return INSTITUTION_ALIASES.get(normalized, normalized)


def phrase_in_text(phrase: str, normalized_text: str) -> bool:
    normalized_phrase = normalize_phrase(phrase)
    if not normalized_phrase:
        return False
    return re.search(
        rf"(?:^|\s){re.escape(normalized_phrase)}(?:$|\s)", normalized_text
    ) is not None


@dataclass
class FilterEvaluation:
    field: str
    configured: list[str]
    actual: list[str]
    matched: list[str]
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "configured": self.configured,
            "actual": self.actual,
            "matched": self.matched,
            "outcome": self.outcome,
        }


@dataclass
class FilterResult:
    included: bool
    reasons: list[str]
    evaluations: list[FilterEvaluation] = field(default_factory=list)


class FilterEngine:
    def __init__(self, config: dict[str, Any], institution_country: dict[str, str]) -> None:
        self.config = config or {}
        self.unknown_policy = str(self.config.get("unknown_policy", "exclude")).lower()
        self.institution_country = {
            canonical_institution(str(key)): canonical_country(str(value))
            for key, value in institution_country.items()
        }
        self.known_institutions = self._known_institutions()

    def enrich(self, article: Article) -> Article:
        metadata = article.metadata
        affiliation_text = self._affiliation_text(article)
        normalized_text = normalize_phrase(affiliation_text)

        institutions: set[str] = set()
        for institution in self.known_institutions:
            aliases = [institution] + [
                alias
                for alias, canonical in INSTITUTION_ALIASES.items()
                if canonical == institution
            ]
            if any(phrase_in_text(alias, normalized_text) for alias in aliases):
                institutions.add(institution)

        for value in metadata.get("source_institutions", []):
            if value:
                institutions.add(canonical_institution(str(value)))

        countries: set[str] = set()
        for alias, country in COUNTRY_ALIASES.items():
            if len(alias.replace(" ", "")) > 2 and phrase_in_text(alias, normalized_text):
                countries.add(country)
        for institution in institutions:
            country = self.institution_country.get(institution)
            if country:
                countries.add(country)
        for value in metadata.get("source_countries", []):
            if value:
                countries.add(canonical_country(str(value)))

        metadata["institutions"] = sorted(institutions)
        metadata["affiliations"] = sorted(institutions)
        metadata["countries"] = sorted(countries)
        return article

    def apply(self, article: Article) -> FilterResult:
        self.enrich(article)
        evaluations: list[FilterEvaluation] = []

        for field_name in FILTER_FIELDS:
            configured = self._values("exclude", field_name)
            if not configured:
                continue
            actual = self._evaluation_values(field_name, article)
            matched = self._matches(field_name, configured, article)
            evaluations.append(
                FilterEvaluation(
                    field=field_name,
                    configured=configured,
                    actual=actual,
                    matched=matched,
                    outcome="excluded" if matched else "passed",
                )
            )
            if matched:
                return FilterResult(
                    False,
                    [f"excluded by {field_name}: {', '.join(matched)}"],
                    evaluations,
                )

        reasons: list[str] = []
        for field_name in ("keywords", "tags", "sources", "types"):
            configured = self._values("include", field_name)
            if not configured:
                continue
            actual = self._evaluation_values(field_name, article)
            matched = self._matches(field_name, configured, article)
            evaluations.append(
                FilterEvaluation(
                    field=field_name,
                    configured=configured,
                    actual=actual,
                    matched=matched,
                    outcome="included" if matched else "missing",
                )
            )
            if not matched:
                return FilterResult(
                    False,
                    [f"missing required include match for {field_name}"],
                    evaluations,
                )
            reasons.append(f"included by {field_name}: {', '.join(matched)}")

        location_configured = {
            field_name: self._values("include", field_name)
            for field_name in LOCATION_FIELDS
        }
        if any(location_configured.values()):
            location_matches: list[str] = []
            location_actual: list[str] = []
            for field_name in LOCATION_FIELDS:
                configured = location_configured[field_name]
                if not configured:
                    continue
                actual = self._evaluation_values(field_name, article)
                matched = self._matches(field_name, configured, article)
                location_actual.extend(actual)
                location_matches.extend(f"{field_name}:{value}" for value in matched)
                evaluations.append(
                    FilterEvaluation(
                        field=field_name,
                        configured=configured,
                        actual=actual,
                        matched=matched,
                        outcome="included" if matched else "missing",
                    )
                )
            if location_matches:
                reasons.append(f"included by location: {', '.join(location_matches)}")
            elif not location_actual and self.unknown_policy == "include":
                reasons.append("included by unknown location policy")
            else:
                detail = "unknown location" if not location_actual else "no target matched"
                return FilterResult(False, [f"location filter failed: {detail}"], evaluations)

        return FilterResult(True, reasons or ["included by default"], evaluations)

    def _known_institutions(self) -> list[str]:
        names = set(self.institution_country)
        for section in ("include", "exclude"):
            for value in self._values(section, "institutions"):
                names.add(canonical_institution(value))
        return sorted(item for item in names if item)

    def _values(self, section: str, field_name: str) -> list[str]:
        values = (self.config.get(section, {}) or {}).get(field_name, []) or []
        return [str(item) for item in values]

    def _matches(self, field_name: str, needles: list[str], article: Article) -> list[str]:
        haystack_values = self._field_values(field_name, article)
        if field_name == "countries":
            actual = {canonical_country(value).casefold() for value in haystack_values}
            return [value for value in needles if canonical_country(value).casefold() in actual]
        if field_name == "institutions":
            actual = {canonical_institution(value) for value in haystack_values}
            return [value for value in needles if canonical_institution(value) in actual]
        if field_name in {"tags", "sources", "types"}:
            actual = {normalize_phrase(value) for value in haystack_values}
            return [value for value in needles if normalize_phrase(value) in actual]

        haystack = normalize_phrase(" ".join(haystack_values))
        return [value for value in needles if normalize_phrase(value) in haystack]

    def _field_values(self, field_name: str, article: Article) -> list[str]:
        if field_name == "keywords":
            return [self._search_text(article)]
        if field_name == "tags":
            return article.tags
        if field_name == "sources":
            return [article.source]
        if field_name == "types":
            return [article.type]
        if field_name == "countries":
            return [str(item) for item in article.metadata.get("countries", [])]
        if field_name == "institutions":
            return [str(item) for item in article.metadata.get("institutions", [])]
        return []

    def _evaluation_values(self, field_name: str, article: Article) -> list[str]:
        values = self._field_values(field_name, article)
        if field_name == "keywords":
            return [f"searchable_text_chars={len(values[0]) if values else 0}"]
        return values

    def _search_text(self, article: Article) -> str:
        metadata = article.metadata
        return " ".join(
            [
                article.title,
                article.summary,
                article.note,
                " ".join(article.authors),
                " ".join(article.tags),
                str(metadata.get("abstract", "")),
            ]
        )

    @staticmethod
    def _affiliation_text(article: Article) -> str:
        return str(article.metadata.get("affiliation_source_text", "")).strip()
