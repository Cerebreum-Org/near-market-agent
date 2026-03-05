"""GitHub publisher — pushes workspace code to a GitHub repo.

For code deliverables (PACKAGE, SERVICE, SYSTEM tiers), creates a public
repo under a configurable GitHub org/user and pushes all workspace files.

Set GITHUB_ORG to your GitHub username or org name. If unset, GitHub
publishing is skipped and deliverables are submitted as text only.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Default .gitignore for deliverables
DEFAULT_GITIGNORE = """\
node_modules/
.venv/
venv/
__pycache__/
*.pyc
dist/
build/
*.egg-info/
.mypy_cache/
.tox/
coverage/
.env
.DS_Store
"""

# Dirs to exclude from git add
_SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".tox",
    "coverage",
    ".mypy_cache",
    ".git",
}


def _run_cmd(cmd: str, cwd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a shell command, capturing output."""
    return subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _clean_workspace(workspace: str) -> None:
    """Remove dirs that shouldn't be pushed."""
    for skip in _SKIP_DIRS:
        path = os.path.join(workspace, skip)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _ensure_gitignore(workspace: str) -> None:
    """Create .gitignore if one doesn't exist."""
    gi_path = os.path.join(workspace, ".gitignore")
    if not os.path.exists(gi_path):
        Path(gi_path).write_text(DEFAULT_GITIGNORE)
        log.info("Created default .gitignore")


def _sanitize_repo_name(job_id: str, job_title: str) -> str:
    """Generate a repo name from job ID and title."""
    # Use first 8 chars of job_id for uniqueness
    short_id = job_id[:8]
    # Sanitize title: lowercase, replace non-alnum with hyphens, truncate
    slug = "".join(c if c.isalnum() else "-" for c in job_title.lower())
    slug = "-".join(part for part in slug.split("-") if part)[:40]
    return f"near-job-{short_id}-{slug}" if slug else f"near-job-{short_id}"


def gh_available() -> bool:
    """Check if the gh CLI is available and authenticated."""
    return shutil.which("gh") is not None


def publish_workspace(
    workspace: str,
    job_title: str,
    job_id: str,
    org: str = "",
    author_name: str = "NEAR Market Agent",
    author_email: str = "agent@market.near.ai",
) -> str | None:
    """Push workspace to a new GitHub repo.

    Args:
        workspace: Path to the workspace directory.
        job_title: Job title (used in repo name).
        job_id: Job ID (used for uniqueness).
        org: GitHub org or username. If empty, publishing is skipped.
        author_name: Git commit author name.
        author_email: Git commit author email.

    Returns the repo URL on success, None on failure.
    """
    if not org:
        log.warning("GITHUB_ORG not set — cannot publish to GitHub")
        return None

    if not gh_available():
        log.warning("gh CLI not found — cannot publish to GitHub")
        return None

    repo_name = _sanitize_repo_name(job_id, job_title)
    full_name = f"{org}/{repo_name}"

    log.info(f"Publishing workspace to GitHub: {full_name}")

    try:
        _clean_workspace(workspace)
        _ensure_gitignore(workspace)

        # Remove meta files that shouldn't be in the deliverable
        for meta in [
            "JOB.md",
            "REQUIREMENTS.md",
            "NEAR-REFERENCE.md",
            "RESEARCH.md",
            "ALIGNMENT_REPORT.md",
        ]:
            meta_path = os.path.join(workspace, meta)
            if os.path.exists(meta_path):
                os.remove(meta_path)

        # git init + add + commit
        result = _run_cmd("git init", workspace)
        if result.returncode != 0:
            log.error(f"git init failed: {result.stderr}")
            return None

        _run_cmd("git add -A", workspace)

        result = _run_cmd(
            f'git commit -m "Deliverable for market.near.ai job"'
            f' --author="{author_name} <{author_email}>"',
            workspace,
        )
        if result.returncode != 0:
            log.error(f"git commit failed: {result.stderr}")
            return None

        # Create repo via gh CLI
        result = _run_cmd(
            f"gh repo create {full_name} --public --source=. --push",
            workspace,
            timeout=60,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # If repo already exists, try pushing to it
            if "already exists" in stderr.lower():
                log.warning(f"Repo {full_name} already exists, pushing anyway")
                _run_cmd(f"git remote add origin https://github.com/{full_name}.git", workspace)
                push_result = _run_cmd("git push -u origin main --force", workspace, timeout=60)
                if push_result.returncode != 0:
                    log.error(f"git push failed: {push_result.stderr}")
                    return None
            else:
                log.error(f"gh repo create failed: {stderr}")
                return None

        repo_url = f"https://github.com/{full_name}"
        log.info(f"Published to {repo_url}")
        return repo_url

    except subprocess.TimeoutExpired:
        log.error("GitHub publish timed out")
        return None
    except Exception as e:
        log.error(f"GitHub publish failed: {e}")
        return None
