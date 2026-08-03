from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import cast

import pytest

from recall_ledger.retrieval import LEXICAL_UNICODE_PROFILE
from tools import lexical_eval_contract as contract

ROOT = Path(__file__).resolve().parents[1]
SUITE_DIRECTORY = ROOT / "evals" / "lexical-v1"
SOURCE_FILES = {
    "evals/lexical-v1/DATA_CARD.md",
    "evals/lexical-v1/corpus.v1.json",
    "evals/lexical-v1/expected.v1.json",
    "evals/lexical-v1/queries.v1.json",
}
SDIST_REVIEW_FILES = SOURCE_FILES | {
    ".github/workflows/ci.yml",
    "requirements-dev.lock",
    "tests/test_ci_contract.py",
    "tests/test_lexical_eval.py",
    "tools/lexical_eval_contract.py",
}


def _copy_suite(tmp_path: Path) -> Path:
    destination = tmp_path / "lexical-v1"
    shutil.copytree(SUITE_DIRECTORY, destination)
    return destination


def _object(path: Path) -> contract.JsonObject:
    value = cast(object, json.loads(path.read_bytes()))
    assert type(value) is dict
    return cast(contract.JsonObject, value)


def _write(path: Path, value: contract.JsonObject) -> None:
    path.write_bytes(contract.canonical_json_bytes(value))


def _object_array(value: contract.JsonValue) -> list[contract.JsonObject]:
    assert type(value) is list
    rows = cast(list[object], value)
    assert all(type(row) is dict for row in rows)
    return cast(list[contract.JsonObject], rows)


def test_committed_suite_recomputes_exact_outcomes_and_disclosed_metrics() -> None:
    summary = contract.validate_suite()

    assert summary.document_count == 8
    assert summary.query_count == 12
    assert summary.cutoff == 5
    assert summary.unicode_profile == LEXICAL_UNICODE_PROFILE
    assert summary.known_semantic_miss_count == 1
    assert summary.operator_inertness_case_count == 1
    assert summary.corpus_sha256 == (
        "a41bbb16bf618f0dbfaf9f62f7c85696db6d0e7ff6113491c4bcfe71cb7658e7"
    )
    assert summary.queries_sha256 == (
        "d0fbf57ea8396eaa97a9093a9ca24a473564f3f3c3d8aa87dce70f6002aba7dd"
    )
    assert summary.expected_sha256 == (
        "ae5a3c4815ef9988e72de76e51408790dd9ac980bacc3a8ce00db58102a288c9"
    )
    assert summary.metrics == {
        "exact_outcome_rate": {"denominator": 12, "numerator": 12, "ppm": 1_000_000},
        "macro_ndcg_at_5": {"denominator": 12, "numerator": 11, "ppm": 916_667},
        "macro_recall_at_5": {"denominator": 12, "numerator": 11, "ppm": 916_667},
        "micro_recall_at_5": {"denominator": 17, "numerator": 16, "ppm": 941_176},
        "mrr_at_5": {"denominator": 12, "numerator": 11, "ppm": 916_667},
        "success_at_1": {"denominator": 12, "numerator": 11, "ppm": 916_667},
        "unexpected_hit_count": {"count": 0},
    }


def test_validator_cli_emits_one_canonical_runtime_bound_record() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository tool
        [sys.executable, "tools/lexical_eval_contract.py"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == b""
    decoded = contract.decode_canonical_document(result.stdout, context="validator stdout")
    assert decoded == contract.validate_suite().to_object()
    assert decoded["unicode_profile"] == LEXICAL_UNICODE_PROFILE


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}\n',
        b'{"a":1.0}\n',
        b'{"a":NaN}\n',
        b'{ "a":1}\n',
        b'{"a":1}',
        b'{"a":1}\n\n',
        b'\xef\xbb\xbf{"a":1}\n',
        b'{"a":"\\u202e"}\n',
    ],
)
def test_decoder_rejects_ambiguous_noncanonical_or_unsafe_json(payload: bytes) -> None:
    with pytest.raises(contract.LexicalEvalContractError):
        contract.decode_canonical_document(payload, context="hostile source")


def test_query_schema_rejects_incomplete_judgments_before_digest_comparison(
    tmp_path: Path,
) -> None:
    suite = _copy_suite(tmp_path)
    path = suite / contract.QUERIES_FILE
    value = _object(path)
    first = _object_array(value["queries"])[0]
    judgments = _object_array(first["judgments"])
    first["judgments"] = cast(contract.JsonValue, judgments[:-1])
    _write(path, value)

    with pytest.raises(contract.LexicalEvalContractError, match="judge every document"):
        contract.validate_suite(suite)


