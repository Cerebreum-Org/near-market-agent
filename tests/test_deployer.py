"""Tests for build verification (deployer) module."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

import pytest

from near_market_agent.deployer import (
    verify_build,
    _verify_npm_build,
    _verify_python_build,
    _verify_docker_build,
    _tool_available,
    _has_script,
    DeployResult,
)
from near_market_agent.job_router import JobTier, RoutingResult


def _make_routing(tier: JobTier) -> RoutingResult:
    return RoutingResult(tier=tier, agent="test-agent", reason="test", template=None, language="typescript")


class TestToolAvailable:
    def test_finds_existing_tool(self):
        # python or python3 should exist in test env
        assert _tool_available("python3") or _tool_available("python")

    def test_missing_tool(self):
        assert _tool_available("definitely-not-a-real-tool-xyz") is False


class TestHasScript:
    def test_has_build_script(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = {"scripts": {"build": "tsc", "test": "vitest"}}
            Path(d, "package.json").write_text(json.dumps(pkg))
            assert _has_script(d, "build") is True
            assert _has_script(d, "test") is True
            assert _has_script(d, "deploy") is False

    def test_no_package_json(self):
        with tempfile.TemporaryDirectory() as d:
            assert _has_script(d, "build") is False

    def test_malformed_package_json(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "package.json").write_text("not json")
            assert _has_script(d, "build") is False


class TestVerifyBuildTextTier:
    def test_text_tier_skips(self):
        routing = _make_routing(JobTier.TEXT)
        result = verify_build("/tmp/fake", routing)
        assert result.success is True
        assert result.method == "skip"


class TestVerifyNpmBuild:
    @patch("near_market_agent.deployer._tool_available", return_value=False)
    def test_no_npm(self, _):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "package.json").write_text("{}")
            result = _verify_npm_build(d)
            assert result.success is True
            assert "not available" in result.output

    @patch("near_market_agent.deployer._run_cmd")
    @patch("near_market_agent.deployer._tool_available", return_value=True)
    def test_build_success(self, _, mock_cmd):
        with tempfile.TemporaryDirectory() as d:
            pkg = {"scripts": {"build": "tsc"}}
            Path(d, "package.json").write_text(json.dumps(pkg))

            mock_cmd.return_value = MagicMock(returncode=0, stdout="Build complete")
            result = _verify_npm_build(d)
            assert result.success is True
            assert result.method == "npm-build"

    @patch("near_market_agent.deployer._run_cmd")
    @patch("near_market_agent.deployer._tool_available", return_value=True)
    def test_build_failure(self, _, mock_cmd):
        with tempfile.TemporaryDirectory() as d:
            pkg = {"scripts": {"build": "tsc"}}
            Path(d, "package.json").write_text(json.dumps(pkg))

            mock_cmd.return_value = MagicMock(returncode=1, stdout="Error: TS2307")
            result = _verify_npm_build(d)
            assert result.success is False
            assert "failed" in result.output.lower()

    @patch("near_market_agent.deployer._tool_available", return_value=True)
    def test_entry_point_check(self, _):
        with tempfile.TemporaryDirectory() as d:
            pkg = {"main": "index.js"}
            Path(d, "package.json").write_text(json.dumps(pkg))
            Path(d, "index.js").write_text("module.exports = {}")
            os.makedirs(os.path.join(d, "node_modules"))  # skip npm install

            result = _verify_npm_build(d)
            assert result.success is True


class TestVerifyPythonBuild:
    @patch("near_market_agent.deployer._tool_available", return_value=False)
    def test_no_python(self, _):
        with tempfile.TemporaryDirectory() as d:
            result = _verify_python_build(d)
            assert result.success is True
            assert "not available" in result.output

    @patch("near_market_agent.deployer._run_cmd")
    @patch("near_market_agent.deployer._tool_available", return_value=True)
    def test_build_success(self, _, mock_cmd):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pyproject.toml").write_text("[build-system]")
            mock_cmd.return_value = MagicMock(returncode=0, stdout="Built wheel")
            result = _verify_python_build(d)
            assert result.success is True
            assert result.method == "python-build"

    @patch("near_market_agent.deployer._run_cmd")
    @patch("near_market_agent.deployer._tool_available", return_value=True)
    def test_fallback_to_structure_check(self, _, mock_cmd):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pyproject.toml").write_text("[build-system]")
            os.makedirs(os.path.join(d, "src", "mypackage"))
            Path(d, "src", "mypackage", "__init__.py").write_text("")
            mock_cmd.return_value = MagicMock(returncode=1, stdout="No module named build")
            result = _verify_python_build(d)
            assert result.success is True
            assert "structure valid" in result.output


class TestVerifyDockerBuild:
    def test_no_dockerfile_skips(self):
        with tempfile.TemporaryDirectory() as d:
            result = _verify_docker_build(d)
            assert result.success is True
            assert result.method == "skip"

    @patch("near_market_agent.deployer._tool_available", return_value=False)
    def test_no_docker_skips(self, _):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "Dockerfile").write_text("FROM node:20")
            result = _verify_docker_build(d)
            assert result.success is True
            assert "No docker/podman" in result.output


class TestVerifyBuildIntegration:
    def test_package_tier_with_npm(self):
        routing = _make_routing(JobTier.PACKAGE)
        with tempfile.TemporaryDirectory() as d:
            pkg = {"main": "index.js"}
            Path(d, "package.json").write_text(json.dumps(pkg))
            Path(d, "index.js").write_text("module.exports = {}")
            os.makedirs(os.path.join(d, "node_modules"))

            result = verify_build(d, routing)
            assert result.success is True

    def test_no_build_system(self):
        routing = _make_routing(JobTier.SERVICE)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "README.md").write_text("# Hello")
            result = verify_build(d, routing)
            assert result.success is True
            assert result.method == "skip"

    @patch("near_market_agent.deployer._verify_npm_build")
    def test_exception_handled(self, mock_npm):
        mock_npm.side_effect = Exception("Unexpected!")
        routing = _make_routing(JobTier.PACKAGE)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "package.json").write_text("{}")
            result = verify_build(d, routing)
            assert result.success is True  # non-fatal
            assert result.method == "error"
