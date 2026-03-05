"""Tests for GitHub publisher module."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from near_market_agent.github_publisher import (
    _clean_workspace,
    _ensure_gitignore,
    _sanitize_repo_name,
    gh_available,
    publish_workspace,
)


class TestSanitizeRepoName:
    def test_basic(self):
        name = _sanitize_repo_name("abc12345-full-id", "Build MCP Server")
        assert name == "near-job-abc12345-build-mcp-server"

    def test_special_chars(self):
        name = _sanitize_repo_name("xyz99999", "Hello World! @#$% Test")
        assert name.startswith("near-job-xyz99999-")
        assert "@" not in name
        assert "#" not in name

    def test_long_title_truncated(self):
        name = _sanitize_repo_name("abcd1234", "A" * 100)
        # slug portion should be <= 40 chars
        slug = name.replace("near-job-abcd1234-", "")
        assert len(slug) <= 40

    def test_empty_title(self):
        name = _sanitize_repo_name("abcd1234", "")
        assert name == "near-job-abcd1234"

    def test_id_truncated_to_8(self):
        name = _sanitize_repo_name("abcdefghijklmnop", "Test")
        assert "abcdefgh" in name
        assert "ijklmnop" not in name


class TestEnsureGitignore:
    def test_creates_gitignore(self):
        with tempfile.TemporaryDirectory() as d:
            _ensure_gitignore(d)
            gi = Path(d, ".gitignore")
            assert gi.exists()
            content = gi.read_text()
            assert "node_modules/" in content
            assert "__pycache__/" in content

    def test_preserves_existing(self):
        with tempfile.TemporaryDirectory() as d:
            gi = Path(d, ".gitignore")
            gi.write_text("custom\n")
            _ensure_gitignore(d)
            assert gi.read_text() == "custom\n"


class TestCleanWorkspace:
    def test_removes_skip_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            for skip in ["node_modules", "__pycache__", ".venv"]:
                os.makedirs(os.path.join(d, skip))
                Path(d, skip, "junk.txt").write_text("junk")
            _clean_workspace(d)
            assert not os.path.exists(os.path.join(d, "node_modules"))
            assert not os.path.exists(os.path.join(d, "__pycache__"))
            assert not os.path.exists(os.path.join(d, ".venv"))

    def test_keeps_source_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "index.ts").write_text("export default 42;")
            _clean_workspace(d)
            assert os.path.exists(os.path.join(d, "index.ts"))


class TestGhAvailable:
    @patch("near_market_agent.github_publisher.shutil.which")
    def test_available(self, mock_which):
        mock_which.return_value = "/usr/local/bin/gh"
        assert gh_available() is True

    @patch("near_market_agent.github_publisher.shutil.which")
    def test_not_available(self, mock_which):
        mock_which.return_value = None
        assert gh_available() is False


class TestPublishWorkspace:
    @patch("near_market_agent.github_publisher.gh_available", return_value=False)
    def test_no_gh_returns_none(self, _):
        result = publish_workspace("/tmp/fake", "Test", "abc123", org="test-org")
        assert result is None

    def test_no_org_returns_none(self):
        result = publish_workspace("/tmp/fake", "Test", "abc123", org="")
        assert result is None

    @patch("near_market_agent.github_publisher._run_cmd")
    @patch("near_market_agent.github_publisher.gh_available", return_value=True)
    def test_success(self, _, mock_cmd):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "index.ts").write_text("code")
            Path(d, "JOB.md").write_text("meta")

            mock_cmd.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = publish_workspace(d, "Test Job", "abc12345", org="my-org")
            assert result == "https://github.com/my-org/near-job-abc12345-test-job"

            # JOB.md should be removed
            assert not os.path.exists(os.path.join(d, "JOB.md"))

    @patch("near_market_agent.github_publisher._run_cmd")
    @patch("near_market_agent.github_publisher.gh_available", return_value=True)
    def test_custom_author(self, _, mock_cmd):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "file.txt").write_text("content")
            mock_cmd.return_value = MagicMock(returncode=0, stdout="", stderr="")

            publish_workspace(
                d,
                "Test",
                "abc12345",
                org="test-org",
                author_name="My Agent",
                author_email="me@example.com",
            )
            # Check the commit command included custom author
            commit_calls = [c for c in mock_cmd.call_args_list if "commit" in str(c)]
            assert any("My Agent" in str(c) for c in commit_calls)

    @patch("near_market_agent.github_publisher._run_cmd")
    @patch("near_market_agent.github_publisher.gh_available", return_value=True)
    def test_git_init_failure(self, _, mock_cmd):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "file.txt").write_text("content")
            mock_cmd.return_value = MagicMock(returncode=1, stderr="fatal: error")
            result = publish_workspace(d, "Test", "abc12345", org="test-org")
            assert result is None

    @patch("near_market_agent.github_publisher._run_cmd")
    @patch("near_market_agent.github_publisher.gh_available", return_value=True)
    def test_timeout_returns_none(self, _, mock_cmd):
        mock_cmd.side_effect = subprocess.TimeoutExpired("cmd", 30)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "file.txt").write_text("content")
            result = publish_workspace(d, "Test", "abc12345", org="test-org")
            assert result is None
