from __future__ import annotations

# Fixed boolean mutations are part of the hostile evidence table.
# ruff: noqa: FBT003
import ast
import base64
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_cli_evidence import (
    fixture_values,
    sample_context,
    sample_provenance,
    valid_document,
    valid_invocations,
)

from tools import capture_cli_evidence as capture
from tools import cli_evidence_contract as contract
from tools import render_cli_evidence

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{}\n\n",
        b'{"a":1,"a":2}\n',
        b'{"value":1.0}\n',
        b'{"value":NaN}\n',
        b'{"value":01}\n',
        b'["not","an","object"]\n',
        b'{"value":"\xff"}\n',
        b'{"value":"raw\x00control"}\n',
        b'{"value":1}',
    ],
)
def test_document_decoder_rejects_noncanonical_or_ambiguous_bytes(payload: bytes) -> None:
    with pytest.raises(contract.EvidenceContractError):
        contract.decode_canonical_document(payload, context="hostile input")


def test_document_decoder_rejects_excessive_depth() -> None:
    value: contract.JsonValue = {"leaf": True}
    for _index in range(contract.MAX_JSON_DEPTH + 2):
        value = {"nested": value}
    raw = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    with pytest.raises(contract.EvidenceContractError, match="nesting"):
        contract.decode_canonical_document(raw, context="deep input")


def test_jsonl_decoder_requires_every_record_to_be_canonical_and_terminated() -> None:
    with pytest.raises(contract.EvidenceContractError):
        contract.decode_canonical_jsonl(b'{"ok":true}\n{"ok":true}', context="JSONL")
    with pytest.raises(contract.EvidenceContractError):
        contract.decode_canonical_jsonl(b'{"ok": true}\n', context="JSONL")


def replace_channel_object(
    invocations: tuple[contract.Invocation, ...],
    *,
    index: int,
    channel: str,
    mutate: Callable[[contract.JsonObject], None],
) -> tuple[contract.Invocation, ...]:
    result = list(invocations)
    original = result[index]
    raw = original.stdout if channel == "stdout" else original.stderr
    parsed = contract.decode_canonical_document(raw, context="test mutation")
    mutate(parsed)
    encoded = contract.canonical_json_bytes(parsed)
    result[index] = (
        replace(original, stdout=encoded)
        if channel == "stdout"
        else replace(original, stderr=encoded)
    )
    return tuple(result)


def set_nested(
    path: tuple[str, ...],
    value: contract.JsonValue,
) -> Callable[[contract.JsonObject], None]:
    def mutate(document: contract.JsonObject) -> None:
        target = document
        for key in path[:-1]:
            target = cast(contract.JsonObject, target[key])
        target[path[-1]] = value

    return mutate


@pytest.mark.parametrize(
    ("index", "channel", "mutation"),
    [
        (0, "stdout", set_nested(("replayed",), True)),
        (1, "stdout", set_nested(("replayed",), False)),
        (2, "stdout", set_nested(("event", "previous_event_hash"), "sha256:" + "0" * 64)),
        (3, "stderr", set_nested(("error", "retry"), "retry_exact_command")),
        (4, "stdout", set_nested(("event", "revision"), 1)),
        (5, "stdout", set_nested(("next_after_revision",), None)),
        (7, "stdout", set_nested(("event", "content"), fixture_values()["content-v2"])),
        (8, "stdout", set_nested(("found",), True)),
        (9, "stdout", set_nested(("operation",), "get")),
    ],
)
def test_raw_scenario_rejects_semantic_tampering(
    index: int,
    channel: str,
    mutation: Callable[[contract.JsonObject], None],
) -> None:
    hostile = replace_channel_object(
        valid_invocations(),
        index=index,
        channel=channel,
        mutate=mutation,
    )
    with pytest.raises(contract.EvidenceContractError):
        contract.validate_raw_scenario(hostile, fixture_values(), sample_context())


def test_raw_scenario_rejects_missing_duplicate_and_reordered_steps() -> None:
    original = valid_invocations()
    variants = (
        original[:-1],
        (*original[:-1], original[-2]),
        (original[1], original[0], *original[2:]),
    )
    for hostile in variants:
        with pytest.raises(contract.EvidenceContractError):
            contract.validate_raw_scenario(hostile, fixture_values(), sample_context())


