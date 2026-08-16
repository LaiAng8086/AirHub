import unittest

from airhub.filters import FilterEngine
from airhub.models import Article


class FilterEngineTest(unittest.TestCase):
    def test_keyword_include(self):
        engine = FilterEngine({"include": {"keywords": ["robotics"]}}, {})
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="Robotics Foundation Models",
        )
        result = engine.apply(article)
        self.assertTrue(result.included)

    def test_country_institution_filter(self):
        engine = FilterEngine(
            {"include": {"countries": ["China"]}},
            {"tsinghua university": "China"},
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="A Paper from Tsinghua University",
            metadata={
                "affiliation_source": "arxiv_html",
                "affiliation_source_text": "Alice, Tsinghua University",
            },
        )
        result = engine.apply(article)
        self.assertTrue(result.included)
        self.assertEqual(article.metadata["countries"], ["China"])

    def test_affiliation_source_text_drives_country_filter(self):
        engine = FilterEngine(
            {"include": {"countries": ["US"]}},
            {"massachusetts institute of technology": "US"},
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="A Paper from Tsinghua University",
            metadata={
                "affiliation_source": "arxiv_html",
                "affiliation_source_text": "Alice, Massachusetts Institute of Technology",
            },
        )
        result = engine.apply(article)
        self.assertTrue(result.included)
        self.assertEqual(article.metadata["countries"], ["United States"])
        self.assertIn("massachusetts institute of technology", article.metadata["institutions"])

    def test_country_or_institution_target_is_sufficient(self):
        engine = FilterEngine(
            {
                "include": {
                    "countries": ["China"],
                    "institutions": ["Stanford University"],
                },
                "unknown_policy": "exclude",
            },
            {
                "tsinghua university": "China",
                "stanford university": "United States",
            },
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="Paper",
            metadata={"affiliation_source_text": "Alice, Tsinghua University"},
        )
        result = engine.apply(article)
        self.assertTrue(result.included)
        self.assertIn("countries:China", result.reasons[0])

    def test_unknown_location_is_excluded(self):
        engine = FilterEngine(
            {
                "include": {"countries": ["China"]},
                "unknown_policy": "exclude",
            },
            {"tsinghua university": "China"},
        )
        result = engine.apply(Article(id="a", type="paper", source="arxiv", title="Paper"))
        self.assertFalse(result.included)
        self.assertEqual(result.reasons, ["location filter failed: unknown location"])

    def test_mit_does_not_match_submit(self):
        engine = FilterEngine(
            {
                "include": {"institutions": ["Massachusetts Institute of Technology"]},
                "unknown_policy": "exclude",
            },
            {"mit": "United States"},
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="Paper",
            metadata={"affiliation_source_text": "Submit without GitHub"},
        )
        self.assertFalse(engine.apply(article).included)
        self.assertEqual(article.metadata["institutions"], [])

    def test_us_alias_does_not_match_pronoun(self):
        engine = FilterEngine(
            {
                "include": {"countries": ["United States"]},
                "unknown_policy": "exclude",
            },
            {},
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="Paper",
            metadata={"affiliation_source_text": "Please contact us for correspondence"},
        )
        self.assertFalse(engine.apply(article).included)
        self.assertEqual(article.metadata["countries"], [])

    def test_punctuated_country_and_institution_aliases(self):
        engine = FilterEngine(
            {
                "include": {"countries": ["United States"]},
                "unknown_policy": "exclude",
            },
            {"mit": "United States"},
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="Paper",
            metadata={"affiliation_source_text": "Alice, M.I.T., U.S.A."},
        )
        self.assertTrue(engine.apply(article).included)
        self.assertEqual(article.metadata["countries"], ["United States"])

    def test_configured_institution_without_country_mapping_is_detected(self):
        engine = FilterEngine(
            {
                "include": {"institutions": ["Fudan University"]},
                "unknown_policy": "exclude",
            },
            {},
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="Paper",
            metadata={"affiliation_source_text": "Alice - Fudan University"},
        )
        self.assertTrue(engine.apply(article).included)

    def test_exclude_wins(self):
        engine = FilterEngine(
            {
                "include": {"keywords": ["robotics"]},
                "exclude": {"keywords": ["medical"]},
            },
            {},
        )
        article = Article(
            id="a",
            type="paper",
            source="arxiv",
            title="Medical Robotics",
        )
        self.assertFalse(engine.apply(article).included)


if __name__ == "__main__":
    unittest.main()
