#!/usr/bin/env python3
"""Closed contract for RecallLedger installed-wheel CLI evidence.

The capture path validates raw CLI bytes first. Only then may this module
replace the generated note identifier, event digests, microsecond timestamps,
and temporary filesystem paths with explicit presentation tokens.
"""

# The exact scenario uses boolean literals and fixed step indexes as contract values.
# ruff: noqa: FBT003, PLR2004

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, NoReturn, TypeAlias, cast

JsonScalar: TypeAlias = str | int | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

SCHEMA_VERSION: Final = 1
ARTIFACT_NAME: Final = "installed-wheel-cli-workflow"
NORMALIZATION_DISCLOSURE: Final = (
    "Captured from an installed wheel; only run-specific note ID, event digests, "
    "microsecond timestamps, and temporary paths are normalized."
)

TENANT_ID: Final = "tn_11111111111111111111111111111111"
CREATE_COMMAND_ID: Final = "cmd_00000000000000000000000000000001"
REVISE_COMMAND_ID: Final = "cmd_00000000000000000000000000000002"
STALE_COMMAND_ID: Final = "cmd_00000000000000000000000000000003"
TOMBSTONE_COMMAND_ID: Final = "cmd_00000000000000000000000000000004"

NOTE_TOKEN: Final = "<NOTE_ID>"  # noqa: S105 - explicit normalization marker
DATA_DIRECTORY_TOKEN: Final = "<DATA_DIR>"  # noqa: S105 - explicit normalization marker
VENV_EXECUTABLE_TOKEN: Final = "recall-ledger"  # noqa: S105 - executable label
HASH_TOKENS: Final = (
    "<EVENT_HASH_R1>",
    "<EVENT_HASH_R2>",
    "<EVENT_HASH_R3>",
)
TIME_TOKENS: Final = (
    "<RECORDED_AT_US_R1>",
    "<RECORDED_AT_US_R2>",
    "<RECORDED_AT_US_R3>",
)

MAX_CHANNEL_BYTES: Final = 262_144
MAX_JSON_DEPTH: Final = 16
MAX_JSON_ITEMS: Final = 256
MAX_JSON_STRING: Final = 131_072
MAX_INTEGER_DIGITS: Final = 19
MAX_JSONL_LINES: Final = 16
MAX_RECORDED_AT_US: Final = 253_402_300_799_999_999
MAX_TITLE_CODEPOINTS: Final = 240
MAX_TITLE_BYTES: Final = 720
MAX_BODY_CODEPOINTS: Final = 32_768
MAX_BODY_BYTES: Final = 98_304
MAX_TAGS: Final = 16
MAX_TAG_CODEPOINTS: Final = 64
MAX_TAG_BYTES: Final = 192
EVENT_HASH_DOMAIN: Final = b"recall-ledger:event:v1\x00"

STEP_IDS: Final = (
    "create",
    "exact-replay",
    "revise",
    "stale-revise",
    "head-after-conflict",
    "history-page-1",
    "history-page-2-jsonl",
    "tombstone",
    "get-after-tombstone",
    "final-head",
)
STEP_PURPOSES: Final = (
    "Create revision 1 from synthetic content.",
    "Replay the exact create command without appending another event.",
    "Append revision 2 with optimistic concurrency.",
    "Reject a fresh command carrying stale expected revision 1.",
    "Prove the rejected stale command did not change the head.",
    "Read the first bounded history page.",
    "Continue history with the JSONL representation.",
    "Append a content-free logical tombstone at revision 3.",
    "Show that the live-read surface hides the tombstoned note.",
    "Show that the audit head retains the terminal tombstone.",
)
STEP_EXITS: Final = (0, 0, 0, 11, 0, 0, 0, 0, 0, 0)
STEP_FIXTURES: Final = (
    "content-v1",
    "content-v1",
    "content-v2",
    "content-stale",
    None,
    None,
    None,
    None,
    None,
    None,
)
FIXTURE_FILES: Final = {
    "content-v1": "docs/visuals/fixtures/cli-content-v1.json",
    "content-v2": "docs/visuals/fixtures/cli-content-v2.json",
    "content-stale": "docs/visuals/fixtures/cli-content-stale.json",
}

EVENT_KEYS: Final = frozenset(
    {
        "command_id",
        "content",
        "event_hash",
        "kind",
        "note_id",
        "previous_event_hash",
        "recorded_at_us",
        "revision",
        "schema_version",
        "tenant_id",
        "tombstone_reason",
    }
)
MUTATION_KEYS: Final = frozenset({"event", "ok", "operation", "replayed"})
READ_KEYS: Final = frozenset({"event", "found", "ok", "operation"})
HISTORY_KEYS: Final = frozenset(
    {
        "after_revision",
        "events",
        "found",
        "limit",
        "next_after_revision",
        "note_id",
        "ok",
        "operation",
        "tenant_id",
    }
)
HISTORY_EVENT_KEYS: Final = frozenset({"event", "record"})
HISTORY_PAGE_KEYS: Final = frozenset(
    {
        "after_revision",
        "event_count",
        "found",
        "limit",
        "next_after_revision",
        "note_id",
        "record",
        "tenant_id",
    }
)
ERROR_KEYS: Final = frozenset({"error", "ok"})
ERROR_DETAIL_KEYS: Final = frozenset({"code", "message", "retry"})

_TENANT_PATTERN: Final = re.compile(r"tn_[0-9a-f]{32}\Z")
_NOTE_PATTERN: Final = re.compile(r"nt_[0-9a-f]{32}\Z")
_NOTE_SEARCH_PATTERN: Final = re.compile(r"nt_[0-9a-f]{32}")
_COMMAND_PATTERN: Final = re.compile(r"cmd_[0-9a-f]{32}\Z")
_EVENT_HASH_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_OID_PATTERN: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_VERSION_PATTERN: Final = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[a-z0-9.+-]*)?\Z")
_CONTROL_PATTERN: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class EvidenceContractError(ValueError):
    """Evidence bytes or structure violated the closed contract."""


@dataclass(frozen=True, slots=True)
class Invocation:
    """One raw installed console-script invocation."""

    identifier: str
    argv: tuple[str, ...]
    input_fixture: str | None
    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class CaptureContext:
    """Exact runtime paths the orchestrator intended each invocation to use."""

    executable: str
    data_directory: str
    fixture_paths: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ParsedInvocation:
    """One invocation after strict channel decoding."""

    invocation: Invocation
    stdout_records: tuple[JsonObject, ...]
    stderr_records: tuple[JsonObject, ...]