def test_raw_scenario_rejects_noncanonical_channel_even_when_json_meaning_matches() -> None:
    original = valid_invocations()
    hostile = list(original)
    hostile[0] = replace(
        hostile[0],
        stdout=hostile[0].stdout.replace(b'{"event":', b'{"event": '),
    )
    with pytest.raises(contract.EvidenceContractError, match="canonical"):
        contract.validate_raw_scenario(
            tuple(hostile),
            fixture_values(),
            sample_context(),
        )


def test_raw_scenario_rejects_executable_data_and_fixture_path_substitution() -> None:
    original = valid_invocations()
    variants: list[tuple[contract.Invocation, ...]] = []
    for index, position, replacement in (
        (0, 0, "/bin/not-recall-ledger"),
        (1, 2, "/forged/data"),
        (2, -1, "/forged/cli-content-v2.json"),
    ):
        hostile_list = list(original)
        argv = list(hostile_list[index].argv)
        argv[position] = replacement
        hostile_list[index] = replace(hostile_list[index], argv=tuple(argv))
        variants.append(tuple(hostile_list))
    for variant in variants:
        with pytest.raises(contract.EvidenceContractError, match="raw argv"):
            contract.validate_raw_scenario(
                variant,
                fixture_values(),
                sample_context(),
            )


@pytest.mark.parametrize("timestamp", [0, 1_700_000_000_000_001])
def test_equal_storage_timestamps_receive_revision_specific_tokens(timestamp: int) -> None:
    timestamps = (timestamp, timestamp, timestamp)
    scenario = contract.validate_raw_scenario(
        valid_invocations(timestamps=timestamps),
        fixture_values(),
        sample_context(),
    )
    document = contract.build_evidence_document(
        scenario,
        provenance=sample_provenance(),
        fixtures=fixture_values(),
    )
    encoded = contract.canonical_json_bytes(document)
    for token in contract.TIME_TOKENS:
        assert encoded.count(token.encode()) >= 1
    assert f'"recorded_at_us":{timestamp}'.encode() not in encoded


def test_raw_hash_recomputation_matches_unicode_event_canonicalization() -> None:
    fixtures = copy.deepcopy(fixture_values())
    fixtures["content-v1"]["title"] = "Portfolio evidence café"
    fixtures["content-v2"]["body"] = "Verified paging keeps Unicode: naïve."
    scenario = contract.validate_raw_scenario(
        valid_invocations(fixtures=fixtures),
        fixtures,
        sample_context(),
    )
    assert tuple(cast(int, event["revision"]) for event in scenario.events) == (1, 2, 3)


def test_normalized_fixture_contract_rejects_duplicate_tags_even_with_new_digest() -> None:
    document = valid_document()
    records = cast(list[contract.JsonObject], document["fixtures"])
    first = records[0]
    content = cast(contract.JsonObject, first["content"])
    content["tags"] = ["duplicate", "duplicate"]
    first["sha256"] = contract.fixture_digest(content)
    with pytest.raises(contract.EvidenceContractError, match="duplicate tag"):
        contract.validate_evidence_document(document)


def test_normalized_fixture_contract_closes_surrogate_rejection() -> None:
    document = valid_document()
    records = cast(list[contract.JsonObject], document["fixtures"])
    first = records[0]
    content = cast(contract.JsonObject, first["content"])
    content["title"] = "\ud800"
    with pytest.raises(contract.EvidenceContractError, match="Unicode scalar"):
        contract.validate_evidence_document(document)


