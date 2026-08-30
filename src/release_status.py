"""Read-only release/version status helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReleaseStatus:
    """Current git release identity."""

    commit: str
    branch: str
    describe: str
    dirty: bool


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), *args],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def collect_release_status(repo_root: str | Path) -> ReleaseStatus:
    """Return current commit, branch, nearest tag description, and dirty state."""

    root = Path(repo_root)
    commit = _git(root, "rev-parse", "--short=12", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    try:
        describe = _git(root, "describe", "--tags", "--always", "--dirty")
    except subprocess.CalledProcessError:
        describe = commit
    dirty = bool(_git(root, "status", "--porcelain"))
    return ReleaseStatus(commit=commit, branch=branch, describe=describe, dirty=dirty)


def format_release_status(status: ReleaseStatus) -> str:
    """Format release status for operator logs."""

    dirty = "dirty" if status.dirty else "clean"
    return (
        f"branch={status.branch}\n"
        f"commit={status.commit}\n"
        f"version={status.describe}\n"
        f"worktree={dirty}"
    )