@dataclass(frozen=True, slots=True)
class ValidatedScenario:
    """Raw scenario after all independent and cross-step assertions."""

    invocations: tuple[ParsedInvocation, ...]
    events: tuple[JsonObject, JsonObject, JsonObject]
    note_id: str
    event_hashes: tuple[str, str, str]
    recorded_at_us: tuple[int, int, int]


def _fail(message: str) -> NoReturn:
    raise EvidenceContractError(message)


def _reject_number(_value: str) -> NoReturn:
    _fail("evidence JSON permits bounded integers only")


def _parse_integer(value: str) -> int:
    digits = value.removeprefix("-")
    if not digits or len(digits) > MAX_INTEGER_DIGITS:
        _fail("evidence JSON integer is outside the lexical bound")
    return int(value)


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            _fail("evidence JSON contains a duplicate object key")
        result[key] = value
    return result


def _validate_json_value(value: JsonValue, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("evidence JSON nesting exceeds the supported depth")
    if type(value) is str:
        text = value
        if len(text) > MAX_JSON_STRING or _CONTROL_PATTERN.search(text) is not None:
            _fail("evidence JSON text is outside the supported bounds")
        return
    if type(value) is int:
        if len(str(abs(value))) > MAX_INTEGER_DIGITS:
            _fail("evidence JSON integer is outside the supported bound")
        return
    if value is None or type(value) is bool:
        return
    if type(value) is list:
        items = value
        if len(items) > MAX_JSON_ITEMS:
            _fail("evidence JSON array has too many items")
        for item in items:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        obj = value
        if len(obj) > MAX_JSON_ITEMS:
            _fail("evidence JSON object has too many keys")
        for key, item in obj.items():
            _validate_json_value(key, depth=depth + 1)
            _validate_json_value(item, depth=depth + 1)
        return
    _fail("evidence JSON contains an unsupported value")


def canonical_json_bytes(value: JsonValue, *, newline: bool = True) -> bytes:
    """Serialize one bounded value using the project evidence convention."""

    _validate_json_value(value)
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return payload + (b"\n" if newline else b"")


def decode_canonical_document(raw: bytes, *, context: str) -> JsonObject:
    """Decode one compact canonical JSON document with one trailing newline."""

    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_CHANNEL_BYTES:
        _fail(f"{context} byte length is outside the supported range")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        _fail(f"{context} must end in exactly one newline")
    try:
        text = raw.decode("utf-8", errors="strict")
        parsed = cast(
            JsonValue,
            json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_int=_parse_integer,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            ),
        )
    except EvidenceContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail(f"{context} is not strict bounded UTF-8 JSON")
    _validate_json_value(parsed)
    if type(parsed) is not dict:
        _fail(f"{context} must contain one JSON object")
    obj = parsed
    if canonical_json_bytes(obj) != raw:
        _fail(f"{context} is not compact canonical JSON")
    return obj


def decode_canonical_jsonl(raw: bytes, *, context: str) -> tuple[JsonObject, ...]:
    """Decode a bounded canonical JSONL channel."""

    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_CHANNEL_BYTES:
        _fail(f"{context} byte length is outside the supported range")
    lines = raw.splitlines(keepends=True)
    if not 1 <= len(lines) <= MAX_JSONL_LINES or any(not line.endswith(b"\n") for line in lines):
        _fail(f"{context} is not a bounded newline-terminated JSONL stream")
    return tuple(
        decode_canonical_document(line, context=f"{context} line {index}")
        for index, line in enumerate(lines, start=1)
    )


def load_fixture(path: Path) -> JsonObject:
    """Load one canonical synthetic content fixture."""

    try:
        raw = path.read_bytes()
    except OSError:
        _fail("CLI evidence fixture is unavailable")
    content = decode_canonical_document(raw, context=f"fixture {path.name}")
    return _validate_content(content, "content fixture")


def fixture_digest(content: JsonObject) -> str:
    """Return the canonical fixture SHA-256 without exposing host paths."""

    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def _validate_bounded_text(
    value: JsonValue,
    *,
    context: str,
    maximum_codepoints: int,
    maximum_bytes: int,
    allow_blank: bool,
) -> str:
    text = _require_string(value, context) if not allow_blank else value
    if type(text) is not str:
        _fail(f"{context} must be exact text")
    if not allow_blank and not text.strip():
        _fail(f"{context} cannot be blank")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        _fail(f"{context} must contain Unicode scalar values")
    if len(text) > maximum_codepoints or len(text.encode()) > maximum_bytes:
        _fail(f"{context} exceeds the content contract")
    return text


def _validate_content(value: JsonValue, context: str) -> JsonObject:
    content = _require_object(value, context)
    _require_keys(content, frozenset({"body", "tags", "title"}), context)
    _validate_bounded_text(
        content["title"],
        context=f"{context} title",
        maximum_codepoints=MAX_TITLE_CODEPOINTS,
        maximum_bytes=MAX_TITLE_BYTES,
        allow_blank=False,
    )
    _validate_bounded_text(
        content["body"],
        context=f"{context} body",
        maximum_codepoints=MAX_BODY_CODEPOINTS,
        maximum_bytes=MAX_BODY_BYTES,
        allow_blank=True,
    )
    tags = _require_array(content["tags"], f"{context} tags", maximum=MAX_TAGS)
    seen: set[str] = set()
    for value_tag in tags:
        tag = _validate_bounded_text(
            value_tag,
            context=f"{context} tag",
            maximum_codepoints=MAX_TAG_CODEPOINTS,
            maximum_bytes=MAX_TAG_BYTES,
            allow_blank=False,
        )
        if tag in seen:
            _fail(f"{context} contains a duplicate tag")
        seen.add(tag)
    return content


def _require_keys(value: JsonObject, expected: frozenset[str], context: str) -> None:
    if frozenset(value) != expected:
        _fail(f"{context} has unknown or missing keys")


def _require_object(value: JsonValue, context: str) -> JsonObject:
    if type(value) is not dict:
        _fail(f"{context} must be an object")
    return value


def _require_array(
    value: JsonValue,
    context: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_JSON_ITEMS,
) -> list[JsonValue]:
    if type(value) is not list:
        _fail(f"{context} must be an array")
    result = value
    if not minimum <= len(result) <= maximum:
        _fail(f"{context} has an unsupported item count")
    return result


def _require_string(value: JsonValue, context: str) -> str:
    if type(value) is not str or not value:
        _fail(f"{context} must be non-empty text")
    return value


