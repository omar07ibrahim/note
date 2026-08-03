from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, TextIO, cast

import pytest

from recall_ledger import (
    CommandId,
    ContractViolation,
    LedgerEvent,
    LedgerStorageError,
    NoteContent,
    NoteId,
    RetrievalContractError,
    TenantId,
    TransitionResult,
    cli,
)
from recall_ledger.retrieval import MAX_QUERY_BYTES

TENANT_A = "tn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TENANT_B = "tn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CREATE = "cmd_00000000000000000000000000000001"
REVISE = "cmd_00000000000000000000000000000002"
DELETE = "cmd_00000000000000000000000000000003"
OTHER = "cmd_00000000000000000000000000000004"
STALE = "cmd_00000000000000000000000000000005"
SEARCH_SECOND = "cmd_00000000000000000000000000000006"


def secure_directory(tmp_path: Path, name: str = "data") -> Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def content_bytes(
    *,
    title: str = "Operator fixture",
    body: str = "Untrusted evidence.",
    tags: tuple[str, ...] = ("demo",),
) -> bytes:
    return json.dumps(
        {"body": body, "tags": list(tags), "title": title},
        ensure_ascii=True,
        sort_keys=True,
    ).encode()


def scanned_content_bytes(*, title: str, body: str, tags: tuple[str, ...]) -> int:
    return len(title.encode()) + len(body.encode()) + sum(len(tag.encode()) for tag in tags)


def common_arguments(data_directory: Path, *, tenant_id: str = TENANT_A) -> list[str]:
    return [
        "--data-dir",
        str(data_directory),
        "--tenant-id",
        tenant_id,
    ]


def run_raw(
    arguments: list[str],
    *,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> tuple[int, str, str]:
    output = io.StringIO() if stdout is None else stdout
    errors = io.StringIO() if stderr is None else stderr
    exit_code = cli.run_cli(
        arguments,
        stdin=io.BytesIO() if stdin is None else stdin,
        stdout=output,
        stderr=errors,
    )
    output_text = output.getvalue() if isinstance(output, io.StringIO) else ""
    error_text = errors.getvalue() if isinstance(errors, io.StringIO) else ""
    return exit_code, output_text, error_text


def run_command(
    data_directory: Path,
    command_arguments: list[str],
    *,
    tenant_id: str = TENANT_A,
    stdin: bytes = b"",
    global_arguments: list[str] | None = None,
) -> tuple[int, str, str]:
    prefix = common_arguments(data_directory, tenant_id=tenant_id)
    if global_arguments is not None:
        prefix.extend(global_arguments)
    return run_raw([*prefix, *command_arguments], stdin=io.BytesIO(stdin))


def decoded_error(rendered: str) -> dict[str, object]:
    value = json.loads(rendered)
    assert type(value) is dict
    return cast(dict[str, object], value)


def create_note(data_directory: Path, *, body: str = "Untrusted evidence.") -> dict[str, object]:
    exit_code, output, errors = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=content_bytes(body=body),
    )
    assert exit_code == cli.EXIT_SUCCESS
    assert errors == ""
    value = json.loads(output)
    assert type(value) is dict
    return cast(dict[str, object], value)


def event_from_result(result: dict[str, object]) -> dict[str, object]:
    event = result["event"]
    assert type(event) is dict
    return cast(dict[str, object], event)


def test_complete_operator_workflow_and_safe_rendering(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    data_directory = secure_directory(tmp_path)
    unsafe_body = "line\x1b[31m red \u202e reversed"
    created = create_note(data_directory, body=unsafe_body)
    created_event = event_from_result(created)
    note_id = cast(str, created_event["note_id"])

    assert created["operation"] == "create"
    assert created["replayed"] is False

    content_path = tmp_path / "content.json"
    content_path.write_bytes(content_bytes(body=unsafe_body))
    replay_code, replay_output, replay_error = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            str(content_path),
        ],
        global_arguments=["--pretty"],
    )
    assert replay_code == cli.EXIT_SUCCESS
    assert replay_error == ""
    assert '\n  "event"' in replay_output
    assert "\x1b" not in replay_output
    assert "\u202e" not in replay_output
    assert "\\u001b" in replay_output
    assert "\\u202e" in replay_output
    replay = json.loads(replay_output)
    assert replay["replayed"] is True
    assert replay["event"] == created_event

    revised_code, revised_output, revised_error = run_command(
        data_directory,
        [
            "revise",
            "--note-id",
            note_id,
            "--command-id",
            REVISE,
            "--expected-revision",
            "1",
            "--content-file",
            "-",
        ],
        stdin=content_bytes(title="Operator fixture v2", body="Keep provenance."),
    )
    assert revised_code == cli.EXIT_SUCCESS
    assert revised_error == ""
    revised = json.loads(revised_output)
    assert revised["operation"] == "revise"
    assert revised["event"]["revision"] == 2

    get_code, get_output, get_error = run_command(
        data_directory,
        ["get", "--note-id", note_id],
    )
    assert get_code == cli.EXIT_SUCCESS
    assert get_error == ""
    assert json.loads(get_output)["event"]["revision"] == 2

    head_code, head_output, head_error = run_command(
        data_directory,
        ["head", "--note-id", note_id],
    )
    assert head_code == cli.EXIT_SUCCESS
    assert head_error == ""
    assert json.loads(head_output)["found"] is True

    history_code, history_output, history_error = run_command(
        data_directory,
        ["history", "--note-id", note_id, "--limit", "1"],
    )
    assert history_code == cli.EXIT_SUCCESS
    assert history_error == ""
    history = json.loads(history_output)
    assert history["found"] is True
    assert len(history["events"]) == 1
    assert history["next_after_revision"] == 1

    jsonl_code, jsonl_output, jsonl_error = run_command(
        data_directory,
        [
            "history",
            "--note-id",
            note_id,
            "--after-revision",
            "1",
            "--limit",
            "100",
            "--jsonl",
        ],
    )
    assert jsonl_code == cli.EXIT_SUCCESS
    assert jsonl_error == ""
    records = [json.loads(line) for line in jsonl_output.splitlines()]
    assert [record["record"] for record in records] == ["event", "page"]
    assert records[-1]["event_count"] == 1
    assert records[-1]["next_after_revision"] is None

    tombstone_code, tombstone_output, tombstone_error = run_command(
        data_directory,
        [
            "tombstone",
            "--note-id",
            note_id,
            "--command-id",
            DELETE,
            "--expected-revision",
            "2",
            "--reason",
            "user_request",
        ],
    )
    assert tombstone_code == cli.EXIT_SUCCESS
    assert tombstone_error == ""
    assert json.loads(tombstone_output)["event"]["content"] is None

    _, absent_output, _ = run_command(
        data_directory,
        ["get", "--note-id", note_id],
    )
    _, terminal_output, _ = run_command(
        data_directory,
        ["head", "--note-id", note_id],
    )
    assert json.loads(absent_output) == {
        "event": None,
        "found": False,
        "ok": True,
        "operation": "get",
    }
    assert json.loads(terminal_output)["event"]["kind"] == "note.tombstoned"

    missing_note = "nt_ffffffffffffffffffffffffffffffff"
    _, missing_history, _ = run_command(
        data_directory,
        ["history", "--note-id", missing_note, "--jsonl"],
        tenant_id=TENANT_B,
    )
    assert json.loads(missing_history)["record"] == "page"
    assert json.loads(missing_history)["found"] is False


