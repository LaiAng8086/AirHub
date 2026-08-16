"""Configuration loading for AirHub producer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT


FILTER_FIELDS = ("keywords", "tags", "sources", "types", "countries", "institutions")
UNKNOWN_POLICIES = {"include", "exclude"}


@dataclass
class SourceConfig:
    type: str
    name: str = ""
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceConfig":
        source_type = str(data.get("type", "")).strip().lower()
        name = str(data.get("name") or data.get("query") or data.get("rss") or source_type)
        enabled = bool(data.get("enabled", True))
        options = {key: value for key, value in data.items() if key not in {"type", "name", "enabled"}}
        return cls(type=source_type, name=name, enabled=enabled, options=options)


@dataclass
class ProducerConfig:
    sources: list[SourceConfig]
    filters: dict[str, Any]
    html: dict[str, Any]
    institution_country: dict[str, str]


@dataclass
class AppSettings:
    """用户可通过统一命令行界面修改的持久化设置。"""

    daily_article_limit: int = 20

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        raw_limit = data.get("daily_article_limit", 20)
        if isinstance(raw_limit, bool):
            raise ValueError("daily_article_limit must be an integer")
        try:
            daily_article_limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("daily_article_limit must be an integer") from exc
        if not 1 <= daily_article_limit <= 10000:
            raise ValueError("daily_article_limit must be between 1 and 10000")
        return cls(daily_article_limit=daily_article_limit)

    def to_dict(self) -> dict[str, int]:
        return {"daily_article_limit": self.daily_article_limit}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, list):
                return parsed
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def normalize_filters(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the filter configuration."""

    unknown_sections = set(config) - {"include", "exclude", "unknown_policy"}
    if unknown_sections:
        names = ", ".join(sorted(str(item) for item in unknown_sections))
        raise ValueError(f"unsupported filter configuration fields: {names}")
    normalized: dict[str, Any] = {}
    for section in ("include", "exclude"):
        raw_section = config.get(section, {}) or {}
        if not isinstance(raw_section, dict):
            raise ValueError(f"filters.{section} must be a mapping")
        normalized_section: dict[str, list[str]] = {}
        for field_name in FILTER_FIELDS:
            raw_values = raw_section.get(field_name, []) or []
            if not isinstance(raw_values, list) or any(
                not isinstance(item, str) for item in raw_values
            ):
                raise ValueError(
                    f"filters.{section}.{field_name} must be a list of strings"
                )
            normalized_section[field_name] = list(
                dict.fromkeys(item.strip() for item in raw_values if item.strip())
            )
        unknown_fields = set(raw_section) - set(FILTER_FIELDS)
        if unknown_fields:
            names = ", ".join(sorted(str(item) for item in unknown_fields))
            raise ValueError(f"unsupported filters.{section} fields: {names}")
        normalized[section] = normalized_section

    unknown_policy = str(config.get("unknown_policy", "exclude")).strip().lower()
    if unknown_policy not in UNKNOWN_POLICIES:
        choices = ", ".join(sorted(UNKNOWN_POLICIES))
        raise ValueError(f"filters.unknown_policy must be one of: {choices}")
    normalized["unknown_policy"] = unknown_policy
    return normalized


def load_simple_yaml(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Load the small YAML subset used by AirHub config files.

    Supported forms are nested mappings and list items:
        include:
          keywords:
            - robotics
    """

    if not path.exists():
        return default
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    last_key_at_indent: dict[int, tuple[Any, str]] = {}

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        text = raw_line.strip()
        while stack and (
            indent < stack[-1][0]
            or (
                indent == stack[-1][0]
                and not (text.startswith("- ") and isinstance(stack[-1][1], list))
            )
        ):
            stack.pop()
        parent = stack[-1][1]

        if text.startswith("- "):
            value = parse_scalar(text[2:])
            if not isinstance(parent, list):
                container, key = last_key_at_indent[indent]
                container[key] = []
                parent = container[key]
                stack.append((indent, parent))
            parent.append(value)
            continue

        key, _, value_text = text.partition(":")
        key = key.strip()
        if value_text.strip():
            parent[key] = parse_scalar(value_text)
        else:
            parent[key] = {}
            stack.append((indent, parent[key]))
            last_key_at_indent[indent + 2] = (parent, key)
    return root


def load_config(root: Path = PROJECT_ROOT) -> ProducerConfig:
    config_dir = root / "config"
    sources_raw = load_json(config_dir / "sources.json", {"sources": []})
    filters = normalize_filters(load_simple_yaml(config_dir / "filters.yaml", {}))
    html = load_simple_yaml(config_dir / "html.yaml", {})
    institution_country = load_simple_yaml(config_dir / "institution_country.yaml", {})
    return ProducerConfig(
        sources=[SourceConfig.from_dict(item) for item in sources_raw.get("sources", [])],
        filters=filters,
        html=html,
        institution_country={
            str(key).lower(): str(value) for key, value in institution_country.items()
        },
    )


def load_settings(root: Path = PROJECT_ROOT) -> AppSettings:
    payload = load_json(root / "config" / "settings.json", {})
    if not isinstance(payload, dict):
        raise ValueError("config/settings.json must contain a JSON object")
    return AppSettings.from_dict(payload)


def save_settings(settings: AppSettings, root: Path = PROJECT_ROOT) -> Path:
    path = root / "config" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(settings.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(path)
    return path