def _require_int(value: JsonValue, context: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail(f"{context} must be a bounded integer")
    return value


def _require_bool(value: JsonValue, context: str) -> bool:
    if type(value) is not bool:
        _fail(f"{context} must be a boolean")
    return value


def _require_exact(value: JsonValue, expected: JsonValue, context: str) -> None:
    if type(value) is not type(expected) or value != expected:
        _fail(f"{context} does not match the evidence scenario")


def _event_digest(event: JsonObject) -> str:
    material = dict(event)
    material.pop("event_hash", None)
    _validate_json_value(material)
    payload = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(EVENT_HASH_DOMAIN + payload).hexdigest()}"


def _validate_raw_event(  # noqa: PLR0913 - event expectations stay explicit
    value: JsonValue,
    *,
    revision: int,
    kind: str,
    command_id: str,
    content: JsonObject | None,
    previous_hash: str | None,
    note_id: str | None = None,
) -> JsonObject:
    event = _require_object(value, f"revision {revision} event")
    _require_keys(event, EVENT_KEYS, f"revision {revision} event")
    _require_exact(event["schema_version"], 1, "event schema version")
    _require_exact(event["revision"], revision, "event revision")
    _require_exact(event["kind"], kind, "event kind")
    _require_exact(event["tenant_id"], TENANT_ID, "event tenant")
    _require_exact(event["command_id"], command_id, "event command")
    _require_exact(event["content"], content, "event content")
    _require_exact(
        event["tombstone_reason"],
        "user_request" if revision == 3 else None,
        "event tombstone reason",
    )
    _require_exact(event["previous_event_hash"], previous_hash, "event predecessor")

    actual_note_id = _require_string(event["note_id"], "event note identifier")
    if _NOTE_PATTERN.fullmatch(actual_note_id) is None:
        _fail("event note identifier is not canonical")
    if note_id is not None and actual_note_id != note_id:
        _fail("event note identifier changed across the workflow")

    actual_command_id = _require_string(event["command_id"], "event command identifier")
    if _COMMAND_PATTERN.fullmatch(actual_command_id) is None:
        _fail("event command identifier is not canonical")
    timestamp = _require_int(event["recorded_at_us"], "event timestamp")
    if timestamp > MAX_RECORDED_AT_US:
        _fail("event timestamp exceeds the supported UTC range")
    digest = _require_string(event["event_hash"], "event digest")
    if _EVENT_HASH_PATTERN.fullmatch(digest) is None:
        _fail("event digest is not canonical SHA-256 text")
    if _event_digest(event) != digest:
        _fail("event digest does not match its canonical material")
    return event


def _parse_invocation(invocation: Invocation, *, jsonl: bool) -> ParsedInvocation:
    if invocation.identifier not in STEP_IDS:
        _fail("raw invocation has an unsupported identifier")
    if type(invocation.exit_code) is not int:
        _fail("raw invocation exit code must be an integer")
    if invocation.stdout:
        stdout = (
            decode_canonical_jsonl(
                invocation.stdout,
                context=f"{invocation.identifier} stdout",
            )
            if jsonl
            else (
                decode_canonical_document(
                    invocation.stdout,
                    context=f"{invocation.identifier} stdout",
                ),
            )
        )
    else:
        stdout = ()
    stderr = (
        (
            decode_canonical_document(
                invocation.stderr,
                context=f"{invocation.identifier} stderr",
            ),
        )
        if invocation.stderr
        else ()
    )
    return ParsedInvocation(
        invocation=invocation,
        stdout_records=stdout,
        stderr_records=stderr,
    )


def _absolute_posix_path(value: str, context: str) -> PurePosixPath:
    if "\x00" in value:
        _fail(f"{context} contains a null byte")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        _fail(f"{context} must be one canonical absolute POSIX path")
    return path


def _validate_capture_context(context: CaptureContext) -> dict[str, str]:
    executable = _absolute_posix_path(context.executable, "capture executable")
    if executable.parts[-2:] != ("bin", "recall-ledger"):
        _fail("capture executable is not the installed recall-ledger console script")
    _absolute_posix_path(context.data_directory, "capture data directory")
    fixture_paths = dict(context.fixture_paths)
    if len(fixture_paths) != len(context.fixture_paths) or set(fixture_paths) != set(FIXTURE_FILES):
        _fail("capture context fixture paths are missing, duplicated, or unknown")
    fixture_parents: set[PurePosixPath] = set()
    for fixture_id, value in fixture_paths.items():
        path = _absolute_posix_path(value, f"{fixture_id} capture fixture")
        if path.name != Path(FIXTURE_FILES[fixture_id]).name:
            _fail("capture fixture filename does not match its committed source")
        fixture_parents.add(path.parent)
    if len(fixture_parents) != 1:
        _fail("capture fixture paths must share one isolated directory")
    return fixture_paths


def _validate_raw_argv(
    invocations: tuple[Invocation, ...],
    *,
    context: CaptureContext,
    note_id: str,
) -> None:
    fixture_paths = _validate_capture_context(context)
    commands = (
        (
            "create",
            "--command-id",
            CREATE_COMMAND_ID,
            "--content-file",
            fixture_paths["content-v1"],
        ),
        (
            "create",
            "--command-id",
            CREATE_COMMAND_ID,
            "--content-file",
            fixture_paths["content-v1"],
        ),
        (
            "revise",
            "--note-id",
            note_id,
            "--command-id",
            REVISE_COMMAND_ID,
            "--expected-revision",
            "1",
            "--content-file",
            fixture_paths["content-v2"],
        ),
        (
            "revise",
            "--note-id",
            note_id,
            "--command-id",
            STALE_COMMAND_ID,
            "--expected-revision",
            "1",
            "--content-file",
            fixture_paths["content-stale"],
        ),
        ("head", "--note-id", note_id),
        (
            "history",
            "--note-id",
            note_id,
            "--after-revision",
            "0",
            "--limit",
            "1",
        ),
        (
            "history",
            "--note-id",
            note_id,
            "--after-revision",
            "1",
            "--limit",
            "1",
            "--jsonl",
        ),
        (
            "tombstone",
            "--note-id",
            note_id,
            "--command-id",
            TOMBSTONE_COMMAND_ID,
            "--expected-revision",
            "2",
            "--reason",
            "user_request",
        ),
        ("get", "--note-id", note_id),
        ("head", "--note-id", note_id),
    )
    prefix = (
        context.executable,
        "--data-dir",
        context.data_directory,
        "--tenant-id",
        TENANT_ID,
    )
    for index, invocation in enumerate(invocations):
        if invocation.argv != (*prefix, *commands[index]):
            _fail(f"{invocation.identifier} raw argv differs from the intended invocation")


