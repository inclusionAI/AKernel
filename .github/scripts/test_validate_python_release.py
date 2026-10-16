#!/usr/bin/env python3
"""Exercise release policy against isolated Git repositories; no network required."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VALIDATOR = Path(__file__).with_name("validate-python-release.py").resolve()


class PythonReleasePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.origin = root / "origin.git"
        self.repo = root / "checkout"
        self.repo.mkdir()
        self.git("init", "--bare", str(self.origin))
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Release policy test")
        self.git("config", "user.email", "release-policy@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("remote", "add", "origin", str(self.origin))
        self.commit_version("0.2.0")
        self.main_sha = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-b", "develop")
        self.commit_version("0.2.0rc1")
        self.develop_sha = self.git("rev-parse", "HEAD").strip()
        self.git("push", "origin", "main", "develop")
        self.git("checkout", "--detach", self.develop_sha)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout

    def commit_version(self, version: str) -> None:
        path = self.repo / "sdk/python/pyproject.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
        self.git("add", "sdk/python/pyproject.toml")
        self.git("commit", "-m", f"Prepare {version}")

    def validate(self, tag: str, prerelease: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), "--tag", tag, "--prerelease", prerelease],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_rejected(self, tag: str, prerelease: str, message: str) -> None:
        result = self.validate(tag, prerelease)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)

    def test_rc_from_develop_annotated_tag_succeeds(self) -> None:
        self.git("tag", "-a", "v0.2.0rc1", "-m", "Candidate")
        self.git("checkout", "--detach", "v0.2.0rc1")
        result = self.validate("v0.2.0rc1", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("origin/develop", result.stdout)

    def test_stable_from_main_succeeds(self) -> None:
        self.git("checkout", "--detach", self.main_sha)
        result = self.validate("v0.2.0", "false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("origin/main", result.stdout)

    def test_stable_from_develop_only_is_rejected(self) -> None:
        self.commit_version("0.2.0")
        self.git("push", "origin", "HEAD:refs/heads/develop")
        self.assert_rejected("v0.2.0", "false", "commit on main")

    def test_rc_from_main_only_is_rejected(self) -> None:
        self.git("checkout", "main")
        self.commit_version("0.2.0rc2")
        self.git("push", "origin", "main")
        self.assert_rejected("v0.2.0rc2", "true", "commit on develop")

    def test_version_mismatch_is_rejected(self) -> None:
        self.assert_rejected("v0.2.0rc2", "true", "does not match package version")

    def test_release_type_mismatch_is_rejected(self) -> None:
        self.assert_rejected(
            "v0.2.0rc1", "false", "RC releases must set prerelease=true"
        )
        self.git("checkout", "--detach", self.main_sha)
        self.assert_rejected("v0.2.0", "true", "stable releases must set false")

    def test_invalid_tag_syntax_is_rejected(self) -> None:
        for tag in ("0.2.0rc1", "v0.2", "v0.2.0-rc.1", "v0.2.0rc1\n"):
            with self.subTest(tag=tag):
                self.assert_rejected(tag, "true", "tag must use vX.Y.Z or vX.Y.ZrcN")

    def test_remote_branch_is_refreshed_before_ancestry_check(self) -> None:
        self.git(
            "--git-dir",
            str(self.origin),
            "update-ref",
            "refs/heads/develop",
            self.main_sha,
        )
        self.git("update-ref", "refs/remotes/origin/develop", self.develop_sha)
        self.assert_rejected("v0.2.0rc1", "true", "commit on develop")

    def test_missing_remote_branch_is_rejected(self) -> None:
        self.git(
            "--git-dir", str(self.origin), "update-ref", "-d", "refs/heads/develop"
        )
        self.assertNotEqual(self.validate("v0.2.0rc1", "true").returncode, 0)


if __name__ == "__main__":
    unittest.main()
