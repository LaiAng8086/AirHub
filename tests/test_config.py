import tempfile
import unittest
from pathlib import Path

from airhub.config import (
    AppSettings,
    load_config,
    load_settings,
    load_simple_yaml,
    normalize_filters,
    save_settings,
)


class ConfigTest(unittest.TestCase):
    def test_simple_yaml_empty_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filters.yaml"
            path.write_text(
                "include:\n"
                "  keywords: []\n"
                "exclude:\n"
                "  countries:\n"
                "    - US\n"
                "unknown_policy: include\n",
                encoding="utf-8",
            )
            data = load_simple_yaml(path, {})
            self.assertEqual(data["include"]["keywords"], [])
            self.assertEqual(data["exclude"]["countries"], ["US"])
            self.assertEqual(data["unknown_policy"], "include")

    def test_inline_json_list_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filters.yaml"
            path.write_text('include:\n  countries: ["China", "United States"]\n')
            data = load_simple_yaml(path, {})
            self.assertEqual(data["include"]["countries"], ["China", "United States"])

    def test_filter_values_must_be_string_lists(self):
        with self.assertRaisesRegex(
            ValueError, r"filters\.include\.countries must be a list of strings"
        ):
            normalize_filters({"include": {"countries": "China"}})

    def test_unknown_filter_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported filters.include fields"):
            normalize_filters({"include": {"country": ["China"]}})

    def test_repo_filter_config_loads_valid_lists(self):
        config = load_config(Path(__file__).resolve().parents[1])
        self.assertEqual(config.filters["include"]["countries"], ["China", "United States"])
        self.assertIn(
            "Massachusetts Institute of Technology",
            config.filters["include"]["institutions"],
        )
        self.assertEqual(config.filters["unknown_policy"], "exclude")

    def test_settings_round_trip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_settings(AppSettings(daily_article_limit=37), root)
            self.assertEqual(load_settings(root).daily_article_limit, 37)
        with self.assertRaisesRegex(ValueError, "between 1 and 10000"):
            AppSettings.from_dict({"daily_article_limit": 0})


if __name__ == "__main__":
    unittest.main()