def validate_raw_scenario(  # noqa: PLR0912, PLR0915 - auditable ten-step proof
    invocations: tuple[Invocation, ...],
    fixtures: dict[str, JsonObject],
    context: CaptureContext,
) -> ValidatedScenario:
    """Validate all raw bytes and the complete ten-step state transition."""

    if len(invocations) != len(STEP_IDS):
        _fail("raw capture must contain exactly ten invocations")
    if tuple(invocation.identifier for invocation in invocations) != STEP_IDS:
        _fail("raw capture steps are missing, duplicated, or reordered")
    if set(fixtures) != set(FIXTURE_FILES):
        _fail("raw capture fixture set is not closed")
    for fixture_id, fixture in fixtures.items():
        _validate_content(fixture, f"{fixture_id} raw fixture")

    parsed = tuple(
        _parse_invocation(invocation, jsonl=index == 6)
        for index, invocation in enumerate(invocations)
    )
    for index, step in enumerate(parsed):
        if step.invocation.exit_code != STEP_EXITS[index]:
            _fail(f"{step.invocation.identifier} returned an unexpected exit code")
        if step.invocation.input_fixture != STEP_FIXTURES[index]:
            _fail(f"{step.invocation.identifier} used an unexpected fixture")
        if index == 3:
            if step.stdout_records or len(step.stderr_records) != 1:
                _fail("stale revision must emit one stderr object and no stdout")
        elif len(step.stdout_records) < 1 or step.stderr_records:
            _fail(f"{step.invocation.identifier} violated stdout/stderr separation")

    create = parsed[0].stdout_records[0]
    _require_keys(create, MUTATION_KEYS, "create result")
    _require_exact(create["ok"], True, "create ok")
    _require_exact(create["operation"], "create", "create operation")
    _require_exact(create["replayed"], False, "create replay flag")
    event_one = _validate_raw_event(
        create["event"],
        revision=1,
        kind="note.created",
        command_id=CREATE_COMMAND_ID,
        content=fixtures["content-v1"],
        previous_hash=None,
    )
    note_id = cast(str, event_one["note_id"])
    hash_one = cast(str, event_one["event_hash"])
    _validate_raw_argv(invocations, context=context, note_id=note_id)

    replay = parsed[1].stdout_records[0]
    _require_keys(replay, MUTATION_KEYS, "exact replay result")
    _require_exact(replay["ok"], True, "exact replay ok")
    _require_exact(replay["operation"], "create", "exact replay operation")
    _require_exact(replay["replayed"], True, "exact replay flag")
    _require_exact(replay["event"], event_one, "exact replay event")

    revise = parsed[2].stdout_records[0]
    _require_keys(revise, MUTATION_KEYS, "revise result")
    _require_exact(revise["ok"], True, "revise ok")
    _require_exact(revise["operation"], "revise", "revise operation")
    _require_exact(revise["replayed"], False, "revise replay flag")
    event_two = _validate_raw_event(
        revise["event"],
        revision=2,
        kind="note.revised",
        command_id=REVISE_COMMAND_ID,
        content=fixtures["content-v2"],
        previous_hash=hash_one,
        note_id=note_id,
    )
    hash_two = cast(str, event_two["event_hash"])
    if cast(int, event_two["recorded_at_us"]) < cast(int, event_one["recorded_at_us"]):
        _fail("revision 2 timestamp precedes revision 1")

    stale = parsed[3].stderr_records[0]
    _require_keys(stale, ERROR_KEYS, "stale revision error")
    _require_exact(stale["ok"], False, "stale revision ok")
    error = _require_object(stale["error"], "stale revision error detail")
    _require_keys(error, ERROR_DETAIL_KEYS, "stale revision error detail")
    _require_exact(error["code"], "REVISION_CONFLICT", "stale revision code")
    _require_exact(
        error["message"],
        "the note head no longer matches the expected revision",
        "stale revision message",
    )
    _require_exact(
        error["retry"],
        "inspect_head_then_new_command",
        "stale revision retry guidance",
    )

    head = parsed[4].stdout_records[0]
    _require_keys(head, READ_KEYS, "head after conflict")
    _require_exact(head["ok"], True, "head after conflict ok")
    _require_exact(head["found"], True, "head after conflict found")
    _require_exact(head["operation"], "head", "head after conflict operation")
    _require_exact(head["event"], event_two, "head after conflict event")

    history_one = parsed[5].stdout_records[0]
    _require_keys(history_one, HISTORY_KEYS, "history page 1")
    _require_exact(history_one["after_revision"], 0, "history page 1 cursor")
    _require_exact(history_one["events"], [event_one], "history page 1 events")
    _require_exact(history_one["found"], True, "history page 1 found")
    _require_exact(history_one["limit"], 1, "history page 1 limit")
    _require_exact(history_one["next_after_revision"], 1, "history page 1 next cursor")
    _require_exact(history_one["note_id"], note_id, "history page 1 note")
    _require_exact(history_one["ok"], True, "history page 1 ok")
    _require_exact(history_one["operation"], "history", "history page 1 operation")
    _require_exact(history_one["tenant_id"], TENANT_ID, "history page 1 tenant")

    history_two_records = parsed[6].stdout_records
    if len(history_two_records) != 2:
        _fail("history page 2 JSONL must contain one event and one page record")
    history_event, history_page = history_two_records
    _require_keys(history_event, HISTORY_EVENT_KEYS, "history page 2 event record")
    _require_exact(history_event["record"], "event", "history page 2 event record kind")
    _require_exact(history_event["event"], event_two, "history page 2 event")
    _require_keys(history_page, HISTORY_PAGE_KEYS, "history page 2 page record")
    for key, expected in {
        "after_revision": 1,
        "event_count": 1,
        "found": True,
        "limit": 1,
        "next_after_revision": None,
        "note_id": note_id,
        "record": "page",
        "tenant_id": TENANT_ID,
    }.items():
        _require_exact(history_page[key], expected, f"history page 2 {key}")

    tombstone = parsed[7].stdout_records[0]
    _require_keys(tombstone, MUTATION_KEYS, "tombstone result")
    _require_exact(tombstone["ok"], True, "tombstone ok")
    _require_exact(tombstone["operation"], "tombstone", "tombstone operation")
    _require_exact(tombstone["replayed"], False, "tombstone replay flag")
    event_three = _validate_raw_event(
        tombstone["event"],
        revision=3,
        kind="note.tombstoned",
        command_id=TOMBSTONE_COMMAND_ID,
        content=None,
        previous_hash=hash_two,
        note_id=note_id,
    )
    hash_three = cast(str, event_three["event_hash"])
    if cast(int, event_three["recorded_at_us"]) < cast(int, event_two["recorded_at_us"]):
        _fail("revision 3 timestamp precedes revision 2")

    live_read = parsed[8].stdout_records[0]
    _require_keys(live_read, READ_KEYS, "get after tombstone")
    _require_exact(
        live_read,
        {"event": None, "found": False, "ok": True, "operation": "get"},
        "get after tombstone result",
    )

    final_head = parsed[9].stdout_records[0]
    _require_keys(final_head, READ_KEYS, "final head")
    _require_exact(final_head["ok"], True, "final head ok")
    _require_exact(final_head["found"], True, "final head found")
    _require_exact(final_head["operation"], "head", "final head operation")
    _require_exact(final_head["event"], event_three, "final head event")

    serialized_events = canonical_json_bytes([event_one, event_two, event_three])
    if STALE_COMMAND_ID.encode("ascii") in serialized_events:
        _fail("the rejected stale command appeared in committed history")

    return ValidatedScenario(
        invocations=parsed,
        events=(event_one, event_two, event_three),
        note_id=note_id,
        event_hashes=(hash_one, hash_two, hash_three),
        recorded_at_us=(
            cast(int, event_one["recorded_at_us"]),
            cast(int, event_two["recorded_at_us"]),
            cast(int, event_three["recorded_at_us"]),
        ),
    )