def test_state_conflicts_have_stable_codes_and_guidance(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    created = create_note(data_directory)
    note_id = cast(str, event_from_result(created)["note_id"])

    missing_code, _, missing_error = run_command(
        data_directory,
        [
            "revise",
            "--note-id",
            "nt_ffffffffffffffffffffffffffffffff",
            "--command-id",
            REVISE,
            "--expected-revision",
            "1",
            "--content-file",
            "-",
        ],
        stdin=content_bytes(),
    )
    assert missing_code == cli.EXIT_STATE
    assert decoded_error(missing_error)["error"] == {
        "code": "NOTE_NOT_FOUND",
        "message": "the tenant-scoped note does not exist",
        "retry": "none",
    }

    conflict_code, _, conflict_error = run_command(
        data_directory,
        [
            "revise",
            "--note-id",
            note_id,
            "--command-id",
            REVISE,
            "--expected-revision",
            "2",
            "--content-file",
            "-",
        ],
        stdin=content_bytes(),
    )
    assert conflict_code == cli.EXIT_STATE
    assert cast(dict[str, object], decoded_error(conflict_error)["error"])["retry"] == (
        "inspect_head_then_new_command"
    )

    idempotency_code, _, idempotency_error = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=content_bytes(body="different intent"),
    )
    assert idempotency_code == cli.EXIT_STATE
    assert cast(dict[str, object], decoded_error(idempotency_error)["error"])["retry"] == (
        "do_not_retry_same_command"
    )

    revise_code, revise_output, _ = run_command(
        data_directory,
        [
            "revise",
            "--note-id",
            note_id,
            "--command-id",
            OTHER,
            "--expected-revision",
            "1",
            "--content-file",
            "-",
        ],
        stdin=content_bytes(),
    )
    assert revise_code == cli.EXIT_SUCCESS
    assert json.loads(revise_output)["event"]["revision"] == 2
    delete_code, _, _ = run_command(
        data_directory,
        [
            "tombstone",
            "--note-id",
            note_id,
            "--command-id",
            DELETE,
            "--expected-revision",
            "2",
            "--reason",
            "administrative",
        ],
    )
    assert delete_code == cli.EXIT_SUCCESS
    terminal_code, _, terminal_error = run_command(
        data_directory,
        [
            "tombstone",
            "--note-id",
            note_id,
            "--command-id",
            STALE,
            "--expected-revision",
            "3",
            "--reason",
            "retention_policy",
        ],
    )
    assert terminal_code == cli.EXIT_STATE
    assert cast(dict[str, object], decoded_error(terminal_error)["error"])["code"] == (
        "NOTE_TOMBSTONED"
    )


def seed_search_corpus(
    data_directory: Path,
) -> tuple[dict[str, object], dict[str, object], int]:
    first = create_note(data_directory, body="alpha beta in the body")
    first_event = event_from_result(first)
    second_content = content_bytes(
        title="Alpha beta runbook",
        body="Operational retrieval evidence.",
        tags=("portfolio", "search"),
    )
    second_code, second_output, second_error = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            SEARCH_SECOND,
            "--content-file",
            "-",
        ],
        stdin=second_content,
    )
    assert second_code == cli.EXIT_SUCCESS
    assert second_error == ""
    second_event = event_from_result(cast(dict[str, object], json.loads(second_output)))
    expected_bytes = scanned_content_bytes(
        title="Operator fixture",
        body="alpha beta in the body",
        tags=("demo",),
    ) + scanned_content_bytes(
        title="Alpha beta runbook",
        body="Operational retrieval evidence.",
        tags=("portfolio", "search"),
    )
    return first_event, second_event, expected_bytes