def test_query_schema_rejects_boolean_grade(tmp_path: Path) -> None:
    suite = _copy_suite(tmp_path)
    path = suite / contract.QUERIES_FILE
    value = _object(path)
    first = _object_array(value["queries"])[0]
    _object_array(first["judgments"])[0]["grade"] = True
    _write(path, value)

    with pytest.raises(contract.LexicalEvalContractError, match="exact bounded integer"):
        contract.validate_suite(suite)


def test_query_schema_rejects_unknown_field(tmp_path: Path) -> None:
    suite = _copy_suite(tmp_path)
    path = suite / contract.QUERIES_FILE
    value = _object(path)
    _object_array(value["queries"])[0]["invented"] = "field"
    _write(path, value)

    with pytest.raises(contract.LexicalEvalContractError, match="unknown or missing"):
        contract.validate_suite(suite)


def test_expected_source_digest_cannot_be_rebound_silently(tmp_path: Path) -> None:
    suite = _copy_suite(tmp_path)
    path = suite / contract.EXPECTED_FILE
    value = _object(path)
    inputs = cast(contract.JsonObject, value["inputs"])
    corpus = cast(contract.JsonObject, inputs["corpus"])
    corpus["sha256"] = "0" * 64
    _write(path, value)

    with pytest.raises(contract.LexicalEvalContractError, match="digest does not bind"):
        contract.validate_suite(suite)


def test_expected_bytes_are_pinned_independently_of_internal_metrics(tmp_path: Path) -> None:
    suite = _copy_suite(tmp_path)
    path = suite / contract.EXPECTED_FILE
    value = _object(path)
    first_outcome = _object_array(value["outcomes"])[0]
    first_hit = _object_array(first_outcome["expected_hits"])[0]
    score = cast(contract.JsonObject, first_hit["score"])
    score["total"] = cast(int, score["total"]) + 1
    _write(path, value)

    with pytest.raises(contract.LexicalEvalContractError, match="frozen file digest changed"):
        contract.validate_suite(suite)