def _replace_runtime_values(
    value: JsonValue,
    *,
    note_id: str,
    hashes: tuple[str, str, str],
    timestamps: tuple[int, int, int],
) -> JsonValue:
    if type(value) is dict and frozenset(value) == EVENT_KEYS:
        event = dict(value)
        revision_value = event["revision"]
        if type(revision_value) is not int or not 1 <= revision_value <= 3:
            _fail("validated event lost its bounded revision before normalization")
        index = revision_value - 1
        if (
            event["note_id"] != note_id
            or event["event_hash"] != hashes[index]
            or event["recorded_at_us"] != timestamps[index]
        ):
            _fail("validated event values changed before normalization")
        expected_previous: JsonValue = None if index == 0 else hashes[index - 1]
        if event["previous_event_hash"] != expected_previous:
            _fail("validated event predecessor changed before normalization")
        event["note_id"] = NOTE_TOKEN
        event["event_hash"] = HASH_TOKENS[index]
        event["recorded_at_us"] = TIME_TOKENS[index]
        event["previous_event_hash"] = None if index == 0 else HASH_TOKENS[index - 1]
        return event
    if type(value) is list:
        return [
            _replace_runtime_values(
                item,
                note_id=note_id,
                hashes=hashes,
                timestamps=timestamps,
            )
            for item in value
        ]
    if type(value) is dict:
        return {
            key: (
                NOTE_TOKEN
                if key == "note_id" and item == note_id
                else _replace_runtime_values(
                    item,
                    note_id=note_id,
                    hashes=hashes,
                    timestamps=timestamps,
                )
            )
            for key, item in value.items()
        }
    return value


def _normalized_argv(
    invocation: Invocation,
    *,
    note_id: str,
) -> list[JsonValue]:
    if not invocation.argv:
        _fail("raw invocation argv is empty")
    result: list[JsonValue] = []
    index = 0
    while index < len(invocation.argv):
        value = invocation.argv[index]
        if index == 0:
            result.append(VENV_EXECUTABLE_TOKEN)
        elif value == "--data-dir":
            if index + 1 >= len(invocation.argv):
                _fail("raw invocation has an incomplete data directory option")
            result.extend((value, DATA_DIRECTORY_TOKEN))
            index += 1
        elif value == "--content-file":
            if index + 1 >= len(invocation.argv) or invocation.input_fixture is None:
                _fail("raw invocation has an invalid content fixture option")
            result.extend((value, f"<FIXTURE:{invocation.input_fixture}>"))
            index += 1
        elif value == note_id:
            result.append(NOTE_TOKEN)
        else:
            result.append(value)
        index += 1
    return result


def build_evidence_document(
    scenario: ValidatedScenario,
    *,
    provenance: JsonObject,
    fixtures: dict[str, JsonObject],
) -> JsonObject:
    """Normalize a fully validated raw scenario into the committed document."""

    steps: list[JsonValue] = []
    for index, parsed in enumerate(scenario.invocations):
        steps.append(
            {
                "argv": _normalized_argv(
                    parsed.invocation,
                    note_id=scenario.note_id,
                ),
                "exit_code": parsed.invocation.exit_code,
                "id": parsed.invocation.identifier,
                "input_fixture": parsed.invocation.input_fixture,
                "number": index + 1,
                "purpose": STEP_PURPOSES[index],
                "stderr": cast(
                    list[JsonValue],
                    _replace_runtime_values(
                        list(parsed.stderr_records),
                        note_id=scenario.note_id,
                        hashes=scenario.event_hashes,
                        timestamps=scenario.recorded_at_us,
                    ),
                ),
                "stdout": cast(
                    list[JsonValue],
                    _replace_runtime_values(
                        list(parsed.stdout_records),
                        note_id=scenario.note_id,
                        hashes=scenario.event_hashes,
                        timestamps=scenario.recorded_at_us,
                    ),
                ),
                "stdout_format": "jsonl" if index == 6 else "json",
            }
        )

    fixture_records: list[JsonValue] = []
    for fixture_id, path in FIXTURE_FILES.items():
        content = fixtures[fixture_id]
        fixture_records.append(
            {
                "content": content,
                "id": fixture_id,
                "path": path,
                "sha256": fixture_digest(content),
            }
        )

    evidence: JsonObject = {
        "artifact": ARTIFACT_NAME,
        "caption": NORMALIZATION_DISCLOSURE,
        "fixtures": fixture_records,
        "normalization": {
            "rules": [
                {
                    "field": "note_id",
                    "replacement": NOTE_TOKEN,
                    "scope": "generated note identity",
                },
                {
                    "field": "event_hash and previous_event_hash",
                    "replacement": "revision-indexed event hash tokens",
                    "scope": "three run-specific event digests",
                },
                {
                    "field": "recorded_at_us",
                    "replacement": "revision-indexed microsecond tokens",
                    "scope": "three storage-owned timestamps",
                },
                {
                    "field": "argv filesystem paths",
                    "replacement": "data, fixture, and installed executable tokens",
                    "scope": "temporary capture workspace only",
                },
            ]
        },
        "provenance": provenance,
        "schema_version": SCHEMA_VERSION,
        "steps": steps,
        "verification": {
            "assertions": [
                "Raw stdout and stderr bytes were canonical and channel-separated.",
                "Every raw event digest was independently recomputed before normalization.",
                "The installed wheel decoded all three raw canonical event envelopes.",
                "Exact replay returned revision 1 without another event append.",
                "A fresh stale command failed with REVISION_CONFLICT and exit 11.",
                "Two bounded history pages returned only revisions 1 and 2 in order.",
                "The tombstone linked revision 3 to revision 2 without retaining content.",
                "Live get hid the tombstone while final head retained it for audit.",
            ],
            "event_hashes_recomputed": True,
            "installed_wheel_decode": True,
            "raw_outputs_validated_before_normalization": True,
        },
    }
    validate_evidence_document(evidence)
    return evidence