def test_search_json_exposes_ranking_citations_and_scan_accounting(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    _, second_event, expected_bytes = seed_search_corpus(data_directory)

    search_code, search_output, search_error = run_command(
        data_directory,
        ["search", "--query-file", "-", "--limit", "1"],
        stdin="\uff21\uff2c\uff30\uff28\uff21 beta".encode(),
    )
    assert search_code == cli.EXIT_SUCCESS
    assert search_error == ""
    document = cast(dict[str, object], json.loads(search_output))
    assert set(document) == {
        "hits",
        "limit",
        "ok",
        "operation",
        "query",
        "scanned_content_bytes",
        "scanned_heads",
        "scanned_live_notes",
        "tenant_id",
        "total_matches",
        "truncated",
    }
    assert document["operation"] == "search"
    assert document["ok"] is True
    assert document["tenant_id"] == TENANT_A
    assert document["limit"] == 1
    assert document["total_matches"] == 2
    assert document["truncated"] is True
    assert document["scanned_heads"] == 2
    assert document["scanned_live_notes"] == 2
    assert document["scanned_content_bytes"] == expected_bytes
    query = cast(dict[str, object], document["query"])
    assert set(query) == {
        "contract_version",
        "encoded_terms",
        "match_expression",
        "terms",
        "unicode_profile",
    }
    assert query["terms"] == ["alpha", "beta"]
    assert query["encoded_terms"] == ["u616c706861", "u62657461"]
    assert query["match_expression"] == '"u616c706861" AND "u62657461"'
    assert query["contract_version"] == 1
    assert cast(str, query["unicode_profile"]).startswith("nfkc-casefold-nfkc+")

    hits = cast(list[dict[str, object]], document["hits"])
    assert len(hits) == 1
    assert set(hits[0]) == {"citation", "content", "rank", "recorded_at_us", "score"}
    assert hits[0]["rank"] == 1
    citation = cast(dict[str, object], hits[0]["citation"])
    assert set(citation) == {"event_hash", "note_id", "revision", "tenant_id"}
    assert citation == {
        "event_hash": second_event["event_hash"],
        "note_id": second_event["note_id"],
        "revision": 1,
        "tenant_id": TENANT_A,
    }
    assert hits[0]["content"] == {
        "body": "Operational retrieval evidence.",
        "tags": ["portfolio", "search"],
        "title": "Alpha beta runbook",
    }
    score = cast(dict[str, object], hits[0]["score"])
    assert set(score) == {
        "body_phrase",
        "body_term_frequency",
        "contract_version",
        "tag_phrase",
        "tag_term_frequency",
        "title_phrase",
        "title_term_frequency",
        "total",
        "unicode_profile",
    }
    assert score["total"] == 48
    assert score["title_term_frequency"] == 2
    assert score["title_phrase"] is True
    assert score["body_phrase"] is False
    assert score["tag_phrase"] is False
    assert score["contract_version"] == query["contract_version"]
    assert score["unicode_profile"] == query["unicode_profile"]


def test_search_jsonl_emits_ranked_hits_then_summary_and_handles_empty_tenant(
    tmp_path: Path,
) -> None:
    data_directory = secure_directory(tmp_path)
    first_event, second_event, _ = seed_search_corpus(data_directory)

    jsonl_code, jsonl_output, jsonl_error = run_command(
        data_directory,
        ["search", "--query-file", "-", "--limit", "2", "--jsonl"],
        stdin=b"alpha beta",
    )
    assert jsonl_code == cli.EXIT_SUCCESS
    assert jsonl_error == ""
    records = [cast(dict[str, object], json.loads(line)) for line in jsonl_output.splitlines()]
    assert [record["record"] for record in records] == ["hit", "hit", "summary"]
    assert cast(dict[str, object], records[0]["hit"])["rank"] == 1
    assert cast(dict[str, object], records[1]["hit"])["rank"] == 2
    assert (
        cast(dict[str, object], cast(dict[str, object], records[0]["hit"])["citation"])["note_id"]
        == second_event["note_id"]
    )
    assert (
        cast(dict[str, object], cast(dict[str, object], records[1]["hit"])["citation"])["note_id"]
        == first_event["note_id"]
    )
    assert records[-1]["hit_count"] == records[-1]["total_matches"] == 2
    assert records[-1]["truncated"] is False
    assert records[-1]["scanned_heads"] == 2
    assert set(records[-1]) == {
        "hit_count",
        "limit",
        "query",
        "record",
        "scanned_content_bytes",
        "scanned_heads",
        "scanned_live_notes",
        "tenant_id",
        "total_matches",
        "truncated",
    }

    empty_code, empty_output, empty_error = run_command(
        data_directory,
        ["search", "--query-file", "-", "--jsonl"],
        tenant_id=TENANT_B,
        stdin=b"alpha",
    )
    assert empty_code == cli.EXIT_SUCCESS
    assert empty_error == ""
    empty_summary = cast(dict[str, object], json.loads(empty_output))
    assert empty_summary["record"] == "summary"
    assert empty_summary["hit_count"] == empty_summary["total_matches"] == 0
    assert empty_summary["truncated"] is False
    assert empty_summary["scanned_heads"] == empty_summary["scanned_live_notes"] == 0


def test_search_pretty_output_remains_one_valid_document(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, output, errors = run_command(
        data_directory,
        ["search", "--query-file", "-"],
        stdin=b"alpha",
        global_arguments=["--pretty"],
    )
    assert exit_code == cli.EXIT_SUCCESS
    assert errors == ""
    assert output.startswith('{\n  "hits": []')
    assert output.endswith("\n}\n")
    assert cast(dict[str, object], json.loads(output))["operation"] == "search"


class _ReadFailure:
    def read(self, _size: int = -1) -> bytes:
        raise OSError


class _TextReader:
    def read(self, _size: int = -1) -> str:
        return "{}"


class _ChunkedReader:
    def __init__(self, payload: bytes, *, chunk_size: int) -> None:
        self.payload = payload
        self.chunk_size = chunk_size
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        accepted = min(size, self.chunk_size, len(self.payload) - self.offset)
        chunk = self.payload[self.offset : self.offset + accepted]
        self.offset += accepted
        return chunk


class _OverReturningReader:
    def read(self, size: int = -1) -> bytes:
        return b"x" * (size + 1)


def test_search_query_rejections_are_safe_request_errors(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, output, errors = run_command(
        data_directory,
        ["search", "--query-file", "-"],
        stdin=b"<> _ -",
    )
    assert exit_code == cli.EXIT_REQUEST
    assert output == ""
    assert decoded_error(errors)["error"] == {
        "code": "EMPTY_QUERY",
        "message": "the query must contain at least one letter or number",
        "retry": "none",
    }


@pytest.mark.parametrize(
    ("stream", "expected_code"),
    (
        (io.BytesIO(b"\xff"), "INPUT_INVALID_UTF8"),
        (io.BytesIO(b"a" * (MAX_QUERY_BYTES + 1)), "INPUT_TOO_LARGE"),
        (_ReadFailure(), "INPUT_UNAVAILABLE"),
        (_TextReader(), "INPUT_INVALID_UTF8"),
        (_OverReturningReader(), "INPUT_TOO_LARGE"),
    ),
)
def test_search_query_stdin_is_bounded_before_storage_open(
    tmp_path: Path,
    stream: object,
    expected_code: str,
) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, output, errors = run_raw(
        [*common_arguments(data_directory), "search", "--query-file", "-"],
        stdin=cast(BinaryIO, stream),
    )
    assert exit_code == cli.EXIT_INPUT
    assert output == ""
    assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == expected_code
    assert list(data_directory.iterdir()) == []


def test_search_query_reader_consumes_multiple_bounded_chunks(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, output, errors = run_raw(
        [*common_arguments(data_directory), "search", "--query-file", "-"],
        stdin=cast(BinaryIO, _ChunkedReader(b"alpha beta", chunk_size=2)),
    )
    assert exit_code == cli.EXIT_SUCCESS
    assert errors == ""
    document = cast(dict[str, object], json.loads(output))
    assert cast(dict[str, object], document["query"])["terms"] == ["alpha", "beta"]


def test_search_query_input_is_not_trimmed_and_limit_is_storage_validated(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    untrimmed_code, untrimmed_output, untrimmed_error = run_command(
        data_directory,
        ["search", "--query-file", "-"],
        stdin=b"a" + b" " * 512,
    )
    assert untrimmed_code == cli.EXIT_REQUEST
    assert untrimmed_output == ""
    assert cast(dict[str, object], decoded_error(untrimmed_error)["error"])["code"] == (
        "QUERY_TOO_LARGE"
    )

    limit_code, limit_output, limit_error = run_command(
        data_directory,
        ["search", "--query-file", "-", "--limit", "101"],
        stdin=b"alpha",
    )
    assert limit_code == cli.EXIT_REQUEST
    assert limit_output == ""
    assert cast(dict[str, object], decoded_error(limit_error)["error"])["code"] == (
        "INVALID_SEARCH_LIMIT"
    )


def test_search_query_file_rejects_unsafe_paths_without_reflection(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    target = tmp_path / "private-query.txt"
    target.write_text("alpha", encoding="utf-8")
    symlink = tmp_path / "private-query-link"
    symlink.symlink_to(target)
    fifo = tmp_path / "private-query-fifo"
    os.mkfifo(fifo)
    directory = tmp_path / "private-query-directory"
    directory.mkdir()

    for path_text, expected_code in (
        (str(symlink), "INPUT_UNAVAILABLE"),
        (str(fifo), "INPUT_UNSAFE_FILE"),
        (str(directory), "INPUT_UNSAFE_FILE"),
        (f"{tmp_path}\x00private", "INPUT_UNAVAILABLE"),
        (str(tmp_path / "private-missing-query"), "INPUT_UNAVAILABLE"),
    ):
        exit_code, output, errors = run_command(
            data_directory,
            ["search", "--query-file", path_text],
        )
        assert exit_code == cli.EXIT_INPUT
        assert output == ""
        assert path_text not in errors
        assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == expected_code
    assert list(data_directory.iterdir()) == []


def test_search_reads_exact_regular_query_file_and_treats_operators_as_terms(
    tmp_path: Path,
) -> None:
    data_directory = secure_directory(tmp_path)
    create_note(data_directory, body="alpha only")
    query_path = tmp_path / "query.txt"
    query_path.write_bytes(b'" OR * NEAR(alpha)')

    exit_code, output, errors = run_command(
        data_directory,
        ["search", "--query-file", str(query_path)],
    )
    assert exit_code == cli.EXIT_SUCCESS
    assert errors == ""
    document = cast(dict[str, object], json.loads(output))
    assert cast(dict[str, object], document["query"])["terms"] == ["or", "near", "alpha"]
    assert document["total_matches"] == 0
    assert document["hits"] == []


@pytest.mark.parametrize(
    "arguments,private_value",
    [
        ([], ""),
        (["--data-d", "\x1b[31mPRIVATE"], "\x1b[31mPRIVATE"),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "create",
                "--command-i",
                "\x1b[31mPRIVATE",
            ],
            "\x1b[31mPRIVATE",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "--busy-timeout-ms",
                "+1",
                "get",
                "--note-id",
                "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
            "+1",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "--busy-timeout-ms",
                "01",
                "get",
                "--note-id",
                "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
            "01",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "--busy-timeout-ms",
                "\u0661",
                "get",
                "--note-id",
                "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
            "\u0661",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "--busy-timeout-ms",
                "99999999999999999999",
                "get",
                "--note-id",
                "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
            "99999999999999999999",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--data-dir",
                "/operator/private-second",
                "--tenant-id",
                TENANT_A,
                "get",
                "--note-id",
                "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
            "/operator/private-second",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "create",
                "--command-id",
                CREATE,
                "--command-id",
                OTHER,
                "--content-file",
                "-",
            ],
            OTHER,
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "history",
                "--note-id",
                "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "--limit",
                "1",
                "--limit",
                "99",
            ],
            "99",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "search",
                "--query-file",
                "alpha",
                "--query-file",
                "\x1b[31mPRIVATE",
            ],
            "\x1b[31mPRIVATE",
        ),
        (
            [
                "--data-dir",
                "/operator/unused",
                "--tenant-id",
                TENANT_A,
                "search",
                "--query-file",
                "-",
                "--limit",
                "01",
            ],
            "01",
        ),
    ],
)
def test_parser_rejections_never_reflect_argv(arguments: list[str], private_value: str) -> None:
    exit_code, output, errors = run_raw(arguments)

    assert exit_code == cli.EXIT_USAGE
    assert output == ""
    assert all(ord(character) < 128 for character in errors)
    if private_value:
        assert private_value not in errors
    assert decoded_error(errors)["error"] == {
        "code": "CLI_USAGE",
        "message": "invalid command arguments; use --help for the supported shape",
        "retry": "none",
    }


def test_help_uses_injected_streams_without_required_context(tmp_path: Path) -> None:
    global_code, global_help, global_error = run_raw(["--help"])
    assert global_code == cli.EXIT_SUCCESS
    assert global_help.startswith("usage: recall-ledger")
    assert global_error == ""

    sub_code, sub_help, sub_error = run_raw(
        [
            *common_arguments(tmp_path / "not-opened"),
            "create",
            "--help",
        ]
    )
    assert sub_code == cli.EXIT_SUCCESS
    assert "usage: recall-ledger create" in sub_help
    assert "--command-id" in sub_help
    assert sub_error == ""

    search_code, search_help, search_error = run_raw(
        [*common_arguments(tmp_path / "not-opened"), "search", "--help"]
    )
    assert search_code == cli.EXIT_SUCCESS
    assert "usage: recall-ledger search" in search_help
    assert "--query-file" in search_help
    assert "--jsonl" in search_help
    assert search_error == ""


def test_execute_rejects_an_injected_command_outside_the_parser_allowlist(
    tmp_path: Path,
) -> None:
    data_directory = secure_directory(tmp_path)
    namespace = Namespace(
        command="private-command",
        data_dir=str(data_directory),
        tenant_id=TENANT_A,
    )
    with pytest.raises(cli._CliError) as captured:
        cli._execute(namespace, stdin=io.BytesIO())
    assert captured.value.exit_code == cli.EXIT_USAGE
    assert captured.value.code == "CLI_USAGE"
    assert list(data_directory.iterdir()) == []


@pytest.mark.parametrize(
    "command_arguments",
    (
        [
            "history",
            "--note-id",
            "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "--jsonl",
        ],
        ["search", "--query-file", "-", "--jsonl"],
    ),
)
def test_pretty_and_jsonl_are_mutually_exclusive(
    tmp_path: Path,
    command_arguments: list[str],
) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, output, errors = run_command(
        data_directory,
        command_arguments,
        global_arguments=["--pretty"],
    )
    assert exit_code == cli.EXIT_USAGE
    assert output == ""
    assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == "CLI_USAGE"


@pytest.mark.parametrize(
    "payload,expected_code",
    [
        (b"\xff", "INPUT_INVALID_UTF8"),
        (b"", "INPUT_INVALID_JSON"),
        (b"{", "INPUT_INVALID_JSON"),
        (b"[", "INPUT_INVALID_JSON"),
        (b'{"body":"","tags":[],"title":"a","title":"b"}', "INPUT_DUPLICATE_KEY"),
        (b'{"body":NaN,"tags":[],"title":"a"}', "INPUT_INVALID_JSON"),
        (b"[" * 2_000 + b"]" * 2_000, "INPUT_INVALID_SHAPE"),
        (b" \t\r\n" + b"[" * 2_000 + b"]" * 2_000, "INPUT_INVALID_SHAPE"),
        (b"[]", "INPUT_INVALID_SHAPE"),
        (b'{"body":"","tags":[]}', "INPUT_INVALID_SHAPE"),
        (
            b'{"body":"","tags":[],"tenant_id":"tn_bad","title":"a"}',
            "INPUT_INVALID_SHAPE",
        ),
        (b'{"body":"","tags":[],"title":1}', "INPUT_INVALID_SHAPE"),
        (b'{"body":1,"tags":[],"title":"a"}', "INPUT_INVALID_SHAPE"),
        (b'{"body":"","tags":"x","title":"a"}', "INPUT_INVALID_SHAPE"),
        (b'{"body":"","tags":[1],"title":"a"}', "INPUT_INVALID_SHAPE"),
    ],
)
def test_strict_json_input_rejections(
    tmp_path: Path,
    payload: bytes,
    expected_code: str,
) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, output, errors = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=payload,
    )
    assert exit_code == cli.EXIT_INPUT
    assert output == ""
    assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == expected_code
    assert list(data_directory.iterdir()) == []


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b" \n[[[]]]", "INPUT_INVALID_SHAPE"),
        (b"{}", "INPUT_INVALID_JSON"),
        (b" \t\r\n", "INPUT_INVALID_JSON"),
    ],
)
def test_root_classification_does_not_depend_on_decoder_recursion(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    expected_code: str,
) -> None:
    def recursion_varies_by_runtime(*_args: object, **_kwargs: object) -> object:
        raise RecursionError

    monkeypatch.setattr(json, "loads", recursion_varies_by_runtime)

    with pytest.raises(cli._CliError) as captured:
        cli._read_content("-", io.BytesIO(payload))

    assert captured.value.exit_code == cli.EXIT_INPUT
    assert captured.value.code == expected_code


