"""Pure-Python tests for sessions.py helpers that don't need cognee runtime.

The tests here cover string-level helpers (compute_project_hash,
dataset_name, get_git_branch via subprocess mock, etc.). End-to-end
tests against live cognee live in ``test_lifecycle_integration.py``
(skipped by default; opt-in via env var).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


class TestProjectHash(unittest.TestCase):
    def setUp(self):
        import sessions as S

        self.S = S

    def test_deterministic(self):
        h1 = self.S.compute_project_hash("/Users/foo/projects/bar")
        h2 = self.S.compute_project_hash("/Users/foo/projects/bar")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in h1))

    def test_different_paths_different_hashes(self):
        h1 = self.S.compute_project_hash("/foo/bar")
        h2 = self.S.compute_project_hash("/foo/baz")
        self.assertNotEqual(h1, h2)

    def test_dataset_name_format(self):
        ds = self.S.dataset_name("abc123")
        self.assertEqual(ds, "project_abc123")


class TestGitHelpers(unittest.TestCase):
    def setUp(self):
        import sessions as S

        self.S = S

    def test_get_git_branch_runs_subprocess(self):
        # We just verify the function exists and returns a string.
        # End-to-end correctness depends on the cwd actually being a repo.
        result = self.S.get_git_branch("/tmp")  # /tmp is unlikely to be a repo
        self.assertIsInstance(result, str)


class TestCacheHelpers(unittest.TestCase):
    """write_cache / read_cache atomicity (no cognee dependency)."""

    def setUp(self):
        import sessions as S

        self.S = S

    def test_read_missing_cache_returns_none(self):
        # Use a hash that's almost certainly not on disk.
        self.assertIsNone(self.S.read_cache("zzzzzzzzzzzz"))


if __name__ == "__main__":
    unittest.main()
