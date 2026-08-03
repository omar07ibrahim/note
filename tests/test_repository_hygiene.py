from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git_check_ignore(path: str) -> int:
    return subprocess.run(  # noqa: S603 - fixed local Git inspection
        ("git", "check-ignore", "--no-index", "--quiet", "--", path),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode


def test_private_runtime_artifacts_are_ignored_without_hiding_public_evidence() -> None:
    private_paths = (
        "scratch/recall-ledger.sqlite3",
        "scratch/recall-ledger.sqlite3-journal",
        "scratch/recall-ledger.sqlite3-shm",
        "scratch/recall-ledger.sqlite3-wal",
        "scratch/.recall-ledger.lock",
        ".coverage.parallel-worker",
    )

    assert all(_git_check_ignore(path) == 0 for path in private_paths)
    assert _git_check_ignore("docs/visuals/evidence/installed-wheel-cli.v1.json") == 1
