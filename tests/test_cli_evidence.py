from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

import pytest

from recall_ledger.events import (
    CommandId,
    LedgerEvent,
    NoteContent,
    NoteId,
    TenantId,
    TombstoneReason,
)
from tools import cli_evidence_contract as contract
from tools import render_cli_evidence

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIRECTORY = ROOT / "docs" / "visuals" / "fixtures"
NOTE_ID = "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TIMESTAMPS = (1_700_000_000_000_001, 1_700_000_000_000_002, 1_700_000_000_000_003)
SHA256_TEXT = "a" * 64


def fixture_values() -> dict[str, contract.JsonObject]:
    return {
        fixture_id: contract.load_fixture(ROOT / relative)
        for fixture_id, relative in contract.FIXTURE_FILES.items()
    }


def event_object(event: LedgerEvent) -> contract.JsonObject:
    value = cast(object, json.loads(event.to_bytes()))
    assert type(value) is dict
    return cast(contract.JsonObject, value)


def note_content(value: contract.JsonObject) -> NoteContent:
    tags = value["tags"]
    assert type(tags) is list
    return NoteContent(
        title=cast(str, value["title"]),
        body=cast(str, value["body"]),
        tags=tuple(cast(list[str], tags)),
    )


def sample_events(
    fixtures: dict[str, contract.JsonObject] | None = None,
    timestamps: tuple[int, int, int] = TIMESTAMPS,
) -> tuple[contract.JsonObject, contract.JsonObject, contract.JsonObject]:
    values = fixture_values() if fixtures is None else fixtures
    first = LedgerEvent.create(
        tenant_id=TenantId(contract.TENANT_ID),
        note_id=NoteId(NOTE_ID),
        command_id=CommandId(contract.CREATE_COMMAND_ID),
        recorded_at_us=timestamps[0],
        content=note_content(values["content-v1"]),
    )
    second = first.revise(
        command_id=CommandId(contract.REVISE_COMMAND_ID),
        recorded_at_us=timestamps[1],
        content=note_content(values["content-v2"]),
    )
    third = second.tombstone(
        command_id=CommandId(contract.TOMBSTONE_COMMAND_ID),
        recorded_at_us=timestamps[2],
        reason=TombstoneReason.USER_REQUEST,
    )
    return event_object(first), event_object(second), event_object(third)


def invocation(  # noqa: PLR0913 - test scenario remains explicit
    identifier: str,
    *,
    command: tuple[str, ...],
    fixture: str | None,
    exit_code: int,
    stdout: tuple[contract.JsonObject, ...] = (),
    stderr: tuple[contract.JsonObject, ...] = (),
    jsonl: bool = False,
) -> contract.Invocation:
    argv = (
        "/evidence-run/runtime/bin/recall-ledger",
        "--data-dir",
        "/evidence-run/data",
        "--tenant-id",
        contract.TENANT_ID,
        *command,
    )
    stdout_bytes = (
        b"".join(contract.canonical_json_bytes(item) for item in stdout)
        if jsonl
        else (contract.canonical_json_bytes(stdout[0]) if stdout else b"")
    )
    stderr_bytes = contract.canonical_json_bytes(stderr[0]) if stderr else b""
    return contract.Invocation(
        identifier=identifier,
        argv=argv,
        input_fixture=fixture,
        exit_code=exit_code,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
    )


def sample_context() -> contract.CaptureContext:
    return contract.CaptureContext(
        executable="/evidence-run/runtime/bin/recall-ledger",
        data_directory="/evidence-run/data",
        fixture_paths=tuple(
            (
                fixture_id,
                f"/evidence-run/{Path(relative).name}",
            )
            for fixture_id, relative in contract.FIXTURE_FILES.items()
        ),
    )