def _validate_relative_path(value: JsonValue, context: str) -> str:
    path = _require_string(value, context)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or path != pure.as_posix():
        _fail(f"{context} must be a normalized repository-relative path")
    return path


def _validate_provenance(value: JsonValue) -> None:  # noqa: PLR0912, PLR0915
    provenance = _require_object(value, "provenance")
    _require_keys(
        provenance,
        frozenset(
            {
                "builder",
                "capture_inputs",
                "installation",
                "source_archive",
                "source_commit",
                "source_date_epoch",
                "source_tree",
                "wheel",
            }
        ),
        "provenance",
    )
    source_commit = _require_string(provenance["source_commit"], "source commit")
    source_tree = _require_string(provenance["source_tree"], "source tree")
    if _OID_PATTERN.fullmatch(source_commit) is None or _OID_PATTERN.fullmatch(source_tree) is None:
        _fail("provenance Git object identifier is not canonical")
    _require_int(provenance["source_date_epoch"], "source date epoch", minimum=1)

    archive = _require_object(provenance["source_archive"], "source archive")
    _require_keys(archive, frozenset({"format", "sha256", "size_bytes"}), "source archive")
    _require_exact(archive["format"], "git-archive-tar", "source archive format")
    if _SHA256_PATTERN.fullmatch(_require_string(archive["sha256"], "archive digest")) is None:
        _fail("source archive digest is not canonical")
    _require_int(archive["size_bytes"], "source archive size", minimum=1)

    builder = _require_object(provenance["builder"], "builder provenance")
    _require_keys(
        builder,
        frozenset({"build_version", "python_version", "setuptools_version"}),
        "builder provenance",
    )
    for key in ("build_version", "python_version", "setuptools_version"):
        version = _require_string(builder[key], f"builder {key}")
        if _VERSION_PATTERN.fullmatch(version) is None:
            _fail(f"builder {key} is not a bounded version")

    inputs = _require_object(provenance["capture_inputs"], "capture inputs")
    _require_keys(inputs, frozenset({"files", "sha256"}), "capture inputs")
    if _SHA256_PATTERN.fullmatch(_require_string(inputs["sha256"], "input digest")) is None:
        _fail("capture input digest is not canonical")
    input_files = _require_array(inputs["files"], "capture input files", minimum=8, maximum=64)
    seen_paths: list[str] = []
    for item in input_files:
        entry = _require_object(item, "capture input file")
        _require_keys(entry, frozenset({"path", "sha256", "size_bytes"}), "capture input file")
        seen_paths.append(_validate_relative_path(entry["path"], "capture input path"))
        if _SHA256_PATTERN.fullmatch(_require_string(entry["sha256"], "input file digest")) is None:
            _fail("capture input file digest is not canonical")
        _require_int(entry["size_bytes"], "capture input file size")
    if seen_paths != sorted(set(seen_paths)):
        _fail("capture input paths must be unique and sorted")

    wheel = _require_object(provenance["wheel"], "wheel provenance")
    _require_keys(
        wheel,
        frozenset({"filename", "record_sha256", "sha256", "size_bytes"}),
        "wheel provenance",
    )
    _require_exact(
        wheel["filename"],
        "recall_ledger-0.1.0-py3-none-any.whl",
        "wheel filename",
    )
    for key in ("record_sha256", "sha256"):
        if _SHA256_PATTERN.fullmatch(_require_string(wheel[key], f"wheel {key}")) is None:
            _fail(f"wheel {key} is not canonical")
    _require_int(wheel["size_bytes"], "wheel size", minimum=1)

    installation = _require_object(provenance["installation"], "installation provenance")
    _require_keys(
        installation,
        frozenset(
            {
                "console_script",
                "console_script_normalized_sha256",
                "distribution",
                "installed_files_sha256",
                "method",
                "module_origin",
                "pip_version",
                "python_version",
                "sqlite_version",
                "version",
            }
        ),
        "installation provenance",
    )
    _require_exact(installation["distribution"], "recall-ledger", "installed distribution")
    _require_exact(installation["version"], "0.1.0", "installed version")
    _require_exact(
        installation["console_script"],
        "recall-ledger = recall_ledger.cli:main",
        "installed console script",
    )
    if (
        _SHA256_PATTERN.fullmatch(
            _require_string(
                installation["console_script_normalized_sha256"],
                "normalized installed console script digest",
            )
        )
        is None
    ):
        _fail("installed console script digest is not canonical")
    _require_exact(
        installation["method"],
        "pip --no-index --no-deps --no-compile",
        "installation method",
    )
    _require_exact(
        installation["module_origin"],
        "installed-venv/recall_ledger/__init__.py",
        "installed module origin",
    )
    for key in ("pip_version", "python_version", "sqlite_version"):
        version = _require_string(installation[key], f"installation {key}")
        if _VERSION_PATTERN.fullmatch(version) is None:
            _fail(f"installation {key} is not a bounded version")
    if (
        _SHA256_PATTERN.fullmatch(
            _require_string(
                installation["installed_files_sha256"],
                "installed file manifest digest",
            )
        )
        is None
    ):
        _fail("installed file manifest digest is not canonical")


def _validate_normalized_event(
    value: JsonValue,
    *,
    revision: int,
    expected_content: JsonObject | None,
) -> JsonObject:
    event = _require_object(value, f"normalized revision {revision} event")
    _require_keys(event, EVENT_KEYS, f"normalized revision {revision} event")
    expected_kind = ("note.created", "note.revised", "note.tombstoned")[revision - 1]
    expected_command = (CREATE_COMMAND_ID, REVISE_COMMAND_ID, TOMBSTONE_COMMAND_ID)[revision - 1]
    expected_previous: JsonValue = None if revision == 1 else HASH_TOKENS[revision - 2]
    for key, expected in {
        "command_id": expected_command,
        "content": expected_content,
        "event_hash": HASH_TOKENS[revision - 1],
        "kind": expected_kind,
        "note_id": NOTE_TOKEN,
        "previous_event_hash": expected_previous,
        "recorded_at_us": TIME_TOKENS[revision - 1],
        "revision": revision,
        "schema_version": 1,
        "tenant_id": TENANT_ID,
        "tombstone_reason": "user_request" if revision == 3 else None,
    }.items():
        _require_exact(event[key], expected, f"normalized revision {revision} {key}")
    return event