@pytest.mark.parametrize("attack", ["score", "removed-hit"])
def test_coherent_expected_tamper_still_fails_the_production_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    suite = _copy_suite(tmp_path)
    path = suite / contract.EXPECTED_FILE
    value = _object(path)
    outcomes = _object_array(value["outcomes"])
    operator_outcome = outcomes[-1]
    if attack == "score":
        operator_hit = _object_array(operator_outcome["expected_hits"])[0]
        score = cast(contract.JsonObject, operator_hit["score"])
        score["total"] = 10
    else:
        operator_outcome["expected_hits"] = []
        operator_outcome["expected_total_matches"] = 0
        metrics = cast(contract.JsonObject, value["expected_metrics"])
        cast(contract.JsonObject, metrics["unexpected_hit_count"])["count"] = 1
    metrics = cast(contract.JsonObject, value["expected_metrics"])
    exact = cast(contract.JsonObject, metrics["exact_outcome_rate"])
    exact.update({"denominator": 12, "numerator": 11, "ppm": 916_667})
    _write(path, value)
    monkeypatch.setattr(
        contract,
        "FROZEN_EXPECTED_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    with pytest.raises(contract.LexicalEvalContractError, match="frozen production outcome"):
        contract.validate_suite(suite)


def test_frozen_metric_denominator_and_ppm_are_cross_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _copy_suite(tmp_path)
    path = suite / contract.EXPECTED_FILE
    value = _object(path)
    metrics = cast(contract.JsonObject, value["expected_metrics"])
    success = cast(contract.JsonObject, metrics["success_at_1"])
    success["numerator"] = 10
    success["ppm"] = 833_333
    _write(path, value)
    monkeypatch.setattr(
        contract,
        "FROZEN_EXPECTED_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    with pytest.raises(contract.LexicalEvalContractError, match="recomputed lexical metrics"):
        contract.validate_suite(suite)


def test_suite_reader_rejects_a_symlinked_source(tmp_path: Path) -> None:
    suite = _copy_suite(tmp_path)
    corpus = suite / contract.CORPUS_FILE
    corpus.unlink()
    corpus.symlink_to(SUITE_DIRECTORY / contract.CORPUS_FILE)

    with pytest.raises(contract.LexicalEvalContractError, match="unavailable"):
        contract.validate_suite(suite)


def test_data_card_has_ordered_scope_integrity_privacy_and_limitations() -> None:
    data_card = SUITE_DIRECTORY / "DATA_CARD.md"
    text = data_card.read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("#")]

    assert headings == [
        "# RecallLedger lexical evaluation v1",
        "## Intended use",
        "## Dataset grain and scope",
        "## Construction and provenance",
        "## Relevance rubric",
        "## Covered cases",
        "## Metric definitions",
        "## Integrity and reproducibility",
        "## Privacy and licensing",
        "## Limitations and non-claims",
        "## Versioning policy",
    ]
    assert "eight synthetic current-head documents and twelve hand-authored queries" in text
    assert "not a production benchmark" in text
    assert "known synonym miss" in text
    assert "literal `OR`, `NEAR`, and wildcard-shaped input" in text
    assert "three independent quality claims" in text

    all_source_text = b"".join(
        path.read_bytes()
        for path in (
            data_card,
            SUITE_DIRECTORY / contract.CORPUS_FILE,
            SUITE_DIRECTORY / contract.QUERIES_FILE,
            SUITE_DIRECTORY / contract.EXPECTED_FILE,
        )
    ).decode("utf-8")
    assert "/home/" not in all_source_text and "/Users/" not in all_source_text
    assert "-----BEGIN " not in all_source_text
    assert re.search(r"\bAKIA[0-9A-Z]{16}\b", all_source_text) is None
    assert re.search(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b", all_source_text) is None
    assert re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", all_source_text) is None


def test_frozen_eval_sources_are_lf_pinned_for_cross_platform_checkouts() -> None:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603 - resolved Git and fixed read-only arguments
        [
            git,
            "check-attr",
            "eol",
            "--",
            "evals/lexical-v1/corpus.v1.json",
            "evals/lexical-v1/DATA_CARD.md",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "evals/lexical-v1/corpus.v1.json: eol: lf",
        "evals/lexical-v1/DATA_CARD.md: eol: lf",
    ]


def test_sdist_includes_eval_sources_while_runtime_wheel_excludes_them(
    tmp_path: Path,
) -> None:
    distribution_directory = tmp_path / "dist"
    build = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned paths
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--no-isolation",
            "--skip-dependency-check",
            "--outdir",
            str(distribution_directory),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    archives = tuple(distribution_directory.glob("recall_ledger-*.tar.gz"))
    wheels = tuple(distribution_directory.glob("recall_ledger-*.whl"))
    assert len(archives) == len(wheels) == 1

    with tarfile.open(archives[0], mode="r:gz") as archive:
        members = archive.getmembers()
        member_paths = tuple(Path(member.name) for member in members)
        assert all(not path.is_absolute() and ".." not in path.parts for path in member_paths)
        roots = {path.parts[0] for path in member_paths if path.parts}
        assert len(roots) == 1
        root_name = next(iter(roots))
        packaged_sources = {
            Path(*Path(member.name).parts[1:]).as_posix()
            for member in members
            if member.isfile() and Path(member.name).parts[0] == root_name
        }
        assert packaged_sources >= SDIST_REVIEW_FILES
        for member in members:
            relative = Path(*Path(member.name).parts[1:]).as_posix()
            if relative in SDIST_REVIEW_FILES:
                assert member.mode & 0o777 == 0o644
        extraction_directory = tmp_path / "extracted"
        extraction_directory.mkdir()
        archive.extractall(extraction_directory, filter="data")

    extracted_root = extraction_directory / root_name
    subprocess_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COV_CORE_") and key != "COVERAGE_PROCESS_START"
    }
    extracted_check = subprocess.run(  # noqa: S603 - extracted repository-owned validator
        [sys.executable, "tools/lexical_eval_contract.py"],
        cwd=extracted_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        env=subprocess_environment,
    )
    assert extracted_check.returncode == 0, extracted_check.stderr.decode()
    extracted_summary = contract.decode_canonical_document(
        extracted_check.stdout,
        context="sdist validator stdout",
    )
    assert extracted_summary == contract.validate_suite().to_object()

    extracted_ci_check = subprocess.run(  # noqa: S603 - extracted review-only tests
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_ci_contract.py",
            "--no-cov",
        ],
        cwd=extracted_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        env=subprocess_environment,
        text=True,
    )
    assert extracted_ci_check.returncode == 0, extracted_ci_check.stderr

    with zipfile.ZipFile(wheels[0], mode="r") as wheel:
        names = set(wheel.namelist())
    assert all(not name.startswith("evals/") for name in names)
    assert all("/evals/" not in name for name in names)