def valid_invocations(
    fixtures: dict[str, contract.JsonObject] | None = None,
    timestamps: tuple[int, int, int] = TIMESTAMPS,
) -> tuple[contract.Invocation, ...]:
    first, second, third = sample_events(fixtures, timestamps)
    create_result: contract.JsonObject = {
        "event": first,
        "ok": True,
        "operation": "create",
        "replayed": False,
    }
    replay_result = copy.deepcopy(create_result)
    replay_result["replayed"] = True
    revise_result: contract.JsonObject = {
        "event": second,
        "ok": True,
        "operation": "revise",
        "replayed": False,
    }
    stale_error: contract.JsonObject = {
        "error": {
            "code": "REVISION_CONFLICT",
            "message": "the note head no longer matches the expected revision",
            "retry": "inspect_head_then_new_command",
        },
        "ok": False,
    }
    head_result: contract.JsonObject = {
        "event": second,
        "found": True,
        "ok": True,
        "operation": "head",
    }
    history_one: contract.JsonObject = {
        "after_revision": 0,
        "events": [first],
        "found": True,
        "limit": 1,
        "next_after_revision": 1,
        "note_id": NOTE_ID,
        "ok": True,
        "operation": "history",
        "tenant_id": contract.TENANT_ID,
    }
    history_event: contract.JsonObject = {"event": second, "record": "event"}
    history_page: contract.JsonObject = {
        "after_revision": 1,
        "event_count": 1,
        "found": True,
        "limit": 1,
        "next_after_revision": None,
        "note_id": NOTE_ID,
        "record": "page",
        "tenant_id": contract.TENANT_ID,
    }
    tombstone_result: contract.JsonObject = {
        "event": third,
        "ok": True,
        "operation": "tombstone",
        "replayed": False,
    }
    hidden_result: contract.JsonObject = {
        "event": None,
        "found": False,
        "ok": True,
        "operation": "get",
    }
    final_head: contract.JsonObject = {
        "event": third,
        "found": True,
        "ok": True,
        "operation": "head",
    }
    fixture_paths = {
        fixture_id: f"/evidence-run/{Path(relative).name}"
        for fixture_id, relative in contract.FIXTURE_FILES.items()
    }
    return (
        invocation(
            "create",
            command=(
                "create",
                "--command-id",
                contract.CREATE_COMMAND_ID,
                "--content-file",
                fixture_paths["content-v1"],
            ),
            fixture="content-v1",
            exit_code=0,
            stdout=(create_result,),
        ),
        invocation(
            "exact-replay",
            command=(
                "create",
                "--command-id",
                contract.CREATE_COMMAND_ID,
                "--content-file",
                fixture_paths["content-v1"],
            ),
            fixture="content-v1",
            exit_code=0,
            stdout=(replay_result,),
        ),
        invocation(
            "revise",
            command=(
                "revise",
                "--note-id",
                NOTE_ID,
                "--command-id",
                contract.REVISE_COMMAND_ID,
                "--expected-revision",
                "1",
                "--content-file",
                fixture_paths["content-v2"],
            ),
            fixture="content-v2",
            exit_code=0,
            stdout=(revise_result,),
        ),
        invocation(
            "stale-revise",
            command=(
                "revise",
                "--note-id",
                NOTE_ID,
                "--command-id",
                contract.STALE_COMMAND_ID,
                "--expected-revision",
                "1",
                "--content-file",
                fixture_paths["content-stale"],
            ),
            fixture="content-stale",
            exit_code=11,
            stderr=(stale_error,),
        ),
        invocation(
            "head-after-conflict",
            command=("head", "--note-id", NOTE_ID),
            fixture=None,
            exit_code=0,
            stdout=(head_result,),
        ),
        invocation(
            "history-page-1",
            command=(
                "history",
                "--note-id",
                NOTE_ID,
                "--after-revision",
                "0",
                "--limit",
                "1",
            ),
            fixture=None,
            exit_code=0,
            stdout=(history_one,),
        ),
        invocation(
            "history-page-2-jsonl",
            command=(
                "history",
                "--note-id",
                NOTE_ID,
                "--after-revision",
                "1",
                "--limit",
                "1",
                "--jsonl",
            ),
            fixture=None,
            exit_code=0,
            stdout=(history_event, history_page),
            jsonl=True,
        ),
        invocation(
            "tombstone",
            command=(
                "tombstone",
                "--note-id",
                NOTE_ID,
                "--command-id",
                contract.TOMBSTONE_COMMAND_ID,
                "--expected-revision",
                "2",
                "--reason",
                "user_request",
            ),
            fixture=None,
            exit_code=0,
            stdout=(tombstone_result,),
        ),
        invocation(
            "get-after-tombstone",
            command=("get", "--note-id", NOTE_ID),
            fixture=None,
            exit_code=0,
            stdout=(hidden_result,),
        ),
        invocation(
            "final-head",
            command=("head", "--note-id", NOTE_ID),
            fixture=None,
            exit_code=0,
            stdout=(final_head,),
        ),
    )


def sample_provenance() -> contract.JsonObject:
    files: list[contract.JsonValue] = [
        {
            "path": f"src/recall_ledger/file_{index}.py",
            "sha256": f"{index:x}".rjust(64, "0"),
            "size_bytes": index,
        }
        for index in range(1, 9)
    ]
    return {
        "builder": {
            "build_version": "1.3.0",
            "python_version": "3.12.3",
            "setuptools_version": "83.0.0",
        },
        "capture_inputs": {"files": files, "sha256": "1" * 64},
        "installation": {
            "console_script": "recall-ledger = recall_ledger.cli:main",
            "console_script_normalized_sha256": "8" * 64,
            "distribution": "recall-ledger",
            "installed_files_sha256": "2" * 64,
            "method": "pip --no-index --no-deps --no-compile",
            "module_origin": "installed-venv/recall_ledger/__init__.py",
            "pip_version": "24.0",
            "python_version": "3.12.3",
            "sqlite_version": "3.45.1",
            "version": "0.1.0",
        },
        "source_archive": {
            "format": "git-archive-tar",
            "sha256": "3" * 64,
            "size_bytes": 4096,
        },
        "source_commit": "4" * 40,
        "source_date_epoch": 1_700_000_000,
        "source_tree": "5" * 40,
        "wheel": {
            "filename": "recall_ledger-0.1.0-py3-none-any.whl",
            "record_sha256": "6" * 64,
            "sha256": "7" * 64,
            "size_bytes": 8192,
        },
    }


