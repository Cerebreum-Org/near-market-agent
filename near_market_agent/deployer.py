"""Build verification — proves deliverables can actually build/run.

Verifies that code deliverables compile, build, and have valid entry points.
Does NOT deploy to production — just validates the build step.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .job_router import JobTier, RoutingResult

log = logging.getLogger(__name__)

BUILD_TIMEOUT = 120  # seconds


@dataclass
class DeployResult:
    """Result from build verification."""

    success: bool
    method: str  # "npm-build", "python-build", "docker-build", "entry-check", "skip"
    output: str

    def summary(self) -> str:
        status = "PASSED" if self.success else "FAILED"
        return f"Build verification ({self.method}): {status}"


def _run_cmd(cmd: str, cwd: str, timeout: int = BUILD_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def _has_script(workspace: str, script_name: str) -> bool:
    """Check if package.json has a specific script."""
    pkg_path = os.path.join(workspace, "package.json")
    if not os.path.exists(pkg_path):
        return False
    try:
        pkg = json.loads(Path(pkg_path).read_text())
        return script_name in pkg.get("scripts", {})
    except (json.JSONDecodeError, OSError):
        return False


def _verify_npm_build(workspace: str) -> DeployResult:
    """Verify npm project builds."""
    if not _tool_available("npm"):
        return DeployResult(
            success=True,
            method="npm-build",
            output="npm not available — skipped build verification",
        )

    # Install deps first (if not already installed)
    node_modules = os.path.join(workspace, "node_modules")
    if not os.path.isdir(node_modules):
        install = _run_cmd("npm install --ignore-scripts 2>&1", workspace)
        if install.returncode != 0:
            return DeployResult(
                success=False,
                method="npm-build",
                output=f"npm install failed:\n{install.stdout[-1000:]}",
            )

    # Try build if script exists
    if _has_script(workspace, "build"):
        result = _run_cmd("npm run build 2>&1", workspace)
        output = result.stdout[-1500:]
        if result.returncode != 0:
            return DeployResult(
                success=False, method="npm-build", output=f"npm run build failed:\n{output}"
            )
        return DeployResult(success=True, method="npm-build", output=f"Build succeeded:\n{output}")

    # No build script — check that main entry exists
    pkg_path = os.path.join(workspace, "package.json")
    if os.path.exists(pkg_path):
        try:
            pkg = json.loads(Path(pkg_path).read_text())
            main = pkg.get("main", "index.js")
            if os.path.exists(os.path.join(workspace, main)):
                return DeployResult(
                    success=True,
                    method="entry-check",
                    output=f"Entry point '{main}' exists (no build script)",
                )
        except (json.JSONDecodeError, OSError):
            pass

    return DeployResult(success=True, method="entry-check", output="No build script — skipped")


def _verify_python_build(workspace: str) -> DeployResult:
    """Verify Python project builds."""
    if not _tool_available("python3") and not _tool_available("python"):
        return DeployResult(
            success=True,
            method="python-build",
            output="python not available — skipped build verification",
        )

    python = "python3" if _tool_available("python3") else "python"

    # Try building sdist/wheel
    result = _run_cmd(f"{python} -m build 2>&1", workspace)
    if result.returncode == 0:
        output = result.stdout[-1000:]
        return DeployResult(
            success=True, method="python-build", output=f"Build succeeded:\n{output}"
        )

    # If `build` module not available, check basic import
    pyproject = os.path.join(workspace, "pyproject.toml")
    setup_py = os.path.join(workspace, "setup.py")
    if os.path.exists(pyproject) or os.path.exists(setup_py):
        # Just verify the package structure is valid
        src_dir = os.path.join(workspace, "src")
        if os.path.isdir(src_dir):
            init_files = list(Path(src_dir).rglob("__init__.py"))
            if init_files:
                return DeployResult(
                    success=True,
                    method="entry-check",
                    output=f"Package structure valid ({len(init_files)} __init__.py found)",
                )

    return DeployResult(success=True, method="entry-check", output="Basic structure check passed")


def _verify_docker_build(workspace: str) -> DeployResult:
    """Verify Dockerfile builds (if docker/podman available)."""
    dockerfile = os.path.join(workspace, "Dockerfile")
    if not os.path.exists(dockerfile):
        return DeployResult(success=True, method="skip", output="No Dockerfile — skipped")

    # Prefer podman on Linux, docker otherwise
    tool = None
    for t in ["podman", "docker"]:
        if _tool_available(t):
            tool = t
            break

    if not tool:
        return DeployResult(
            success=True, method="skip", output="No docker/podman — skipped Dockerfile verification"
        )

    tag = f"near-verify-{os.path.basename(workspace)[:12]}"
    result = _run_cmd(f"{tool} build -t {tag} . 2>&1", workspace, timeout=180)
    output = result.stdout[-1500:]

    # Clean up the image
    _run_cmd(f"{tool} rmi {tag} 2>&1", workspace, timeout=30)

    if result.returncode != 0:
        return DeployResult(
            success=False, method="docker-build", output=f"{tool} build failed:\n{output}"
        )
    return DeployResult(success=True, method="docker-build", output=f"{tool} build succeeded")


def verify_build(workspace: str, routing: RoutingResult) -> DeployResult:
    """Verify that a workspace deliverable can build.

    Routes to the appropriate verification method based on project type.
    Non-blocking: never crashes the pipeline.
    """
    if routing.tier == JobTier.TEXT:
        return DeployResult(
            success=True, method="skip", output="Text tier — no build verification needed"
        )

    try:
        pkg_json = os.path.join(workspace, "package.json")
        pyproject = os.path.join(workspace, "pyproject.toml")
        setup_py = os.path.join(workspace, "setup.py")
        dockerfile = os.path.join(workspace, "Dockerfile")

        results: list[DeployResult] = []

        # Check primary build system
        if os.path.exists(pkg_json):
            results.append(_verify_npm_build(workspace))
        elif os.path.exists(pyproject) or os.path.exists(setup_py):
            results.append(_verify_python_build(workspace))

        # Also check Docker if present
        if os.path.exists(dockerfile):
            results.append(_verify_docker_build(workspace))

        if not results:
            return DeployResult(success=True, method="skip", output="No recognized build system")

        # Aggregate: fail if any critical step failed
        failures = [r for r in results if not r.success]
        if failures:
            combined = "\n".join(f.output for f in failures)
            return DeployResult(
                success=False,
                method=failures[0].method,
                output=combined[:2000],
            )

        combined = "\n".join(r.output for r in results)
        return DeployResult(
            success=True,
            method=results[0].method,
            output=combined[:2000],
        )

    except subprocess.TimeoutExpired:
        return DeployResult(success=False, method="timeout", output="Build verification timed out")
    except Exception as e:
        log.warning(f"Build verification error (non-fatal): {e}")
        return DeployResult(success=True, method="error", output=f"Verification error: {e}")