def test_evidence_validator_rejects_unknown_keys_host_paths_and_raw_note_ids() -> None:
    unknown = valid_document()
    unknown["unexpected"] = True
    with pytest.raises(contract.EvidenceContractError, match="unknown"):
        contract.validate_evidence_document(unknown)

    host_path = valid_document()
    steps = cast(list[contract.JsonObject], host_path["steps"])
    argv = cast(list[contract.JsonValue], steps[0]["argv"])
    argv[2] = "/home/private/data"
    with pytest.raises(contract.EvidenceContractError):
        contract.validate_evidence_document(host_path)

    raw_note = valid_document()
    steps = cast(list[contract.JsonObject], raw_note["steps"])
    argv = cast(list[contract.JsonValue], steps[4]["argv"])
    argv[-1] = "nt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    with pytest.raises(contract.EvidenceContractError):
        contract.validate_evidence_document(raw_note)


def tar_payload(*, name: str, kind: str = "file") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        root = tarfile.TarInfo("source/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        member = tarfile.TarInfo(name)
        member.mode = 0o644
        if kind == "file":
            payload = b"safe"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        elif kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "target"
            archive.addfile(member)
        elif kind == "hardlink":
            member.type = tarfile.LNKTYPE
            member.linkname = "source/target"
            archive.addfile(member)
        elif kind == "fifo":
            member.type = tarfile.FIFOTYPE
            archive.addfile(member)
        else:
            raise AssertionError(kind)
    return output.getvalue()


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("../escape", "file"),
        ("/absolute", "file"),
        ("source/link", "symlink"),
        ("source/link", "hardlink"),
        ("source/fifo", "fifo"),
    ],
)
def test_archive_extractor_rejects_traversal_links_and_special_files(
    tmp_path: Path,
    name: str,
    kind: str,
) -> None:
    with pytest.raises(capture.CaptureError):
        capture._extract_archive(tar_payload(name=name, kind=kind), tmp_path)
    assert not (tmp_path.parent / "escape").exists()


def test_archive_extractor_accepts_one_bounded_regular_source_tree(tmp_path: Path) -> None:
    source = capture._extract_archive(
        tar_payload(name="source/example.txt"),
        tmp_path,
    )
    assert source == tmp_path / "source"
    assert (source / "example.txt").read_bytes() == b"safe"


def test_capture_subprocess_uses_exact_environment_without_shell_inheritance(
    tmp_path: Path,
) -> None:
    environment = {"PATH": "/usr/bin:/bin", "VISIBLE": "synthetic"}
    result = capture._run(
        ("/usr/bin/env",),
        cwd=tmp_path,
        environment=environment,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout.splitlines() == [b"PATH=/usr/bin:/bin", b"VISIBLE=synthetic"]

    tree = ast.parse((ROOT / "tools" / "capture_cli_evidence.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert len(calls) == 1
    assert all(
        not (
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
        )
        for keyword in calls[0].keywords
    )


def test_capture_subprocess_stops_at_the_in_memory_output_bound(tmp_path: Path) -> None:
    with pytest.raises(capture.CaptureError, match="output byte limit"):
        capture._run(
            (sys.executable, "-c", "import sys;sys.stdout.write('x'*4096)"),
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin"},
            maximum_output_bytes=64,
        )


def test_capture_subprocess_kills_descendants_after_session_leader_exits(
    tmp_path: Path,
) -> None:
    survivor = tmp_path / "descendant-survived"
    child = (
        "import pathlib,time;"
        "time.sleep(2);"
        f"pathlib.Path({str(survivor)!r}).write_text('survived',encoding='utf-8')"
    )
    leader = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}])"
    with pytest.raises(capture.CaptureError, match="timed out"):
        capture._run(
            (sys.executable, "-c", leader),
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin"},
            timeout=1,
        )
    time.sleep(1.25)
    assert not survivor.exists()


def test_capture_subprocess_cleans_group_after_successful_leader_exit(
    tmp_path: Path,
) -> None:
    survivor = tmp_path / "detached-descendant-survived"
    child = (
        "import pathlib,time;"
        "time.sleep(1);"
        f"pathlib.Path({str(survivor)!r}).write_text('survived',encoding='utf-8')"
    )
    leader = (
        "import subprocess,sys;"
        "subprocess.Popen("
        f"[sys.executable,'-c',{child!r}],"
        "stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)"
    )
    result = capture._run(
        (sys.executable, "-c", leader),
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
    )
    assert result == capture.ProcessResult(returncode=0, stdout=b"", stderr=b"")
    time.sleep(1.25)
    assert not survivor.exists()


