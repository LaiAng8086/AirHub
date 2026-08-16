"""Prepare an isolated Codex home for DeepSeek's native Responses API."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .paths import PROJECT_ROOT


DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
REASONING_EFFORTS = {"low", "high", "max"}
API_KEY_RE = re.compile(r"sk-[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class DeepSeekCodexSettings:
    api_key: str
    reasoning_effort: str

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DeepSeekCodexSettings":
        unknown = set(payload) - {"api_key", "reasoning_effort"}
        if unknown:
            names = ", ".join(sorted(str(item) for item in unknown))
            raise ValueError(f"DeepSeek 配置包含未知字段: {names}")

        api_key = str(payload.get("api_key", "")).strip()
        if not api_key:
            raise ValueError("请先在 config/deepseek.json 中填写 api_key")
        if not API_KEY_RE.fullmatch(api_key):
            raise ValueError("DeepSeek api_key 格式无效，应为以 sk- 开头的密钥")

        reasoning_effort = str(payload.get("reasoning_effort", "high")).strip().lower()
        if reasoning_effort not in REASONING_EFFORTS:
            choices = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(f"reasoning_effort 必须是以下值之一: {choices}")
        return cls(api_key=api_key, reasoning_effort=reasoning_effort)


@dataclass(frozen=True)
class DeepSeekCodexRuntime:
    home: Path
    model: str
    reasoning_effort: str


def load_deepseek_settings(root: Path = PROJECT_ROOT) -> DeepSeekCodexSettings:
    path = root.resolve() / "config" / "deepseek.json"
    if not path.is_file():
        raise FileNotFoundError(
            "缺少 config/deepseek.json；请参考 config/deepseek.example.json 创建"
        )
    os.chmod(path, 0o600)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取 config/deepseek.json") from exc
    if not isinstance(payload, dict):
        raise ValueError("config/deepseek.json 必须是 JSON 对象")
    return DeepSeekCodexSettings.from_dict(payload)


def _validate_model_catalog(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 DeepSeek Codex 模型目录: {path}") from exc
    models = payload.get("models", []) if isinstance(payload, dict) else []
    if not any(
        isinstance(model, dict) and model.get("slug") == DEEPSEEK_MODEL
        for model in models
    ):
        raise ValueError(f"DeepSeek Codex 模型目录缺少 {DEEPSEEK_MODEL}")


def _copy_skill(source: Path, destination: Path) -> None:
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(f"Codex skill 源不存在: {source}")
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
        elif source_path.is_file():
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def prepare_deepseek_codex_home(root: Path = PROJECT_ROOT) -> DeepSeekCodexRuntime:
    root = root.resolve()
    settings = load_deepseek_settings(root)
    catalog_source = root / "config" / "deepseek_codex_models.json"
    _validate_model_catalog(catalog_source)

    runtime_home = root / "cache" / "deepseek_codex_home"
    runtime_home.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_home, 0o700)
    catalog_path = runtime_home / "models.json"
    shutil.copy2(catalog_source, catalog_path)
    skills_root = root / "codex_skills"
    required_skills = {"paper-digest", "podcast-transcript-polisher"}
    for skill_name in (
        "paper-digest",
        "xhs-link-classifier",
        "podcast-transcript-polisher",
    ):
        source = skills_root / skill_name
        if source.is_dir():
            _copy_skill(source, runtime_home / "skills" / skill_name)
        elif skill_name in required_skills:
            raise FileNotFoundError(f"Codex skill 源不存在: {source}")

    config_path = runtime_home / "config.toml"
    config_text = "\n".join(
        (
            f'model = {_toml_string(DEEPSEEK_MODEL)}',
            'model_provider = "deepseek"',
            'preferred_auth_method = "apikey"',
            'forced_login_method = "api"',
            f'model_reasoning_effort = {_toml_string(settings.reasoning_effort)}',
            f'model_catalog_json = {_toml_string(str(catalog_path))}',
            'approval_policy = "never"',
            'sandbox_mode = "workspace-write"',
            '',
            '[model_providers.deepseek]',
            'name = "deepseek"',
            f'base_url = {_toml_string(DEEPSEEK_BASE_URL)}',
            'wire_api = "responses"',
            f'experimental_bearer_token = {_toml_string(settings.api_key)}',
            '',
        )
    )
    temporary = config_path.with_suffix(".toml.tmp")
    temporary.write_text(config_text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(config_path)
    os.chmod(config_path, 0o600)
    return DeepSeekCodexRuntime(
        home=runtime_home,
        model=DEEPSEEK_MODEL,
        reasoning_effort=settings.reasoning_effort,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare isolated DeepSeek Codex config")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    args = parser.parse_args()
    try:
        runtime = prepare_deepseek_codex_home(Path(args.root))
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(runtime.home)
    print(runtime.model)
    print(runtime.reasoning_effort)


if __name__ == "__main__":
    main()