def test_content_contract_rejection_is_exit_ten_before_storage_open(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, output, errors = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=content_bytes(title="   "),
    )
    assert exit_code == cli.EXIT_REQUEST
    assert output == ""
    assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == "BLANK_TEXT"
    assert list(data_directory.iterdir()) == []


@pytest.mark.parametrize(
    "stream,expected_code",
    [
        (_ReadFailure(), "INPUT_UNAVAILABLE"),
        (_TextReader(), "INPUT_INVALID_UTF8"),
    ],
)
def test_stdin_reader_failures_are_bounded(
    tmp_path: Path,
    stream: object,
    expected_code: str,
) -> None:
    data_directory = secure_directory(tmp_path)
    exit_code, _, errors = run_raw(
        [
            *common_arguments(data_directory),
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=cast(BinaryIO, stream),
    )
    assert exit_code == cli.EXIT_INPUT
    assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == expected_code


def test_closed_and_oversized_stdin_are_bounded(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    closed = io.BytesIO()
    closed.close()
    closed_code, _, closed_error = run_raw(
        [
            *common_arguments(data_directory),
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=closed,
    )
    assert closed_code == cli.EXIT_INPUT
    assert cast(dict[str, object], decoded_error(closed_error)["error"])["code"] == (
        "INPUT_UNAVAILABLE"
    )

    oversized_code, _, oversized_error = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=b"x" * (cli._MAX_CONTENT_INPUT_BYTES + 1),
    )
    assert oversized_code == cli.EXIT_INPUT
    assert cast(dict[str, object], decoded_error(oversized_error)["error"])["code"] == (
        "INPUT_TOO_LARGE"
    )

    closed_query = io.BytesIO()
    closed_query.close()
    query_code, query_output, query_error = run_raw(
        [*common_arguments(data_directory), "search", "--query-file", "-"],
        stdin=closed_query,
    )
    assert query_code == cli.EXIT_INPUT
    assert query_output == ""
    assert cast(dict[str, object], decoded_error(query_error)["error"])["code"] == (
        "INPUT_UNAVAILABLE"
    )


def test_content_file_rejects_symlink_fifo_directory_and_nul(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    target = tmp_path / "target.json"
    target.write_bytes(content_bytes())
    symlink = tmp_path / "content-link"
    symlink.symlink_to(target)
    fifo = tmp_path / "content-fifo"
    os.mkfifo(fifo)
    input_directory = tmp_path / "content-directory"
    input_directory.mkdir()

    for path_text, expected_code in (
        (str(symlink), "INPUT_UNAVAILABLE"),
        (str(input_directory), "INPUT_UNSAFE_FILE"),
        (f"{tmp_path}\x00private", "INPUT_UNAVAILABLE"),
        (str(tmp_path / "missing.json"), "INPUT_UNAVAILABLE"),
    ):
        exit_code, output, errors = run_command(
            data_directory,
            [
                "create",
                "--command-id",
                CREATE,
                "--content-file",
                path_text,
            ],
        )
        assert exit_code == cli.EXIT_INPUT
        assert output == ""
        assert path_text not in errors
        assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == expected_code

    fifo_descriptor = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(cli._CliError) as fifo_error:
            cli._require_regular_file(fifo_descriptor)
    finally:
        os.close(fifo_descriptor)
    assert fifo_error.value.code == "INPUT_UNSAFE_FILE"


def test_content_open_sets_no_follow_and_nonblocking_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "content.json"
    expected = content_bytes()
    fixture.write_bytes(expected)
    real_open = os.open
    observed_flags: list[int] = []

    def observed_open(path: str, flags: int) -> int:
        observed_flags.append(flags)
        return real_open(path, flags)

    with monkeypatch.context() as context:
        context.setattr(os, "open", observed_open)
        assert cli._read_content_file(str(fixture)) == expected
    assert len(observed_flags) == 1
    assert observed_flags[0] & os.O_NOFOLLOW
    assert observed_flags[0] & os.O_NONBLOCK


def test_content_descriptor_close_failure_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_directory = secure_directory(tmp_path)

    monkeypatch.setattr(os, "open", lambda *_args: 123)

    def failed_fstat(_descriptor: int) -> os.stat_result:
        raise OSError

    def failed_close(_descriptor: int) -> None:
        raise OSError

    monkeypatch.setattr(os, "fstat", failed_fstat)
    monkeypatch.setattr(os, "close", failed_close)
    exit_code, _, errors = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "private-path",
        ],
    )
    assert exit_code == cli.EXIT_INPUT
    assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == ("INPUT_UNAVAILABLE")


@pytest.mark.parametrize(
    "code,expected",
    [
        ("INVALID_HISTORY_LIMIT", cli.EXIT_REQUEST),
        ("INVALID_SEARCH_LIMIT", cli.EXIT_REQUEST),
        ("NOTE_NOT_FOUND", cli.EXIT_STATE),
        ("LEDGER_BUSY", cli.EXIT_BUSY),
        ("COMMIT_OUTCOME_UNKNOWN", cli.EXIT_UNCERTAIN),
        ("MIGRATION_FAILED", cli.EXIT_STORAGE_SAFETY),
        ("SEARCH_INVENTORY_TOO_LARGE", cli.EXIT_STORAGE),
        ("SEARCH_CORPUS_TOO_LARGE", cli.EXIT_STORAGE),
        ("DATABASE_FULL", cli.EXIT_STORAGE),
    ],
)
def test_storage_exit_code_groups(code: str, expected: int) -> None:
    assert cli._storage_exit_code(code) == expected


@pytest.mark.parametrize(
    "code,command,expected",
    [
        ("LEDGER_BUSY", "create", "retry_exact_command"),
        ("LEDGER_BUSY", "get", "retry_same_invocation"),
        ("ROLLBACK_FAILED", "revise", "reopen_and_retry_exact_command"),
        ("ROLLBACK_FAILED", "history", "reopen_and_retry_same_invocation"),
        ("ROLLBACK_FAILED", "search", "reopen_and_retry_same_invocation"),
        ("REVISION_CONFLICT", "revise", "inspect_head_then_new_command"),
        ("IDEMPOTENCY_CONFLICT", "create", "do_not_retry_same_command"),
        ("DATABASE_FULL", None, "none"),
    ],
)
def test_retry_guidance_groups(
    code: str,
    command: str | None,
    expected: str,
) -> None:
    assert cli._retry_guidance(code, command=command) == expected


@pytest.mark.parametrize(
    "error,expected_exit,expected_code",
    [
        (ContractViolation("INVALID_TEXT", "safe contract"), cli.EXIT_REQUEST, "INVALID_TEXT"),
        (RetrievalContractError("EMPTY_QUERY", "safe query"), cli.EXIT_REQUEST, "EMPTY_QUERY"),
        (
            RetrievalContractError("CONTENT_TOKEN_STREAM_TOO_LARGE", "safe content bound"),
            cli.EXIT_STORAGE,
            "CONTENT_TOKEN_STREAM_TOO_LARGE",
        ),
        (
            RetrievalContractError("UNICODE_PROFILE_MISMATCH", "private derived state"),
            cli.EXIT_INTERNAL,
            "INTERNAL_ERROR",
        ),
        (
            LedgerStorageError("LEDGER_BUSY", "safe storage"),
            cli.EXIT_BUSY,
            "LEDGER_BUSY",
        ),
        (KeyboardInterrupt(), cli.EXIT_INTERRUPTED, "INTERRUPTED"),
        (RuntimeError("private internal"), cli.EXIT_INTERNAL, "INTERNAL_ERROR"),
    ],
)
def test_top_level_exception_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_exit: int,
    expected_code: str,
) -> None:
    data_directory = secure_directory(tmp_path)

    def failed_execute(
        _namespace: object,
        *,
        stdin: BinaryIO,
    ) -> cli._RenderedResult:
        del stdin
        raise error

    monkeypatch.setattr(cli, "_execute", failed_execute)
    exit_code, output, errors = run_command(
        data_directory,
        ["get", "--note-id", "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    )
    assert exit_code == expected_exit
    assert output == ""
    assert "private internal" not in errors
    assert cast(dict[str, object], decoded_error(errors)["error"])["code"] == expected_code


class _FakeLedger:
    def __init__(
        self,
        *,
        operation_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.operation_error = operation_error
        self.close_error = close_error
        self.closed = False

    def get_note(self, *, tenant_id: TenantId, note_id: NoteId) -> LedgerEvent | None:
        del tenant_id, note_id
        if self.operation_error is not None:
            raise self.operation_error
        return None

    def create_note(
        self,
        *,
        tenant_id: TenantId,
        command_id: CommandId,
        content: NoteContent,
    ) -> TransitionResult:
        if self.operation_error is not None:
            raise self.operation_error
        event = LedgerEvent.create(
            tenant_id=tenant_id,
            note_id=NoteId("nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            command_id=command_id,
            recorded_at_us=1,
            content=content,
        )
        return TransitionResult(event=event, replayed=False)

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _LedgerFactory:
    def __init__(self, ledger: _FakeLedger) -> None:
        self.ledger = ledger

    def open(self, _directory: Path, *, busy_timeout_ms: int) -> _FakeLedger:
        del busy_timeout_ms
        return self.ledger


class _CloseCheckingWriter(io.StringIO):
    def __init__(self, ledger: _FakeLedger) -> None:
        super().__init__()
        self.ledger = ledger

    def write(self, value: str) -> int:
        assert self.ledger.closed
        return super().write(value)


def test_ledger_closes_before_success_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_directory = secure_directory(tmp_path)
    ledger = _FakeLedger()
    monkeypatch.setattr(cli, "SQLiteLedger", _LedgerFactory(ledger))
    output = _CloseCheckingWriter(ledger)
    errors = io.StringIO()
    exit_code = cli.run_cli(
        [
            *common_arguments(data_directory),
            "get",
            "--note-id",
            "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
        stdin=io.BytesIO(),
        stdout=output,
        stderr=errors,
    )
    assert exit_code == cli.EXIT_SUCCESS
    assert errors.getvalue() == ""


def test_close_failure_prevents_success_output_and_preserves_uncertain_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_directory = secure_directory(tmp_path)
    close_failure = LedgerStorageError("DATABASE_CLOSE_FAILED", "safe close")
    success_ledger = _FakeLedger(close_error=close_failure)
    monkeypatch.setattr(cli, "SQLiteLedger", _LedgerFactory(success_ledger))
    success_code, success_output, success_error = run_command(
        data_directory,
        ["get", "--note-id", "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    )
    assert success_code == cli.EXIT_STORAGE
    assert success_output == ""
    assert cast(dict[str, object], decoded_error(success_error)["error"])["code"] == (
        "DATABASE_CLOSE_FAILED"
    )
    assert cast(dict[str, object], decoded_error(success_error)["error"])["retry"] == (
        "retry_same_invocation"
    )

    uncertain = LedgerStorageError("COMMIT_OUTCOME_UNKNOWN", "safe uncertain")
    uncertain_ledger = _FakeLedger(
        operation_error=uncertain,
        close_error=close_failure,
    )
    monkeypatch.setattr(cli, "SQLiteLedger", _LedgerFactory(uncertain_ledger))
    uncertain_code, uncertain_output, uncertain_error = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=content_bytes(),
    )
    assert uncertain_code == cli.EXIT_UNCERTAIN
    assert uncertain_output == ""
    assert decoded_error(uncertain_error)["error"] == {
        "code": "COMMIT_OUTCOME_UNKNOWN",
        "message": "safe uncertain",
        "retry": "reopen_and_retry_exact_command",
    }

    contract_ledger = _FakeLedger(
        operation_error=ContractViolation("INVALID_TEXT", "safe operation"),
        close_error=close_failure,
    )
    monkeypatch.setattr(cli, "SQLiteLedger", _LedgerFactory(contract_ledger))
    contract_code, _, contract_error = run_command(
        data_directory,
        ["get", "--note-id", "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    )
    assert contract_code == cli.EXIT_STORAGE
    assert cast(dict[str, object], decoded_error(contract_error)["error"])["code"] == (
        "DATABASE_CLOSE_FAILED"
    )

    committed_ledger = _FakeLedger(close_error=close_failure)
    monkeypatch.setattr(cli, "SQLiteLedger", _LedgerFactory(committed_ledger))
    committed_code, committed_output, committed_error = run_command(
        data_directory,
        [
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=content_bytes(),
    )
    assert committed_code == cli.EXIT_STORAGE
    assert committed_output == ""
    assert cast(dict[str, object], decoded_error(committed_error)["error"])["retry"] == (
        "retry_exact_command"
    )


def test_close_failure_mapper_handles_interrupt_and_unexpected_error() -> None:
    with pytest.raises(cli._CliError) as interrupted:
        cli._raise_close_failure(KeyboardInterrupt(), command="create")
    assert interrupted.value.exit_code == cli.EXIT_INTERRUPTED
    assert interrupted.value.code == "INTERRUPTED"
    assert interrupted.value.retry == "retry_exact_command"

    private_error = RuntimeError("private close")
    with pytest.raises(RuntimeError) as unexpected:
        cli._raise_close_failure(private_error, command="get")
    assert unexpected.value is private_error


class _FailingWriter(io.StringIO):
    def __init__(self, error_factory: Callable[[], BaseException]) -> None:
        super().__init__()
        self.error_factory = error_factory

    def write(self, _value: str) -> int:
        raise self.error_factory()


class _PartialWriter:
    def __init__(self, chunk_size: int) -> None:
        self.chunk_size = chunk_size
        self.parts: list[str] = []
        self.flushed = False

    def write(self, value: str) -> int:
        accepted = min(self.chunk_size, len(value))
        self.parts.append(value[:accepted])
        return accepted

    def flush(self) -> None:
        self.flushed = True


class _InvalidWriteResult:
    def __init__(self, result: object) -> None:
        self.result = result

    def write(self, _value: str) -> object:
        return self.result

    def flush(self) -> None:
        return


def test_output_failures_preserve_post_commit_retry_context(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    errors = io.StringIO()
    create_exit = cli.run_cli(
        [
            *common_arguments(data_directory),
            "create",
            "--command-id",
            CREATE,
            "--content-file",
            "-",
        ],
        stdin=io.BytesIO(content_bytes(body="receipt must be replayable")),
        stdout=_FailingWriter(BrokenPipeError),
        stderr=errors,
    )
    assert create_exit == cli.EXIT_OUTPUT
    assert decoded_error(errors.getvalue())["error"] == {
        "code": "OUTPUT_UNAVAILABLE",
        "message": "the JSON result could not be written",
        "retry": "retry_exact_command",
    }

    replay = create_note(data_directory, body="receipt must be replayable")
    assert replay["replayed"] is True

    read_errors = io.StringIO()
    read_exit = cli.run_cli(
        [
            *common_arguments(data_directory),
            "get",
            "--note-id",
            cast(str, event_from_result(replay)["note_id"]),
        ],
        stdin=io.BytesIO(),
        stdout=_FailingWriter(OSError),
        stderr=read_errors,
    )
    assert read_exit == cli.EXIT_OUTPUT
    assert (
        cast(dict[str, object], decoded_error(read_errors.getvalue())["error"])["retry"]
        == "retry_same_invocation"
    )

    interrupted_errors = io.StringIO()
    interrupted_exit = cli.run_cli(
        [
            *common_arguments(data_directory),
            "get",
            "--note-id",
            cast(str, event_from_result(replay)["note_id"]),
        ],
        stdin=io.BytesIO(),
        stdout=_FailingWriter(KeyboardInterrupt),
        stderr=interrupted_errors,
    )
    assert interrupted_exit == cli.EXIT_INTERRUPTED
    assert decoded_error(interrupted_errors.getvalue())["error"] == {
        "code": "INTERRUPTED",
        "message": "writing the JSON result was interrupted",
        "retry": "retry_same_invocation",
    }


def test_partial_output_progress_completes_and_invalid_counts_fail(
    tmp_path: Path,
) -> None:
    data_directory = secure_directory(tmp_path)
    partial = _PartialWriter(chunk_size=3)
    partial_errors = io.StringIO()
    partial_exit = cli.run_cli(
        [
            *common_arguments(data_directory),
            "get",
            "--note-id",
            "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
        stdin=io.BytesIO(),
        stdout=cast(TextIO, partial),
        stderr=partial_errors,
    )
    assert partial_exit == cli.EXIT_SUCCESS
    assert partial.flushed
    assert json.loads("".join(partial.parts))["found"] is False
    assert partial_errors.getvalue() == ""

    for invalid_count in (0, -1, 10_000, None, True):
        errors = io.StringIO()
        invalid_exit = cli.run_cli(
            [
                *common_arguments(data_directory),
                "get",
                "--note-id",
                "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
            stdin=io.BytesIO(),
            stdout=cast(TextIO, _InvalidWriteResult(invalid_count)),
            stderr=errors,
        )
        assert invalid_exit == cli.EXIT_OUTPUT
        assert cast(dict[str, object], decoded_error(errors.getvalue())["error"])["code"] == (
            "OUTPUT_UNAVAILABLE"
        )


def test_error_and_help_output_failures_are_stable() -> None:
    error_exit = cli.run_cli(
        [],
        stdin=io.BytesIO(),
        stdout=io.StringIO(),
        stderr=_FailingWriter(BrokenPipeError),
    )
    assert error_exit == cli.EXIT_OUTPUT

    interrupted_error_exit = cli.run_cli(
        [],
        stdin=io.BytesIO(),
        stdout=io.StringIO(),
        stderr=_FailingWriter(KeyboardInterrupt),
    )
    assert interrupted_error_exit == cli.EXIT_INTERRUPTED

    help_errors = io.StringIO()
    help_exit = cli.run_cli(
        ["--help"],
        stdin=io.BytesIO(),
        stdout=_FailingWriter(BrokenPipeError),
        stderr=help_errors,
    )
    assert help_exit == cli.EXIT_OUTPUT
    assert cast(dict[str, object], decoded_error(help_errors.getvalue())["error"])["code"] == (
        "OUTPUT_UNAVAILABLE"
    )

    help_interrupt_errors = io.StringIO()
    help_interrupt_exit = cli.run_cli(
        ["--help"],
        stdin=io.BytesIO(),
        stdout=_FailingWriter(KeyboardInterrupt),
        stderr=help_interrupt_errors,
    )
    assert help_interrupt_exit == cli.EXIT_INTERRUPTED
    assert (
        cast(
            dict[str, object],
            decoded_error(help_interrupt_errors.getvalue())["error"],
        )["code"]
        == "INTERRUPTED"
    )


def test_closed_output_streams_return_stable_exit_seventy_four(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    closed_stdout = io.StringIO()
    closed_stdout.close()
    result_errors = io.StringIO()
    result_exit = cli.run_cli(
        [
            *common_arguments(data_directory),
            "get",
            "--note-id",
            "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
        stdin=io.BytesIO(),
        stdout=closed_stdout,
        stderr=result_errors,
    )
    assert result_exit == cli.EXIT_OUTPUT
    assert (
        cast(dict[str, object], decoded_error(result_errors.getvalue())["error"])["code"]
        == "OUTPUT_UNAVAILABLE"
    )

    closed_stderr = io.StringIO()
    closed_stderr.close()
    usage_exit = cli.run_cli(
        [],
        stdin=io.BytesIO(),
        stdout=io.StringIO(),
        stderr=closed_stderr,
    )
    assert usage_exit == cli.EXIT_OUTPUT

    closed_help = io.StringIO()
    closed_help.close()
    help_errors = io.StringIO()
    help_exit = cli.run_cli(
        ["--help"],
        stdin=io.BytesIO(),
        stdout=closed_help,
        stderr=help_errors,
    )
    assert help_exit == cli.EXIT_OUTPUT
    assert cast(dict[str, object], decoded_error(help_errors.getvalue())["error"])["code"] == (
        "OUTPUT_UNAVAILABLE"
    )


def test_unexpected_event_envelope_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = LedgerEvent.create(
        tenant_id=TenantId(TENANT_A),
        note_id=NoteId("nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        command_id=CommandId(CREATE),
        recorded_at_us=1,
        content=NoteContent("title", "body"),
    )
    monkeypatch.setattr(json, "loads", lambda _value: [])
    with pytest.raises(cli._UnexpectedEnvelopeError):
        cli._event_object(event)


def test_main_supports_injected_text_stdin_and_neutralizes_terminal_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_directory = secure_directory(tmp_path)
    output = io.StringIO()
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    exit_code = cli.main(
        [
            *common_arguments(data_directory),
            "get",
            "--note-id",
            "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ]
    )
    assert exit_code == cli.EXIT_SUCCESS
    assert json.loads(output.getvalue())["found"] is False

    monkeypatch.setattr(sys, "argv", ["recall-ledger", "--help"])
    output.seek(0)
    output.truncate()
    assert cli.main() == cli.EXIT_SUCCESS
    assert output.getvalue().startswith("usage: recall-ledger")

    neutralized: list[bool] = []
    monkeypatch.setattr(cli, "run_cli", lambda *_args, **_kwargs: cli.EXIT_OUTPUT)
    monkeypatch.setattr(cli, "_neutralize_standard_streams", lambda: neutralized.append(True))
    assert cli.main([]) == cli.EXIT_OUTPUT
    monkeypatch.setattr(cli, "run_cli", lambda *_args, **_kwargs: cli.EXIT_INTERRUPTED)
    assert cli.main([]) == cli.EXIT_INTERRUPTED
    assert neutralized == [True, True]


def test_neutralize_standard_streams_covers_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(fileno=lambda: 9))
    monkeypatch.setattr(sys, "stderr", SimpleNamespace(fileno=lambda: 8))
    monkeypatch.setattr(cli, "_open_null_descriptor", lambda: 10)
    monkeypatch.setattr(cli, "_replace_descriptor", lambda *args: calls.append(args))
    monkeypatch.setattr(
        cli,
        "_close_descriptor_quietly",
        lambda descriptor: calls.append(("close", descriptor)),
    )
    cli._neutralize_standard_streams()
    assert set(calls[:2]) == {(10, 8), (10, 9)}
    assert calls[-1] == ("close", 10)

    def failed_dup(*_args: object) -> None:
        raise OSError

    calls.clear()
    monkeypatch.setattr(cli, "_replace_descriptor", failed_dup)
    cli._neutralize_standard_streams()
    assert calls == [("close", 10)]

    monkeypatch.setattr(
        sys,
        "stdout",
        SimpleNamespace(fileno=lambda: (_ for _ in ()).throw(ValueError())),
    )
    monkeypatch.setattr(
        sys,
        "stderr",
        SimpleNamespace(fileno=lambda: (_ for _ in ()).throw(OSError())),
    )
    cli._neutralize_standard_streams()

    monkeypatch.setattr(sys, "stdout", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(sys, "stderr", SimpleNamespace(fileno=lambda: 10))
    monkeypatch.setattr(cli, "_open_null_descriptor", lambda: 10)
    calls.clear()
    cli._neutralize_standard_streams()
    assert calls == []

    def failed_null_open() -> int:
        raise OSError

    monkeypatch.setattr(sys, "stdout", SimpleNamespace(fileno=lambda: 9))
    monkeypatch.setattr(sys, "stderr", SimpleNamespace(fileno=lambda: 8))
    monkeypatch.setattr(cli, "_open_null_descriptor", failed_null_open)
    cli._neutralize_standard_streams()


def test_standard_descriptor_helpers_cover_success_and_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = cli._open_null_descriptor()
    cli._close_descriptor_quietly(descriptor)
    cli._close_descriptor_quietly(descriptor)

    duplicates: list[tuple[int, int]] = []

    def record_duplicate(source: int, destination: int) -> None:
        duplicates.append((source, destination))

    with monkeypatch.context() as context:
        context.setattr(os, "dup2", record_duplicate)
        cli._replace_descriptor(7, 8)
    assert duplicates == [(7, 8)]


def test_invalid_storage_request_codes_are_exit_ten(tmp_path: Path) -> None:
    data_directory = secure_directory(tmp_path)
    negative_busy, _, busy_error = run_command(
        data_directory,
        ["get", "--note-id", "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        global_arguments=["--busy-timeout-ms", "-1"],
    )
    assert negative_busy == cli.EXIT_REQUEST
    assert cast(dict[str, object], decoded_error(busy_error)["error"])["code"] == (
        "INVALID_BUSY_TIMEOUT"
    )

    invalid_id, _, id_error = run_command(
        data_directory,
        ["get", "--note-id", "not-a-note-id"],
    )
    assert invalid_id == cli.EXIT_REQUEST
    assert cast(dict[str, object], decoded_error(id_error)["error"])["code"] == (
        "INVALID_IDENTIFIER"
    )

    relative, _, relative_error = run_raw(
        [
            "--data-dir",
            "relative",
            "--tenant-id",
            TENANT_A,
            "get",
            "--note-id",
            "nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ]
    )
    assert relative == cli.EXIT_REQUEST
    assert cast(dict[str, object], decoded_error(relative_error)["error"])["code"] == (
        "INVALID_DATA_DIRECTORY"
    )


def test_installed_wheel_entry_point_and_real_broken_pipe_recovery(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    wheel_directory = tmp_path / "wheelhouse"
    wheel_directory.mkdir()
    build = subprocess.run(  # noqa: S603 - executable and arguments are test-owned
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheel_directory),
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheels = tuple(wheel_directory.glob("recall_ledger-*.whl"))
    assert len(wheels) == 1

    environment = tmp_path / "installed"
    create_environment = subprocess.run(  # noqa: S603 - executable and arguments are test-owned
        [sys.executable, "-m", "venv", str(environment)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert create_environment.returncode == 0, create_environment.stderr
    installed_python = environment / "bin" / "python"
    install = subprocess.run(  # noqa: S603 - executable and arguments are test-owned
        [
            str(installed_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            str(wheels[0]),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    executable = environment / "bin" / "recall-ledger"
    assert executable.is_file()

    data_directory = secure_directory(tmp_path, "installed-data")
    fixture = tmp_path / "installed-content.json"
    fixture.write_bytes(content_bytes(body="x" * 4_096))
    arguments = [
        str(executable),
        *common_arguments(data_directory),
        "create",
        "--command-id",
        CREATE,
        "--content-file",
        str(fixture),
    ]
    producer = subprocess.Popen(  # noqa: S603 - installed entry point and args are test-owned
        arguments,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert producer.stdout is not None
    assert producer.stderr is not None
    producer.stdout.close()
    return_code = producer.wait(timeout=30)
    stderr_bytes = producer.stderr.read()
    assert return_code == cli.EXIT_OUTPUT
    assert b"Exception ignored" not in stderr_bytes
    output_error = json.loads(stderr_bytes)
    assert output_error["error"]["retry"] == "retry_exact_command"

    replay = subprocess.run(  # noqa: S603 - installed entry point and args are test-owned
        arguments,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert replay.returncode == cli.EXIT_SUCCESS
    assert replay.stderr == ""
    assert json.loads(replay.stdout)["replayed"] is True

    query = tmp_path / "installed-query.txt"
    query.write_text("operator fixture", encoding="utf-8")
    installed_search = subprocess.run(  # noqa: S603 - installed entry point is test-owned
        [
            str(executable),
            *common_arguments(data_directory),
            "search",
            "--query-file",
            str(query),
            "--limit",
            "1",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed_search.returncode == cli.EXIT_SUCCESS
    assert installed_search.stderr == ""
    installed_result = cast(dict[str, object], json.loads(installed_search.stdout))
    assert installed_result["operation"] == "search"
    assert installed_result["total_matches"] == 1
    installed_hit = cast(list[dict[str, object]], installed_result["hits"])[0]
    assert (
        cast(dict[str, object], installed_hit["citation"])["note_id"]
        == event_from_result(cast(dict[str, object], json.loads(replay.stdout)))["note_id"]
    )
    assert cast(dict[str, object], installed_hit["score"])["total"] == 48

    invalid = subprocess.Popen(  # noqa: S603 - installed entry point is test-owned
        [str(executable), "--malicious-invalid-option"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert invalid.stdout is not None
    invalid.stdout.close()
    assert invalid.wait(timeout=30) == cli.EXIT_OUTPUT
