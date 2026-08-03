from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LOCK = ROOT / "requirements-dev.lock"

ACTION_USES = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
}
LOCKED_PACKAGES = {
    "build",
    "coverage",
    "iniconfig",
    "mypy",
    "mypy-extensions",
    "packaging",
    "pathspec",
    "pip",
    "pluggy",
    "pygments",
    "pyproject-hooks",
    "pytest",
    "pytest-cov",
    "ruff",
    "setuptools",
    "typing-extensions",
}
REQUIREMENT_PATTERN = re.compile(
    r"(?P<name>[a-z0-9][a-z0-9-]*)==(?P<version>[0-9][a-zA-Z0-9.]*)"
    r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})+)\Z"
)


def _canonical_text(path: Path) -> str:
    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    assert b"\r" not in payload and b"\t" not in payload and b"\x00" not in payload
    assert path.stat().st_mode & 0o777 == 0o644
    return payload.decode("ascii")


def _locked_requirements() -> dict[str, tuple[str, tuple[str, ...]]]:
    lock_text = _canonical_text(LOCK)
    assert lock_text.splitlines().count("--only-binary=:all:") == 1
    logical_lines: list[str] = []
    pending: list[str] = []
    for raw_line in lock_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "--only-binary=:all:":
            continue
        pending.append(line.removesuffix("\\").strip())
        if line.endswith("\\"):
            continue
        logical_lines.append(" ".join(pending))
        pending.clear()
    assert not pending

    requirements: dict[str, tuple[str, tuple[str, ...]]] = {}
    for line in logical_lines:
        match = REQUIREMENT_PATTERN.fullmatch(line)
        assert match is not None, line
        name = match.group("name")
        hashes = tuple(re.findall(r"sha256:([0-9a-f]{64})", match.group("hashes")))
        assert hashes and len(hashes) == len(set(hashes))
        assert name not in requirements
        requirements[name] = (match.group("version"), hashes)
    return requirements


def test_workflow_uses_a_read_only_bounded_matrix() -> None:
    workflow = _canonical_text(WORKFLOW)

    assert workflow.startswith("name: CI\n\non:\n")
    assert "\n  pull_request:\n" in workflow
    assert "\n  push:\n    branches: [main]\n" in workflow
    assert "\n  workflow_dispatch:\n" in workflow
    assert "pull_request_target" not in workflow
    assert "\npermissions:\n  contents: read\n" in workflow
    assert "secrets." not in workflow and "GITHUB_TOKEN" not in workflow
    assert "cancel-in-progress: true" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "timeout-minutes: 15" in workflow
    assert "fail-fast: false" in workflow and "max-parallel: 2" in workflow
    assert 'python: ["3.11", "3.12"]' in workflow

    uses = {
        line.strip().removeprefix("uses: ").split(" #", maxsplit=1)[0]
        for line in workflow.splitlines()
        if line.strip().startswith("uses: ")
    }
    assert uses == ACTION_USES
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow


def test_workflow_runs_every_fail_closed_review_boundary() -> None:
    workflow = _canonical_text(WORKFLOW)

    required_commands = {
        "python -m pip install --require-hashes -r requirements-dev.lock",
        "python -m pip install --no-deps --no-build-isolation --editable .",
        "python -m pip check",
        "python -m ruff check .",
        "python -m ruff format --check .",
        "python -m mypy src tests tools",
        "python tools/lexical_eval_contract.py",
        "python tools/render_visuals.py --check",
        "python tools/render_cli_evidence.py --check",
        "python tools/capture_cli_evidence.py --check",
        "python -m pytest",
        'test -z "$(git status --porcelain=v1 --untracked-files=all)"',
    }
    for command in required_commands:
        assert command in workflow
    assert "if: matrix.python == '3.11'" in workflow
    assert "if: matrix.python == '3.12'" in workflow
    assert "cache-dependency-path: requirements-dev.lock" in workflow


def test_linux_matrix_toolchain_is_complete_and_hash_locked() -> None:
    requirements = _locked_requirements()

    assert requirements.keys() == LOCKED_PACKAGES
    assert requirements["pip"][0] == "26.1.2"
    assert len(requirements["coverage"][1]) == 2
    assert len(requirements["mypy"][1]) == 2
    assert all(
        len(hashes) == 1
        for name, (_version, hashes) in requirements.items()
        if name not in {"coverage", "mypy"}
    )


def test_direct_dev_pins_cannot_drift_from_the_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    direct = project["project"]["optional-dependencies"]["dev"]
    direct_pins = dict(requirement.split("==", maxsplit=1) for requirement in direct)
    locked = _locked_requirements()

    assert all("==" in requirement for requirement in direct)
    assert {name: locked[name][0] for name in direct_pins} == direct_pins