def test_capture_input_manifest_ignores_caches_but_binds_harness_bytes(
    tmp_path: Path,
) -> None:
    for relative in capture.CAPTURE_INPUT_EXACT:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic\n")
    runtime = tmp_path / "src" / "recall_ledger" / "runtime.py"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(b"runtime = True\n")
    initial = capture._capture_input_manifest(tmp_path)

    cache = runtime.parent / "__pycache__" / "runtime.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"host-specific cache")
    assert capture._capture_input_manifest(tmp_path) == initial

    harness = tmp_path / "tools" / "capture_cli_evidence.py"
    harness.write_bytes(b"changed harness\n")
    assert capture._capture_input_manifest(tmp_path)["sha256"] != initial["sha256"]


def test_wheel_record_digest_decoder_requires_canonical_base64url() -> None:
    digest = hashlib.sha256(b"member").digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert capture._decode_record_hash(f"sha256={encoded}") == digest
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    final_index = alphabet.index(encoded[-1])
    assert final_index % 4 == 0
    noncanonical = f"{encoded[:-1]}{alphabet[final_index + 1]}"
    assert base64.urlsafe_b64decode(f"{noncanonical}=") == digest
    for hostile in (f"sha256={encoded}=", f"sha256={encoded} ", "sha256=*", "md5=abc"):
        with pytest.raises(capture.CaptureError):
            capture._decode_record_hash(hostile)
    with pytest.raises(capture.CaptureError, match="pad bits"):
        capture._decode_record_hash(f"sha256={noncanonical}")


def test_distribution_metadata_probe_cannot_import_the_target_package() -> None:
    tree = ast.parse(capture._INSTALLATION_METADATA_PROBE)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert all(not name.startswith("recall_ledger") for name in imported_modules)


def test_capture_writer_is_atomic_and_rejects_symlink_or_hardlink_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "evidence" / "capture.json"
    write_modes: list[int] = []
    real_write = os.write

    def inspected_write(descriptor: int, payload: bytes) -> int:
        write_modes.append(os.fstat(descriptor).st_mode & 0o777)
        return real_write(descriptor, payload)

    monkeypatch.setattr(capture, "DEFAULT_EVIDENCE_PATH", output)
    monkeypatch.setattr(os, "write", inspected_write)
    capture._atomic_write(output, b'{"safe":true}\n')
    assert write_modes and set(write_modes) == {0o600}
    assert output.read_bytes() == b'{"safe":true}\n'
    assert output.stat().st_mode & 0o777 == 0o644

    output.unlink()
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    output.symlink_to(target)
    with pytest.raises(capture.CaptureError):
        capture._atomic_write(output, b"hostile")
    assert target.read_text(encoding="utf-8") == "unchanged"

    output.unlink()
    os.link(target, output)
    with pytest.raises(capture.CaptureError):
        capture._atomic_write(output, b"hostile")
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_capture_workspace_is_pinned_private_and_rejects_artifacts_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    monkeypatch.setattr(capture, "ARTIFACTS_DIRECTORY", artifacts)
    with capture._private_workspace() as workspace:
        resolved = workspace.resolve(strict=True)
        assert resolved.parent == artifacts
        assert resolved.stat().st_mode & 0o077 == 0
        (workspace / "owned.txt").write_text("synthetic", encoding="utf-8")
    assert list(artifacts.iterdir()) == []

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    artifacts.rmdir()
    artifacts.symlink_to(target, target_is_directory=True)
    with pytest.raises(capture.CaptureError), capture._private_workspace():
        pass
    assert list(target.iterdir()) == []


