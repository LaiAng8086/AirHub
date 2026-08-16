from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from airhub.deepseek_codex import (
    DEEPSEEK_MODEL,
    DeepSeekCodexSettings,
    load_deepseek_settings,
    prepare_deepseek_codex_home,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def prepare_root(root: Path, effort: str = "high") -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "deepseek.json").write_text(
        json.dumps(
            {
                "api_key": "sk-" + "test_" + "1234567890",
                "reasoning_effort": effort,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (config_dir / "deepseek_codex_models.json").write_text(
        (PROJECT_ROOT / "config" / "deepseek_codex_models.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    skill_dir = root / "codex_skills" / "paper-digest"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# paper-digest\n", encoding="utf-8")
    classifier_dir = root / "codex_skills" / "xhs-link-classifier"
    classifier_dir.mkdir(parents=True)
    (classifier_dir / "SKILL.md").write_text(
        "# xhs-link-classifier\n", encoding="utf-8"
    )
    podcast_dir = root / "codex_skills" / "podcast-transcript-polisher"
    podcast_dir.mkdir(parents=True)
    (podcast_dir / "SKILL.md").write_text(
        "# podcast-transcript-polisher\n", encoding="utf-8"
    )


class DeepSeekCodexTest(unittest.TestCase):
    def test_settings_accept_only_supported_effort_and_safe_key(self):
        settings = DeepSeekCodexSettings.from_dict(
            {"api_key": "sk-" + "safe_key.123", "reasoning_effort": "MAX"}
        )
        self.assertEqual(settings.reasoning_effort, "max")
        with self.assertRaisesRegex(ValueError, "api_key 格式无效"):
            DeepSeekCodexSettings.from_dict(
                {"api_key": "not-a-key", "reasoning_effort": "high"}
            )
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            DeepSeekCodexSettings.from_dict(
                {"api_key": "sk-safe", "reasoning_effort": "xhigh"}
            )

    def test_load_rejects_empty_placeholder_without_leaking_a_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "deepseek.json").write_text(
                '{"api_key":"", "reasoning_effort":"high"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "填写 api_key"):
                load_deepseek_settings(root)

    def test_prepare_writes_isolated_codex_config_and_copies_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root, effort="max")

            runtime = prepare_deepseek_codex_home(root)

            self.assertEqual(runtime.model, DEEPSEEK_MODEL)
            self.assertEqual(runtime.reasoning_effort, "max")
            self.assertTrue((runtime.home / "skills" / "paper-digest" / "SKILL.md").is_file())
            self.assertTrue(
                (runtime.home / "skills" / "xhs-link-classifier" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (
                    runtime.home
                    / "skills"
                    / "podcast-transcript-polisher"
                    / "SKILL.md"
                ).is_file()
            )
            self.assertTrue((runtime.home / "models.json").is_file())
            config_path = runtime.home / "config.toml"
            config = config_path.read_text(encoding="utf-8")
            self.assertIn('base_url = "https://api.deepseek.com/"', config)
            self.assertIn('wire_api = "responses"', config)
            self.assertIn('model = "deepseek-v4-flash"', config)
            self.assertIn('model_reasoning_effort = "max"', config)
            self.assertIn('experimental_bearer_token = "sk-test_1234567890"', config)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE((root / "config" / "deepseek.json").stat().st_mode),
                0o600,
            )

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_generated_config_is_accepted_by_codex_without_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare_root(root)
            runtime = prepare_deepseek_codex_home(root)
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(runtime.home)

            completed = subprocess.run(
                ["codex", "features", "list"],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