def valid_document() -> contract.JsonObject:
    scenario = contract.validate_raw_scenario(
        valid_invocations(),
        fixture_values(),
        sample_context(),
    )
    return contract.build_evidence_document(
        scenario,
        provenance=sample_provenance(),
        fixtures=fixture_values(),
    )


def test_synthetic_fixtures_are_compact_canonical_and_non_personal() -> None:
    assert {path.name for path in FIXTURE_DIRECTORY.glob("*.json")} == {
        "cli-content-stale.json",
        "cli-content-v1.json",
        "cli-content-v2.json",
    }
    for fixture_id, relative in contract.FIXTURE_FILES.items():
        path = ROOT / relative
        content = contract.load_fixture(path)
        assert path.read_bytes() == contract.canonical_json_bytes(content)
        assert fixture_id in {"content-stale", "content-v1", "content-v2"}
        assert b"@" not in path.read_bytes()


def test_raw_ten_step_scenario_proves_replay_conflict_paging_and_tombstone() -> None:
    scenario = contract.validate_raw_scenario(
        valid_invocations(),
        fixture_values(),
        sample_context(),
    )
    assert scenario.note_id == NOTE_ID
    assert tuple(cast(int, event["revision"]) for event in scenario.events) == (1, 2, 3)
    assert scenario.recorded_at_us == TIMESTAMPS
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in scenario.event_hashes)
    assert scenario.events[1]["previous_event_hash"] == scenario.event_hashes[0]
    assert scenario.events[2]["previous_event_hash"] == scenario.event_hashes[1]


def test_normalization_removes_only_run_specific_event_values_and_paths() -> None:
    document = valid_document()
    encoded = contract.canonical_json_bytes(document)
    first, second, third = sample_events()
    assert NOTE_ID.encode() not in encoded
    assert b"/evidence-run" not in encoded
    for event in (first, second, third):
        assert cast(str, event["event_hash"]).encode() not in encoded
        assert str(cast(int, event["recorded_at_us"])).encode() not in encoded
    assert contract.TENANT_ID.encode() in encoded
    assert contract.STALE_COMMAND_ID.encode() in encoded
    assert b"REVISION_CONFLICT" in encoded
    assert b"inspect_head_then_new_command" in encoded
    assert cast(contract.JsonObject, document["provenance"]) == sample_provenance()


def test_evidence_round_trip_is_closed_canonical_and_byte_stable() -> None:
    document = valid_document()
    encoded = contract.canonical_json_bytes(document)
    assert contract.decode_evidence_bytes(encoded) == document
    assert contract.canonical_json_bytes(contract.decode_evidence_bytes(encoded)) == encoded


def test_terminal_projection_contains_every_command_channel_and_exit() -> None:
    document = valid_document()
    first_panel = render_cli_evidence.transcript_lines(document, range(5))
    second_panel = render_cli_evidence.transcript_lines(document, range(5, 10))
    text = "\n".join(line for line, _kind in (*first_panel, *second_panel))
    assert text.count("$ recall-ledger") == 10
    assert text.count("exit   | 0") == 9
    assert text.count("exit   | 11") == 1
    assert "stderr |" in text
    assert '"code":"REVISION_CONFLICT"' in text
    assert '"kind":"note.tombstoned"' in text
    assert contract.NOTE_TOKEN in text


def test_terminal_svg_is_deterministic_accessible_and_self_contained() -> None:
    document = valid_document()
    first = render_cli_evidence.render_evidence(document)
    second = render_cli_evidence.render_evidence(copy.deepcopy(document))
    assert first == second
    assert set(first) == set(render_cli_evidence.OUTPUT_NAMES)
    for filename, payload in first.items():
        assert len(payload) < render_cli_evidence.MAX_SVG_BYTES
        root = ElementTree.fromstring(payload)  # noqa: S314 - renderer output is bounded
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert root.attrib["role"] == "img"
        assert "aria-labelledby" in root.attrib
        assert contract.NORMALIZATION_DISCLOSURE.encode() in payload
        assert b"<script" not in payload
        assert b"<image" not in payload
        assert b"foreignObject" not in payload
        assert b"http://" not in payload.replace(b"http://www.w3.org/2000/svg", b"")
        assert b"/tmp/" not in payload
        assert b"/home/" not in payload
        assert filename.removesuffix(".svg").encode() in payload


@pytest.mark.parametrize(
    ("step_index", "field", "replacement"),
    [
        (0, "exit_code", 11),
        (1, "stdout", []),
        (2, "input_fixture", "content-v1"),
        (3, "stderr", []),
        (4, "stdout", []),
        (5, "stdout_format", "jsonl"),
        (6, "exit_code", 12),
        (7, "input_fixture", "content-v2"),
        (8, "stdout_format", "jsonl"),
        (9, "id", "get-after-tombstone"),
    ],
)
def test_normalized_step_contract_rejects_drift(
    step_index: int,
    field: str,
    replacement: contract.JsonValue,
) -> None:
    document = valid_document()
    steps = cast(list[contract.JsonObject], document["steps"])
    steps[step_index][field] = replacement
    with pytest.raises(contract.EvidenceContractError):
        contract.validate_evidence_document(document)