def test_terminal_source_reader_rejects_symlink_hardlink_and_fifo(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(contract.canonical_json_bytes(valid_document()))

    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(render_cli_evidence.TerminalRenderError):
        render_cli_evidence._read_source(symlink)

    hardlink = tmp_path / "hardlink.json"
    os.link(target, hardlink)
    with pytest.raises(render_cli_evidence.TerminalRenderError):
        render_cli_evidence._read_source(hardlink)

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(render_cli_evidence.TerminalRenderError):
        render_cli_evidence._read_source(fifo)


def test_terminal_writer_rejects_noncanonical_name_and_symlink_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(render_cli_evidence, "OUTPUT_DIRECTORY", tmp_path)
    with pytest.raises(render_cli_evidence.TerminalRenderError):
        render_cli_evidence._atomic_write("../escape.svg", b"<svg/>")

    filename = render_cli_evidence.OUTPUT_NAMES[0]
    output = tmp_path / filename
    write_modes: list[int] = []
    real_write = os.write

    def inspected_write(descriptor: int, payload: bytes) -> int:
        write_modes.append(os.fstat(descriptor).st_mode & 0o777)
        return real_write(descriptor, payload)

    monkeypatch.setattr(os, "write", inspected_write)
    render_cli_evidence._atomic_write(filename, b"<svg/>")
    assert write_modes and set(write_modes) == {0o600}
    assert output.read_bytes() == b"<svg/>"
    assert output.stat().st_mode & 0o777 == 0o644
    output.unlink()

    target = tmp_path / "target.svg"
    target.write_text("unchanged", encoding="utf-8")
    output.symlink_to(target)
    with pytest.raises(render_cli_evidence.TerminalRenderError):
        render_cli_evidence._atomic_write(filename, b"<svg/>")
    assert target.read_text(encoding="utf-8") == "unchanged"


def _set_document_path(
    document: contract.JsonObject,
    path: tuple[str, ...],
    value: contract.JsonValue,
) -> None:
    parent = document
    for component in path[:-1]:
        parent = cast(contract.JsonObject, parent[component])
    parent[path[-1]] = value


def _canonical_evidence(document: contract.JsonObject) -> bytes:
    return contract.canonical_json_bytes(document)


def test_portable_recapture_allowlist_is_exact_and_accepts_only_environment_provenance() -> None:
    expected_paths = (
        ("provenance", "builder", "python_version"),
        ("provenance", "installation", "pip_version"),
        ("provenance", "installation", "python_version"),
        ("provenance", "installation", "sqlite_version"),
        ("provenance", "wheel", "sha256"),
    )
    assert expected_paths == capture.PORTABLE_RECAPTURE_PROVENANCE_PATHS

    committed = valid_document()
    recaptured = copy.deepcopy(committed)
    _set_document_path(recaptured, expected_paths[0], "3.12.13")
    _set_document_path(recaptured, expected_paths[1], "26.1.2")
    _set_document_path(recaptured, expected_paths[2], "3.12.13")
    _set_document_path(recaptured, expected_paths[3], "3.46.1")
    _set_document_path(recaptured, expected_paths[4], "a" * 64)

    capture._check_portable_runtime_evidence(
        _canonical_evidence(committed),
        _canonical_evidence(recaptured),
    )


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("provenance", "source_commit"), "a" * 40),
        (("provenance", "source_archive", "sha256"), "a" * 64),
        (("provenance", "capture_inputs", "sha256"), "a" * 64),
        (("provenance", "wheel", "record_sha256"), "a" * 64),
        (("provenance", "wheel", "size_bytes"), 44_116),
        (("provenance", "installation", "installed_files_sha256"), "a" * 64),
        (("provenance", "builder", "build_version"), "9.9.9"),
    ],
)
def test_portable_runtime_comparison_rejects_nonallowlisted_provenance_drift(
    path: tuple[str, ...],
    replacement: contract.JsonValue,
) -> None:
    committed = valid_document()
    recaptured = copy.deepcopy(committed)
    _set_document_path(recaptured, path, replacement)

    with pytest.raises(capture.CaptureError, match="outside portable recapture provenance"):
        capture._check_portable_runtime_evidence(
            _canonical_evidence(committed),
            _canonical_evidence(recaptured),
        )