def _validate_fixture_records(value: JsonValue) -> dict[str, JsonObject]:
    records = _require_array(value, "evidence fixtures", minimum=3, maximum=3)
    if len(records) != len(FIXTURE_FILES):
        _fail("evidence fixture count is not closed")
    fixtures: dict[str, JsonObject] = {}
    for record_value, (fixture_id, path) in zip(records, FIXTURE_FILES.items(), strict=True):
        record = _require_object(record_value, "evidence fixture")
        _require_keys(record, frozenset({"content", "id", "path", "sha256"}), "evidence fixture")
        _require_exact(record["id"], fixture_id, "evidence fixture identifier")
        _require_exact(record["path"], path, "evidence fixture path")
        content = _validate_content(record["content"], "evidence fixture content")
        _require_exact(record["sha256"], fixture_digest(content), "evidence fixture digest")
        fixtures[fixture_id] = content
    return fixtures


def _validate_normalization(value: JsonValue) -> None:
    normalization = _require_object(value, "normalization")
    _require_keys(normalization, frozenset({"rules"}), "normalization")
    rules = _require_array(normalization["rules"], "normalization rules", minimum=4, maximum=4)
    expected = (
        ("note_id", NOTE_TOKEN, "generated note identity"),
        (
            "event_hash and previous_event_hash",
            "revision-indexed event hash tokens",
            "three run-specific event digests",
        ),
        (
            "recorded_at_us",
            "revision-indexed microsecond tokens",
            "three storage-owned timestamps",
        ),
        (
            "argv filesystem paths",
            "data, fixture, and installed executable tokens",
            "temporary capture workspace only",
        ),
    )
    for value_item, (field, replacement, scope) in zip(rules, expected, strict=True):
        item = _require_object(value_item, "normalization rule")
        _require_keys(item, frozenset({"field", "replacement", "scope"}), "normalization rule")
        _require_exact(item["field"], field, "normalization field")
        _require_exact(item["replacement"], replacement, "normalization replacement")
        _require_exact(item["scope"], scope, "normalization scope")


def _validate_normalized_argv(value: JsonValue, *, step_index: int) -> None:
    items = _require_array(value, "normalized argv", minimum=6, maximum=20)
    if any(type(item) is not str for item in items):
        _fail("normalized argv must contain text only")
    argv = cast(list[str], items)
    if argv[0] != VENV_EXECUTABLE_TOKEN:
        _fail("normalized argv must use the installed console-script name")
    if argv[1:5] != ["--data-dir", DATA_DIRECTORY_TOKEN, "--tenant-id", TENANT_ID]:
        _fail("normalized argv lost the explicit data or tenant context")
    if any(value.startswith("/") or "/home/" in value for value in argv):
        _fail("normalized argv contains an absolute host path")
    expected_fixture = STEP_FIXTURES[step_index]
    fixture_tokens = [value for value in argv if value.startswith("<FIXTURE:")]
    if expected_fixture is None:
        if fixture_tokens:
            _fail("normalized argv contains an unexpected fixture token")
    elif fixture_tokens != [f"<FIXTURE:{expected_fixture}>"]:
        _fail("normalized argv fixture token does not match the step")
    fixture_one = "<FIXTURE:content-v1>"
    fixture_two = "<FIXTURE:content-v2>"
    fixture_stale = "<FIXTURE:content-stale>"
    expected_commands = (
        [
            "create",
            "--command-id",
            CREATE_COMMAND_ID,
            "--content-file",
            fixture_one,
        ],
        [
            "create",
            "--command-id",
            CREATE_COMMAND_ID,
            "--content-file",
            fixture_one,
        ],
        [
            "revise",
            "--note-id",
            NOTE_TOKEN,
            "--command-id",
            REVISE_COMMAND_ID,
            "--expected-revision",
            "1",
            "--content-file",
            fixture_two,
        ],
        [
            "revise",
            "--note-id",
            NOTE_TOKEN,
            "--command-id",
            STALE_COMMAND_ID,
            "--expected-revision",
            "1",
            "--content-file",
            fixture_stale,
        ],
        ["head", "--note-id", NOTE_TOKEN],
        [
            "history",
            "--note-id",
            NOTE_TOKEN,
            "--after-revision",
            "0",
            "--limit",
            "1",
        ],
        [
            "history",
            "--note-id",
            NOTE_TOKEN,
            "--after-revision",
            "1",
            "--limit",
            "1",
            "--jsonl",
        ],
        [
            "tombstone",
            "--note-id",
            NOTE_TOKEN,
            "--command-id",
            TOMBSTONE_COMMAND_ID,
            "--expected-revision",
            "2",
            "--reason",
            "user_request",
        ],
        ["get", "--note-id", NOTE_TOKEN],
        ["head", "--note-id", NOTE_TOKEN],
    )
    if argv[5:] != expected_commands[step_index]:
        _fail("normalized argv does not match the exact ten-step workflow")


