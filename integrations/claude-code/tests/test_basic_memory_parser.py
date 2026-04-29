"""Pure-Python tests for the Basic Memory markdown parser.

Cognee runtime is not required for any of these — all parsers and the
INVERSE_VERBS map are stdlib + pydantic only.

Run with:

    cd integrations/claude-code
    python3 -m pytest tests/

Or, without pytest installed:

    python3 -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


class TestFrontmatter(unittest.TestCase):
    def setUp(self):
        import basic_memory as BM

        self.BM = BM

    def test_split_frontmatter_basic(self):
        md = "---\ntitle: Foo\npermalink: foo\n---\n\nbody"
        fm, rest = self.BM.split_frontmatter(md)
        self.assertEqual(fm, {"title": "Foo", "permalink": "foo"})
        # Parser strips through the trailing newline after the closing ---,
        # so only the body section follows.
        self.assertEqual(rest.strip(), "body")

    def test_split_frontmatter_list(self):
        md = '---\ntags: [security, auth]\n---\n'
        fm, _ = self.BM.split_frontmatter(md)
        self.assertEqual(fm["tags"], ["security", "auth"])

    def test_split_frontmatter_quoted(self):
        md = '---\ntitle: "Hello: World"\n---\n'
        fm, _ = self.BM.split_frontmatter(md)
        self.assertEqual(fm["title"], "Hello: World")

    def test_split_frontmatter_missing(self):
        md = "no frontmatter here"
        fm, rest = self.BM.split_frontmatter(md)
        self.assertEqual(fm, {})
        self.assertEqual(rest, md)


class TestPermalinkSlug(unittest.TestCase):
    def setUp(self):
        import basic_memory as BM

        self.BM = BM

    def test_title_with_colon(self):
        self.assertEqual(
            self.BM.title_to_permalink("ADR-014: Auth Strategy"),
            "adr-014-auth-strategy",
        )

    def test_title_with_spaces(self):
        self.assertEqual(self.BM.title_to_permalink("User DB Schema"), "user-db-schema")

    def test_already_kebab(self):
        self.assertEqual(self.BM.title_to_permalink("api-security"), "api-security")

    def test_empty(self):
        self.assertEqual(self.BM.title_to_permalink(""), "")

    def test_permalink_to_title_roundtrip(self):
        self.assertEqual(self.BM.permalink_to_title("api-security"), "Api Security")


class TestObservationParser(unittest.TestCase):
    def setUp(self):
        import basic_memory as BM

        self.BM = BM

    def test_basic_observation(self):
        md = "## Observations\n- [decision] Use JWT tokens\n"
        results = list(self.BM.parse_observations(md))
        self.assertEqual(len(results), 1)
        cat, content, tags, ctx = results[0]
        self.assertEqual(cat, "decision")
        self.assertEqual(content, "Use JWT tokens")
        self.assertEqual(tags, [])
        self.assertIsNone(ctx)

    def test_observation_with_tags(self):
        md = "## Observations\n- [decision] Use JWT #jwt #auth\n"
        results = list(self.BM.parse_observations(md))
        cat, content, tags, ctx = results[0]
        self.assertEqual(content, "Use JWT")
        self.assertEqual(tags, ["jwt", "auth"])

    def test_observation_with_context(self):
        md = "## Observations\n- [decision] Use JWT (per RFC 7519)\n"
        results = list(self.BM.parse_observations(md))
        cat, content, tags, ctx = results[0]
        self.assertEqual(content, "Use JWT")
        self.assertEqual(ctx, "per RFC 7519")

    def test_observation_with_tags_and_context(self):
        md = "## Observations\n- [risk] Tokens expire #rotation (mitigated by refresh)\n"
        results = list(self.BM.parse_observations(md))
        cat, content, tags, ctx = results[0]
        self.assertEqual(cat, "risk")
        self.assertEqual(content, "Tokens expire")
        self.assertEqual(tags, ["rotation"])
        self.assertEqual(ctx, "mitigated by refresh")

    def test_no_observations_section(self):
        md = "# Just a heading\n\nSome prose."
        self.assertEqual(list(self.BM.parse_observations(md)), [])


class TestRelationParser(unittest.TestCase):
    def setUp(self):
        import basic_memory as BM

        self.BM = BM

    def test_typed_relations(self):
        md = (
            "## Relations\n"
            "- depends_on [[User DB Schema]]\n"
            "- implements [[ADR-014: Auth Strategy]]\n"
            "- blocks [[Frontend Onboarding]]\n"
        )
        results = list(self.BM.parse_relations(md))
        self.assertEqual(
            results,
            [
                ("depends_on", "user-db-schema"),
                ("implements", "adr-014-auth-strategy"),
                ("blocks", "frontend-onboarding"),
            ],
        )

    def test_no_relations_section(self):
        md = "# Heading\n\n[[Inline]] mention"
        self.assertEqual(list(self.BM.parse_relations(md)), [])


class TestInlineWikilinks(unittest.TestCase):
    def setUp(self):
        import basic_memory as BM

        self.BM = BM

    def test_inline_excludes_relations_targets(self):
        md = (
            "Body has [[Bedrock]] and [[JWT]] mentions.\n\n"
            "## Relations\n"
            "- depends_on [[User DB Schema]]\n"
        )
        results = self.BM.parse_inline_wikilinks(md)
        # User DB Schema lives in Relations — must be excluded.
        self.assertIn("bedrock", results)
        self.assertIn("jwt", results)
        self.assertNotIn("user-db-schema", results)

    def test_dedup(self):
        md = "[[Foo]] then [[Foo]] again."
        results = self.BM.parse_inline_wikilinks(md)
        self.assertEqual(results, ["foo"])


class TestInverseVerbs(unittest.TestCase):
    def setUp(self):
        import basic_memory as BM

        self.BM = BM

    def test_canonical_pairs(self):
        cases = [
            ("depends_on", "required_by"),
            ("required_by", "depends_on"),
            ("implements", "implemented_by"),
            ("blocks", "blocked_by"),
            ("blocked_by", "blocks"),
            ("part_of", "contains"),
            ("contains", "part_of"),
        ]
        for v, expected in cases:
            self.assertEqual(
                self.BM.inverse_verb(v),
                expected,
                f"inverse_verb({v!r}) → {self.BM.inverse_verb(v)!r}, expected {expected!r}",
            )

    def test_symmetric(self):
        self.assertEqual(self.BM.inverse_verb("pairs_with"), "pairs_with")
        self.assertEqual(self.BM.inverse_verb("relates_to"), "relates_to")

    def test_unknown_falls_back_to_relates_to(self):
        self.assertEqual(self.BM.inverse_verb("invented_yesterday"), "relates_to")


class TestSectionExtractors(unittest.TestCase):
    def setUp(self):
        import basic_memory as BM

        self.BM = BM

    def test_extract_section(self):
        md = (
            "# Title\n\n"
            "Body.\n\n"
            "## Observations\n"
            "- [fact] one\n"
            "- [fact] two\n\n"
            "## Relations\n"
            "- depends_on [[X]]\n"
        )
        obs = self.BM.extract_section(md, "Observations")
        self.assertIn("- [fact] one", obs)
        self.assertNotIn("depends_on", obs)
        rel = self.BM.extract_section(md, "Relations")
        self.assertIn("depends_on", rel)
        self.assertNotIn("[fact]", rel)

    def test_strip_section(self):
        md = "## A\nfoo\n\n## B\nbar\n"
        out = self.BM.strip_section(md, "A")
        self.assertNotIn("foo", out)
        self.assertIn("bar", out)


if __name__ == "__main__":
    unittest.main()