def test_portable_runtime_comparison_rejects_scenario_and_verification_drift() -> None:
    committed = valid_document()

    scenario_drift = copy.deepcopy(committed)
    steps = cast(list[contract.JsonValue], scenario_drift["steps"])
    first_step = cast(contract.JsonObject, steps[0])
    first_step["purpose"] = "Changed workflow claim."
    with pytest.raises(contract.EvidenceContractError):
        capture._check_portable_runtime_evidence(
            _canonical_evidence(committed),
            _canonical_evidence(scenario_drift),
        )

    verification_drift = copy.deepcopy(committed)
    verification = cast(contract.JsonObject, verification_drift["verification"])
    verification["installed_wheel_decode"] = False
    with pytest.raises(contract.EvidenceContractError):
        capture._check_portable_runtime_evidence(
            _canonical_evidence(committed),
            _canonical_evidence(verification_drift),
        )


@pytest.mark.parametrize(
    ("builder_python", "installed_python", "message"),
    [
        ("3.12.13", "3.12.12", "builder and installed Python versions differ"),
        ("3.13.0", "3.13.0", "recorded Python major.minor series"),
    ],
)
def test_portable_runtime_comparison_rejects_incoherent_or_cross_series_python(
    builder_python: str,
    installed_python: str,
    message: str,
) -> None:
    committed = valid_document()
    recaptured = copy.deepcopy(committed)
    _set_document_path(
        recaptured,
        ("provenance", "builder", "python_version"),
        builder_python,
    )
    _set_document_path(
        recaptured,
        ("provenance", "installation", "python_version"),
        installed_python,
    )

    with pytest.raises(capture.CaptureError, match=message):
        capture._check_portable_runtime_evidence(
            _canonical_evidence(committed),
            _canonical_evidence(recaptured),
        )


def test_check_remains_byte_exact_while_portable_mode_recaptures_recorded_source(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    committed_document = valid_document()
    committed = _canonical_evidence(committed_document)
    recaptured_document = copy.deepcopy(committed_document)
    _set_document_path(
        recaptured_document,
        ("provenance", "builder", "python_version"),
        "3.12.13",
    )
    _set_document_path(
        recaptured_document,
        ("provenance", "installation", "python_version"),
        "3.12.13",
    )
    _set_document_path(
        recaptured_document,
        ("provenance", "installation", "pip_version"),
        "26.1.2",
    )
    recaptured = _canonical_evidence(recaptured_document)
    source_commit = cast(
        str,
        cast(contract.JsonObject, committed_document["provenance"])["source_commit"],
    )
    capture_calls: list[tuple[Path, str]] = []

    def fake_capture(root: Path, commit: str) -> bytes:
        capture_calls.append((root, commit))
        return recaptured

    monkeypatch.setattr(capture, "_read_committed_evidence", lambda: committed)
    monkeypatch.setattr(capture, "_check_current_inputs", lambda _document: None)
    monkeypatch.setattr(capture, "capture_evidence", fake_capture)

    assert capture.main(["--check"]) == 1
    assert "not byte-for-byte current" in capsys.readouterr().err
    assert capture.main(["--check-portable-runtime"]) == 0
    assert capsys.readouterr().err == ""
    assert capture_calls == [(capture.ROOT, source_commit), (capture.ROOT, source_commit)]


def committed_harness_source(commit: str) -> bool:
    result = subprocess.run(  # noqa: S603 - fixed Git argv
        (
            "git",
            "cat-file",
            "-e",
            f"{commit}:tools/capture_cli_evidence.py",
        ),
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_clean_archive_wheel_capture_is_deterministic_and_fully_normalized() -> None:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    if not committed_harness_source(commit):
        pytest.skip("integration runs after the evidence harness is committed")

    first = capture.capture_evidence(ROOT, commit)
    second = capture.capture_evidence(ROOT, commit)
    assert first == second
    document = contract.decode_evidence_bytes(first)
    provenance = cast(contract.JsonObject, document["provenance"])
    assert provenance["source_commit"] == commit
    assert cast(contract.JsonObject, provenance["capture_inputs"])[
        "sha256"
    ] == capture.current_capture_input_digest(ROOT)
    assert b"/home/" not in first
    assert b"/tmp/" not in first
    assert re.search(rb"nt_[0-9a-f]{32}", first) is None
    assert b"REVISION_CONFLICT" in first