def _validate_steps(  # noqa: PLR0912, PLR0915 - closed ten-step state proof
    value: JsonValue,
    fixtures: dict[str, JsonObject],
) -> None:
    steps = _require_array(value, "evidence steps", minimum=10, maximum=10)
    normalized_events: dict[int, JsonObject] = {}
    for index, value_item in enumerate(steps):
        step = _require_object(value_item, "evidence step")
        _require_keys(
            step,
            frozenset(
                {
                    "argv",
                    "exit_code",
                    "id",
                    "input_fixture",
                    "number",
                    "purpose",
                    "stderr",
                    "stdout",
                    "stdout_format",
                }
            ),
            "evidence step",
        )
        _require_exact(step["number"], index + 1, "evidence step number")
        _require_exact(step["id"], STEP_IDS[index], "evidence step identifier")
        _require_exact(step["purpose"], STEP_PURPOSES[index], "evidence step purpose")
        _require_exact(step["exit_code"], STEP_EXITS[index], "evidence step exit")
        _require_exact(step["input_fixture"], STEP_FIXTURES[index], "evidence step fixture")
        _require_exact(
            step["stdout_format"],
            "jsonl" if index == 6 else "json",
            "evidence step output format",
        )
        _validate_normalized_argv(step["argv"], step_index=index)
        stdout = _require_array(step["stdout"], "evidence step stdout", maximum=2)
        stderr = _require_array(step["stderr"], "evidence step stderr", maximum=1)
        if index == 3:
            if stdout or len(stderr) != 1:
                _fail("normalized stale revision channels are invalid")
        elif len(stdout) < 1 or stderr:
            _fail("normalized success channels are invalid")

        if index in {0, 1, 2, 7}:
            result = _require_object(stdout[0], "normalized mutation")
            _require_keys(result, MUTATION_KEYS, "normalized mutation")
            revision = {0: 1, 1: 1, 2: 2, 7: 3}[index]
            expected_fixture = {1: "content-v1", 2: "content-v2"}.get(revision)
            content = None if expected_fixture is None else fixtures[expected_fixture]
            event = _validate_normalized_event(
                result["event"],
                revision=revision,
                expected_content=content,
            )
            normalized_events[revision] = event
            _require_exact(result["ok"], True, "normalized mutation ok")
            _require_exact(
                result["operation"],
                ("create", "create", "revise", "tombstone")[(0, 1, 2, 7).index(index)],
                "normalized mutation operation",
            )
            _require_exact(result["replayed"], index == 1, "normalized replay flag")
        elif index == 3:
            error_result = _require_object(stderr[0], "normalized stale error")
            _require_keys(error_result, ERROR_KEYS, "normalized stale error")
            _require_exact(error_result["ok"], False, "normalized stale ok")
            detail = _require_object(error_result["error"], "normalized stale detail")
            _require_keys(detail, ERROR_DETAIL_KEYS, "normalized stale detail")
            _require_exact(detail["code"], "REVISION_CONFLICT", "normalized stale code")
            _require_exact(
                detail["message"],
                "the note head no longer matches the expected revision",
                "normalized stale message",
            )
            _require_exact(
                detail["retry"],
                "inspect_head_then_new_command",
                "normalized stale retry",
            )
        elif index in {4, 8, 9}:
            result = _require_object(stdout[0], "normalized read")
            _require_keys(result, READ_KEYS, "normalized read")
            expected_operation = "get" if index == 8 else "head"
            _require_exact(result["operation"], expected_operation, "normalized read operation")
            _require_exact(result["ok"], True, "normalized read ok")
            if index == 8:
                _require_exact(result["found"], False, "normalized get found")
                _require_exact(result["event"], None, "normalized get event")
            else:
                revision = 2 if index == 4 else 3
                _require_exact(result["found"], True, "normalized head found")
                _require_exact(
                    result["event"],
                    normalized_events[revision],
                    "normalized head event",
                )
        elif index == 5:
            result = _require_object(stdout[0], "normalized history page 1")
            _require_keys(result, HISTORY_KEYS, "normalized history page 1")
            expected_page_one: dict[str, JsonValue] = {
                "after_revision": 0,
                "events": [normalized_events[1]],
                "found": True,
                "limit": 1,
                "next_after_revision": 1,
                "note_id": NOTE_TOKEN,
                "ok": True,
                "operation": "history",
                "tenant_id": TENANT_ID,
            }
            for key, expected in expected_page_one.items():
                _require_exact(result[key], expected, f"normalized history page 1 {key}")
        elif index == 6:
            if len(stdout) != 2:
                _fail("normalized history JSONL must contain two records")
            event_record = _require_object(stdout[0], "normalized history event record")
            page_record = _require_object(stdout[1], "normalized history page record")
            _require_keys(event_record, HISTORY_EVENT_KEYS, "normalized history event record")
            _require_exact(event_record["record"], "event", "normalized history event kind")
            _require_exact(
                event_record["event"],
                normalized_events[2],
                "normalized history revision 2",
            )
            _require_keys(page_record, HISTORY_PAGE_KEYS, "normalized history page record")
            for key, expected in {
                "after_revision": 1,
                "event_count": 1,
                "found": True,
                "limit": 1,
                "next_after_revision": None,
                "note_id": NOTE_TOKEN,
                "record": "page",
                "tenant_id": TENANT_ID,
            }.items():
                _require_exact(page_record[key], expected, f"normalized history page 2 {key}")

    first_stdout = _require_array(
        _require_object(steps[0], "step 1")["stdout"],
        "step 1 stdout",
    )
    replay_stdout = _require_array(
        _require_object(steps[1], "step 2")["stdout"],
        "step 2 stdout",
    )
    first = _require_object(first_stdout[0], "step 1 result")
    replay = _require_object(replay_stdout[0], "step 2 result")
    _require_exact(replay["event"], first["event"], "normalized exact replay event")


def _validate_verification(value: JsonValue) -> None:
    verification = _require_object(value, "verification")
    _require_keys(
        verification,
        frozenset(
            {
                "assertions",
                "event_hashes_recomputed",
                "installed_wheel_decode",
                "raw_outputs_validated_before_normalization",
            }
        ),
        "verification",
    )
    assertions = _require_array(
        verification["assertions"],
        "verification assertions",
        minimum=8,
        maximum=8,
    )
    if any(type(item) is not str or not item.endswith(".") for item in assertions):
        _fail("verification assertions must be bounded complete sentences")
    for key in (
        "event_hashes_recomputed",
        "installed_wheel_decode",
        "raw_outputs_validated_before_normalization",
    ):
        if not _require_bool(verification[key], f"verification {key}"):
            _fail(f"verification {key} must be true")


def validate_evidence_document(value: JsonValue) -> JsonObject:
    """Validate the complete normalized evidence document."""

    document = _require_object(value, "evidence document")
    _require_keys(
        document,
        frozenset(
            {
                "artifact",
                "caption",
                "fixtures",
                "normalization",
                "provenance",
                "schema_version",
                "steps",
                "verification",
            }
        ),
        "evidence document",
    )
    _require_exact(document["schema_version"], SCHEMA_VERSION, "evidence schema")
    _require_exact(document["artifact"], ARTIFACT_NAME, "evidence artifact")
    _require_exact(document["caption"], NORMALIZATION_DISCLOSURE, "evidence caption")
    fixtures = _validate_fixture_records(document["fixtures"])
    _validate_normalization(document["normalization"])
    _validate_provenance(document["provenance"])
    _validate_steps(document["steps"], fixtures)
    _validate_verification(document["verification"])

    serialized = canonical_json_bytes(document)
    forbidden = (b"/home/", b"gho_", b"github_pat_", b"BEGIN PRIVATE KEY", b"\x1b")
    if any(token in serialized for token in forbidden):
        _fail("evidence document contains a host path, escape, or credential marker")
    if _NOTE_SEARCH_PATTERN.search(serialized.decode("ascii")) is not None:
        _fail("evidence document retained a generated note identifier")
    return document


def decode_evidence_bytes(raw: bytes) -> JsonObject:
    """Decode and validate one committed canonical evidence document."""

    return validate_evidence_document(
        decode_canonical_document(raw, context="CLI evidence document")
    )
