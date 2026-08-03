"""Installed, machine-readable operator CLI for RecallLedger.

The data directory and tenant identifier are explicit trusted caller context.
This module does not authenticate tenants, infer identity from note content, or
offer model integration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final, NoReturn, TextIO, cast

from .events import (
    CommandId,
    ContractViolation,
    LedgerEvent,
    NoteContent,
    NoteId,
    TenantId,
    TombstoneReason,
)
from .retrieval import MAX_QUERY_BYTES, LexicalQuery, LexicalScore, RetrievalContractError
from .storage import (
    HistoryPage,
    LedgerStorageError,
    SearchHit,
    SearchResults,
    SQLiteLedger,
    TransitionResult,
)

EXIT_SUCCESS: Final = 0
EXIT_USAGE: Final = 2
EXIT_INPUT: Final = 3
EXIT_REQUEST: Final = 10
EXIT_STATE: Final = 11
EXIT_BUSY: Final = 12
EXIT_STORAGE_SAFETY: Final = 13
EXIT_UNCERTAIN: Final = 14
EXIT_STORAGE: Final = 15
EXIT_INTERNAL: Final = 70
EXIT_OUTPUT: Final = 74
EXIT_INTERRUPTED: Final = 130

_DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000
_DEFAULT_HISTORY_LIMIT: Final = 50
_DEFAULT_SEARCH_LIMIT: Final = 20
_MAX_CONTENT_INPUT_BYTES: Final = 524_288
_DECIMAL_PATTERN: Final = re.compile(r"(?:0|-?[1-9][0-9]{0,18})\Z")
_CONTENT_KEYS: Final = frozenset({"body", "tags", "title"})
_JSON_WHITESPACE: Final = frozenset(" \t\r\n")
_SUPPORTED_COMMANDS: Final = frozenset(
    {"create", "get", "head", "history", "revise", "search", "tombstone"}
)
_QUERY_REQUEST_CODES: Final = frozenset(
    {
        "EMPTY_QUERY",
        "INVALID_QUERY_TYPE",
        "INVALID_QUERY_UNICODE",
        "QUERY_NORMALIZATION_TOO_LARGE",
        "QUERY_TERM_TOO_LARGE",
        "QUERY_TOO_LARGE",
        "TOO_MANY_QUERY_TERMS",
    }
)
_CONTENT_RETRIEVAL_CODES: Final = frozenset(
    {
        "CONTENT_NORMALIZATION_TOO_LARGE",
        "CONTENT_TOKEN_STREAM_TOO_LARGE",
    }
)

_REQUEST_CODES: Final = frozenset(
    {
        "INVALID_BUSY_TIMEOUT",
        "INVALID_DATA_DIRECTORY",
        "INVALID_EXPECTED_REVISION",
        "INVALID_HISTORY_CURSOR",
        "INVALID_HISTORY_LIMIT",
        "INVALID_SEARCH_LIMIT",
    }
)
_STATE_CODES: Final = frozenset(
    {
        "IDEMPOTENCY_CONFLICT",
        "NOTE_NOT_FOUND",
        "NOTE_TOMBSTONED",
        "REVISION_CONFLICT",
    }
)
_BUSY_CODES: Final = frozenset({"LEDGER_BUSY", "MIGRATION_BUSY"})
_UNCERTAIN_CODES: Final = frozenset(
    {
        "COMMIT_OUTCOME_UNKNOWN",
        "CONNECTION_POISONED",
        "ROLLBACK_FAILED",
        "TRANSACTION_STATE_UNCERTAIN",
    }
)
_STORAGE_SAFETY_CODES: Final = frozenset(
    {
        "DATABASE_CONFIGURATION_FAILED",
        "DATABASE_INTEGRITY",
        "DATABASE_OPEN_FAILED",
        "DATABASE_PROFILE_UNAVAILABLE",
        "MIGRATION_FAILED",
        "MIGRATION_DRIFT",
        "SCHEMA_TOO_NEW",
        "SQLITE_TOO_OLD",
        "STORAGE_IDENTITY_CHANGED",
        "UNCLAIMED_DATABASE",
        "UNSAFE_DATA_DIRECTORY",
        "UNSAFE_STORAGE_FILE",
        "WRONG_APPLICATION",
    }
)

JsonObject = dict[str, object]


class _CliError(RuntimeError):
    """One bounded CLI-layer rejection."""

    def __init__(
        self,
        *,
        exit_code: int,
        code: str,
        message: str,
        retry: str = "none",
    ) -> None:
        self.exit_code = exit_code
        self.code = code
        self.retry = retry
        super().__init__(message)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argparse parser that never reflects rejected argv values."""

    def error(self, _message: str) -> NoReturn:
        raise _CliError(
            exit_code=EXIT_USAGE,
            code="CLI_USAGE",
            message="invalid command arguments; use --help for the supported shape",
        )

    def print_help(self, _file: object | None = None) -> NoReturn:
        raise _HelpRequestedError(self.format_help())


class _StoreOnceAction(argparse.Action):
    """Reject repeated value-bearing options instead of silently taking the last."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        seen = cast(set[str], getattr(namespace, "_recall_ledger_seen_options", set()))
        if self.dest in seen:
            parser.error(f"duplicate option: {option_string}")
        seen.add(self.dest)
        namespace._recall_ledger_seen_options = seen
        setattr(namespace, self.dest, values)


@dataclass(frozen=True, slots=True)
class _RenderedResult:
    records: tuple[JsonObject, ...]
    jsonl: bool
    output_retry: str
    pretty: bool


class _DuplicateJsonKeyError(ValueError):
    """Internal sentinel for an ambiguous JSON object."""


class _DecimalArgumentError(argparse.ArgumentTypeError):
    """A fixed parser error that does not reflect the rejected value."""

    def __init__(self) -> None:
        super().__init__("expected a bounded ASCII decimal integer")


class _UnexpectedEnvelopeError(RuntimeError):
    """A validated event unexpectedly failed its internal shape assertion."""

    def __init__(self) -> None:
        super().__init__("validated event envelope was not an object")


class _IncompleteWriteError(OSError):
    """A TextIO implementation made no valid forward progress."""

    def __init__(self) -> None:
        super().__init__("the output stream did not accept a valid text chunk")


class _HelpRequestedError(Exception):
    """Carries trusted parser help to the injected output stream."""

    def __init__(self, help_text: str) -> None:
        self.help_text = help_text
        super().__init__()


def _decimal(value: str) -> int:
    if _DECIMAL_PATTERN.fullmatch(value) is None:
        raise _DecimalArgumentError
    return int(value)


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(
        prog="recall-ledger",
        allow_abbrev=False,
        description=(
            "Operate one tenant-scoped RecallLedger using an explicit trusted data "
            "directory and tenant context. Tenant context is not authentication; "
            "search is a bounded reference scan, not a persistent index."
        ),
    )
    parser.add_argument(
        "--data-dir",
        action=_StoreOnceAction,
        required=True,
        metavar="ABSOLUTE_DIRECTORY",
        help="existing operator-owned absolute 0700 data directory",
    )
    parser.add_argument(
        "--tenant-id",
        action=_StoreOnceAction,
        required=True,
        metavar="TENANT_ID",
        help="trusted caller-resolved tenant identifier; not an authorization token",
    )
    parser.add_argument(
        "--busy-timeout-ms",
        action=_StoreOnceAction,
        type=_decimal,
        default=_DEFAULT_BUSY_TIMEOUT_MS,
        metavar="MILLISECONDS",
        help="SQLite busy timeout from 0 through 60000 (default: 5000)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent the single-document JSON result",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create",
        allow_abbrev=False,
        help="create a note or replay its exact command",
    )
    _add_command_id(create)
    _add_content_file(create)

    revise = commands.add_parser(
        "revise",
        allow_abbrev=False,
        help="revise a live note at an exact revision",
    )
    _add_note_id(revise)
    _add_command_id(revise)
    _add_expected_revision(revise)
    _add_content_file(revise)

    tombstone = commands.add_parser(
        "tombstone",
        allow_abbrev=False,
        help="append a content-free terminal tombstone at an exact revision",
    )
    _add_note_id(tombstone)
    _add_command_id(tombstone)
    _add_expected_revision(tombstone)
    tombstone.add_argument(
        "--reason",
        action=_StoreOnceAction,
        required=True,
        choices=tuple(reason.value for reason in TombstoneReason),
        help="bounded logical-deletion reason code",
    )

    get = commands.add_parser(
        "get",
        allow_abbrev=False,
        help="read a live head; tombstones appear absent",
    )
    _add_note_id(get)

    head = commands.add_parser(
        "head",
        allow_abbrev=False,
        help="read the current head, including a tombstone",
    )
    _add_note_id(head)

    history = commands.add_parser(
        "history",
        allow_abbrev=False,
        help="read one verified bounded history page",
    )
    _add_note_id(history)
    history.add_argument(
        "--after-revision",
        action=_StoreOnceAction,
        type=_decimal,
        default=0,
        metavar="REVISION",
        help="exclusive page cursor from 0 through the current head (default: 0)",
    )
    history.add_argument(
        "--limit",
        action=_StoreOnceAction,
        type=_decimal,
        default=_DEFAULT_HISTORY_LIMIT,
        metavar="COUNT",
        help="page size from 1 through 100 (default: 50)",
    )
    history.add_argument(
        "--jsonl",
        action="store_true",
        help="emit one event record per line followed by one page record",
    )

    search = commands.add_parser(
        "search",
        allow_abbrev=False,
        help="score every verified live tenant head with the deterministic lexical oracle",
    )
    search.add_argument(
        "--query-file",
        action=_StoreOnceAction,
        required=True,
        metavar="PATH|-",
        help="strict UTF-8 query text from a regular file, or '-' for stdin; not trimmed",
    )
    search.add_argument(
        "--limit",
        action=_StoreOnceAction,
        type=_decimal,
        default=_DEFAULT_SEARCH_LIMIT,
        metavar="COUNT",
        help="top-K count from 1 through 100; the complete scan is unchanged (default: 20)",
    )
    search.add_argument(
        "--jsonl",
        action="store_true",
        help="emit one hit record per line followed by one summary record",
    )
    return parser


def _add_note_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--note-id",
        action=_StoreOnceAction,
        required=True,
        metavar="NOTE_ID",
    )


def _add_command_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--command-id",
        action=_StoreOnceAction,
        required=True,
        metavar="COMMAND_ID",
        help="caller-retained idempotency key; exact retries must reuse it",
    )


def _add_expected_revision(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-revision",
        action=_StoreOnceAction,
        required=True,
        type=_decimal,
        metavar="REVISION",
        help="exact optimistic-concurrency precondition",
    )


def _add_content_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--content-file",
        action=_StoreOnceAction,
        required=True,
        metavar="PATH|-",
        help="strict UTF-8 JSON object from a file, or '-' for stdin",
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError


def _has_top_level_array(text: str) -> bool:
    """Classify an array root without descending into decoder-specific recursion."""

    for character in text:
        if character not in _JSON_WHITESPACE:
            return character == "["
    return False


def _raise_content_root_shape() -> NoReturn:
    raise _CliError(
        exit_code=EXIT_INPUT,
        code="INPUT_INVALID_SHAPE",
        message="content JSON must contain exactly body, tags, and title",
    )


def _read_bounded_input(
    stream: BinaryIO,
    *,
    maximum: int,
    input_name: str,
    too_large_message: str,
) -> bytes:
    payload = bytearray()
    while len(payload) <= maximum:
        remaining = maximum + 1 - len(payload)
        try:
            chunk = stream.read(remaining)
        except (OSError, ValueError):
            raise _CliError(
                exit_code=EXIT_INPUT,
                code="INPUT_UNAVAILABLE",
                message=f"the {input_name} input could not be read",
            ) from None
        if type(chunk) is not bytes:
            raise _CliError(
                exit_code=EXIT_INPUT,
                code="INPUT_INVALID_UTF8",
                message=f"the {input_name} input must be strict UTF-8 bytes",
            )
        if len(chunk) > remaining:
            raise _CliError(
                exit_code=EXIT_INPUT,
                code="INPUT_TOO_LARGE",
                message=too_large_message,
            )
        if not chunk:
            break
        payload.extend(chunk)
    if len(payload) > maximum:
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_TOO_LARGE",
            message=too_large_message,
        )
    return bytes(payload)


def _read_bounded(stream: BinaryIO) -> bytes:
    return _read_bounded_input(
        stream,
        maximum=_MAX_CONTENT_INPUT_BYTES,
        input_name="content",
        too_large_message="the content JSON exceeds the CLI input limit",
    )


def _read_bounded_query(stream: BinaryIO) -> bytes:
    return _read_bounded_input(
        stream,
        maximum=MAX_QUERY_BYTES,
        input_name="query",
        too_large_message="the query text exceeds the CLI input limit",
    )


def _require_regular_input_file(descriptor: int, *, input_name: str) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_UNSAFE_FILE",
            message=f"the {input_name} input must be a regular non-symlink file",
        )


def _require_regular_file(descriptor: int) -> None:
    _require_regular_input_file(descriptor, input_name="content")


def _read_input_file(
    path_text: str,
    *,
    input_name: str,
    maximum: int,
    too_large_message: str,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path_text,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        _require_regular_input_file(descriptor, input_name=input_name)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return _read_bounded_input(
                stream,
                maximum=maximum,
                input_name=input_name,
                too_large_message=too_large_message,
            )
    except _CliError:
        raise
    except (OSError, ValueError):
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_UNAVAILABLE",
            message=f"the {input_name} input could not be opened",
        ) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise _CliError(
                    exit_code=EXIT_INPUT,
                    code="INPUT_UNAVAILABLE",
                    message=f"the {input_name} input could not be closed",
                ) from None


def _read_content_file(path_text: str) -> bytes:
    return _read_input_file(
        path_text,
        input_name="content",
        maximum=_MAX_CONTENT_INPUT_BYTES,
        too_large_message="the content JSON exceeds the CLI input limit",
    )


def _read_query_file(path_text: str) -> bytes:
    return _read_input_file(
        path_text,
        input_name="query",
        maximum=MAX_QUERY_BYTES,
        too_large_message="the query text exceeds the CLI input limit",
    )


def _read_content(path_text: str, stdin: BinaryIO) -> NoteContent:
    payload = _read_bounded(stdin) if path_text == "-" else _read_content_file(path_text)
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_INVALID_UTF8",
            message="the content input is not strict UTF-8",
        ) from None
    try:
        value = cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            ),
        )
    except _DuplicateJsonKeyError:
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_DUPLICATE_KEY",
            message="the content JSON contains a duplicate object key",
        ) from None
    except RecursionError:
        if _has_top_level_array(text):
            _raise_content_root_shape()
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_INVALID_JSON",
            message="the content input is not strict JSON",
        ) from None
    except (json.JSONDecodeError, ValueError):
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_INVALID_JSON",
            message="the content input is not strict JSON",
        ) from None
    if type(value) is not dict or set(value) != _CONTENT_KEYS:
        _raise_content_root_shape()
    content = cast(dict[str, object], value)
    title = content["title"]
    body = content["body"]
    tags = content["tags"]
    if type(title) is not str or type(body) is not str or type(tags) is not list:
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_INVALID_SHAPE",
            message="content title and body must be strings and tags must be an array",
        )
    if any(type(tag) is not str for tag in tags):
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_INVALID_SHAPE",
            message="every content tag must be a string",
        )
    return NoteContent(title=title, body=body, tags=tuple(cast(list[str], tags)))


def _read_query(path_text: str, stdin: BinaryIO) -> str:
    payload = _read_bounded_query(stdin) if path_text == "-" else _read_query_file(path_text)
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _CliError(
            exit_code=EXIT_INPUT,
            code="INPUT_INVALID_UTF8",
            message="the query input is not strict UTF-8",
        ) from None


def _event_object(event: LedgerEvent) -> JsonObject:
    decoded = cast(object, json.loads(event.to_bytes()))
    if type(decoded) is not dict:
        raise _UnexpectedEnvelopeError
    return cast(JsonObject, decoded)


def _mutation_result(operation: str, result: TransitionResult) -> JsonObject:
    return {
        "event": _event_object(result.event),
        "ok": True,
        "operation": operation,
        "replayed": result.replayed,
    }


def _read_result(operation: str, event: LedgerEvent | None) -> JsonObject:
    return {
        "event": None if event is None else _event_object(event),
        "found": event is not None,
        "ok": True,
        "operation": operation,
    }


def _history_document(
    *,
    tenant_id: TenantId,
    note_id: NoteId,
    after_revision: int,
    limit: int,
    page: HistoryPage | None,
) -> JsonObject:
    return {
        "after_revision": after_revision,
        "events": [] if page is None else [_event_object(event) for event in page.events],
        "found": page is not None,
        "limit": limit,
        "next_after_revision": None if page is None else page.next_after_revision,
        "note_id": note_id,
        "ok": True,
        "operation": "history",
        "tenant_id": tenant_id,
    }


def _history_records(
    *,
    tenant_id: TenantId,
    note_id: NoteId,
    after_revision: int,
    limit: int,
    page: HistoryPage | None,
) -> tuple[JsonObject, ...]:
    events = () if page is None else page.events
    event_records: tuple[JsonObject, ...] = tuple(
        {"event": _event_object(event), "record": "event"} for event in events
    )
    page_record: JsonObject = {
        "after_revision": after_revision,
        "event_count": len(events),
        "found": page is not None,
        "limit": limit,
        "next_after_revision": None if page is None else page.next_after_revision,
        "note_id": note_id,
        "record": "page",
        "tenant_id": tenant_id,
    }
    return (*event_records, page_record)


def _query_object(query: LexicalQuery) -> JsonObject:
    return {
        "contract_version": query.contract_version,
        "encoded_terms": query.encoded_terms,
        "match_expression": query.match_expression,
        "terms": query.terms,
        "unicode_profile": query.unicode_profile,
    }


def _score_object(score: LexicalScore) -> JsonObject:
    return {
        "body_phrase": score.body_phrase,
        "body_term_frequency": score.body_term_frequency,
        "contract_version": score.contract_version,
        "tag_phrase": score.tag_phrase,
        "tag_term_frequency": score.tag_term_frequency,
        "title_phrase": score.title_phrase,
        "title_term_frequency": score.title_term_frequency,
        "total": score.total,
        "unicode_profile": score.unicode_profile,
    }


def _search_hit_object(hit: SearchHit, *, rank: int) -> JsonObject:
    return {
        "citation": {
            "event_hash": hit.citation.event_hash,
            "note_id": hit.citation.note_id,
            "revision": hit.citation.revision,
            "tenant_id": hit.citation.tenant_id,
        },
        "content": {
            "body": hit.content.body,
            "tags": hit.content.tags,
            "title": hit.content.title,
        },
        "rank": rank,
        "recorded_at_us": hit.recorded_at_us,
        "score": _score_object(hit.score),
    }


def _search_document(results: SearchResults) -> JsonObject:
    return {
        "hits": [
            _search_hit_object(hit, rank=rank) for rank, hit in enumerate(results.hits, start=1)
        ],
        "limit": results.limit,
        "ok": True,
        "operation": "search",
        "query": _query_object(results.query),
        "scanned_content_bytes": results.scanned_content_bytes,
        "scanned_heads": results.scanned_heads,
        "scanned_live_notes": results.scanned_live_notes,
        "tenant_id": results.tenant_id,
        "total_matches": results.total_matches,
        "truncated": results.total_matches > len(results.hits),
    }


def _search_records(results: SearchResults) -> tuple[JsonObject, ...]:
    hit_records: tuple[JsonObject, ...] = tuple(
        {"hit": _search_hit_object(hit, rank=rank), "record": "hit"}
        for rank, hit in enumerate(results.hits, start=1)
    )
    summary: JsonObject = {
        "hit_count": len(results.hits),
        "limit": results.limit,
        "query": _query_object(results.query),
        "record": "summary",
        "scanned_content_bytes": results.scanned_content_bytes,
        "scanned_heads": results.scanned_heads,
        "scanned_live_notes": results.scanned_live_notes,
        "tenant_id": results.tenant_id,
        "total_matches": results.total_matches,
        "truncated": results.total_matches > len(results.hits),
    }
    return (*hit_records, summary)


def _command_retry(command: str | None, *, reopen: bool = False) -> str:
    mutation = command in {"create", "revise", "tombstone"}
    if reopen:
        return "reopen_and_retry_exact_command" if mutation else "reopen_and_retry_same_invocation"
    return "retry_exact_command" if mutation else "retry_same_invocation"


def _raise_close_failure(error: BaseException, *, command: str) -> NoReturn:
    retry = _command_retry(command)
    if isinstance(error, LedgerStorageError):
        raise _CliError(
            exit_code=_storage_exit_code(error.code),
            code=error.code,
            message=str(error),
            retry=retry,
        ) from None
    if isinstance(error, KeyboardInterrupt):
        raise _CliError(
            exit_code=EXIT_INTERRUPTED,
            code="INTERRUPTED",
            message="closing the ledger was interrupted",
            retry=retry,
        ) from None
    raise error


def _execute(  # noqa: PLR0912,PLR0915 - command and lifecycle states stay explicit
    namespace: argparse.Namespace,
    *,
    stdin: BinaryIO,
) -> _RenderedResult:
    data_directory = Path(cast(str, namespace.data_dir))
    if not data_directory.is_absolute():
        raise _CliError(
            exit_code=EXIT_REQUEST,
            code="INVALID_DATA_DIRECTORY",
            message="the data directory must be an explicit absolute path",
        )
    tenant_id = TenantId(cast(str, namespace.tenant_id))
    command = cast(str, namespace.command)
    if command not in _SUPPORTED_COMMANDS:
        raise _CliError(
            exit_code=EXIT_USAGE,
            code="CLI_USAGE",
            message="the parser produced an unsupported command",
        )
    pretty = cast(bool, namespace.pretty)
    busy_timeout_ms = cast(int, namespace.busy_timeout_ms)
    jsonl = cast(bool, getattr(namespace, "jsonl", False))
    if pretty and jsonl:
        raise _CliError(
            exit_code=EXIT_USAGE,
            code="CLI_USAGE",
            message="--pretty and --jsonl cannot be combined",
        )

    content: NoteContent | None = None
    query_text: str | None = None
    if command in {"create", "revise"}:
        content = _read_content(cast(str, namespace.content_file), stdin)
    elif command == "search":
        query_text = _read_query(cast(str, namespace.query_file), stdin)

    ledger = SQLiteLedger.open(data_directory, busy_timeout_ms=busy_timeout_ms)
    records: tuple[JsonObject, ...]
    try:
        if command == "create":
            result = ledger.create_note(
                tenant_id=tenant_id,
                command_id=CommandId(cast(str, namespace.command_id)),
                content=cast(NoteContent, content),
            )
            records = (_mutation_result(command, result),)
        elif command == "revise":
            result = ledger.revise_note(
                tenant_id=tenant_id,
                note_id=NoteId(cast(str, namespace.note_id)),
                command_id=CommandId(cast(str, namespace.command_id)),
                expected_revision=cast(int, namespace.expected_revision),
                content=cast(NoteContent, content),
            )
            records = (_mutation_result(command, result),)
        elif command == "tombstone":
            result = ledger.tombstone_note(
                tenant_id=tenant_id,
                note_id=NoteId(cast(str, namespace.note_id)),
                command_id=CommandId(cast(str, namespace.command_id)),
                expected_revision=cast(int, namespace.expected_revision),
                reason=TombstoneReason(cast(str, namespace.reason)),
            )
            records = (_mutation_result(command, result),)
        elif command == "get":
            event = ledger.get_note(
                tenant_id=tenant_id,
                note_id=NoteId(cast(str, namespace.note_id)),
            )
            records = (_read_result(command, event),)
        elif command == "head":
            event = ledger.get_head(
                tenant_id=tenant_id,
                note_id=NoteId(cast(str, namespace.note_id)),
            )
            records = (_read_result(command, event),)
        elif command == "history":
            note_id = NoteId(cast(str, namespace.note_id))
            after_revision = cast(int, namespace.after_revision)
            limit = cast(int, namespace.limit)
            page = ledger.read_history(
                tenant_id=tenant_id,
                note_id=note_id,
                after_revision=after_revision,
                limit=limit,
            )
            records = (
                _history_records(
                    tenant_id=tenant_id,
                    note_id=note_id,
                    after_revision=after_revision,
                    limit=limit,
                    page=page,
                )
                if jsonl
                else (
                    _history_document(
                        tenant_id=tenant_id,
                        note_id=note_id,
                        after_revision=after_revision,
                        limit=limit,
                        page=page,
                    ),
                )
            )
        else:
            results = ledger.search_notes(
                tenant_id=tenant_id,
                query=cast(str, query_text),
                limit=cast(int, namespace.limit),
            )
            records = _search_records(results) if jsonl else (_search_document(results),)
    except BaseException as operation_error:
        try:
            ledger.close()
        except BaseException as close_error:
            if (
                isinstance(operation_error, LedgerStorageError)
                and operation_error.code in _UNCERTAIN_CODES
            ):
                raise operation_error from None
            _raise_close_failure(close_error, command=command)
        raise
    else:
        try:
            ledger.close()
        except BaseException as close_error:
            _raise_close_failure(close_error, command=command)
    output_retry = _command_retry(command)
    return _RenderedResult(
        records=records,
        jsonl=jsonl,
        output_retry=output_retry,
        pretty=pretty,
    )


def _storage_exit_code(code: str) -> int:
    if code in _REQUEST_CODES:
        return EXIT_REQUEST
    if code in _STATE_CODES:
        return EXIT_STATE
    if code in _BUSY_CODES:
        return EXIT_BUSY
    if code in _UNCERTAIN_CODES:
        return EXIT_UNCERTAIN
    if code in _STORAGE_SAFETY_CODES:
        return EXIT_STORAGE_SAFETY
    return EXIT_STORAGE


def _retry_guidance(code: str, *, command: str | None) -> str:
    if code in _BUSY_CODES:
        return _command_retry(command)
    if code in _UNCERTAIN_CODES:
        return _command_retry(command, reopen=True)
    if code == "REVISION_CONFLICT":
        return "inspect_head_then_new_command"
    if code == "IDEMPOTENCY_CONFLICT":
        return "do_not_retry_same_command"
    return "none"


def _error_object(*, code: str, message: str, retry: str) -> JsonObject:
    return {
        "error": {
            "code": code,
            "message": message,
            "retry": retry,
        },
        "ok": False,
    }


def _compact_json(value: JsonObject) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _serialize(result: _RenderedResult) -> str:
    if result.jsonl:
        return "".join(f"{_compact_json(record)}\n" for record in result.records)
    return (
        json.dumps(
            result.records[0],
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            indent=2 if result.pretty else None,
            separators=None if result.pretty else (",", ":"),
        )
        + "\n"
    )


def _write(stream: TextIO, payload: str) -> None:
    offset = 0
    while offset < len(payload):
        written: object = stream.write(payload[offset:])
        remaining = len(payload) - offset
        if type(written) is not int or not 1 <= written <= remaining:
            raise _IncompleteWriteError
        offset += written
    stream.flush()


def _emit_error(
    stream: TextIO,
    *,
    exit_code: int,
    code: str,
    message: str,
    retry: str,
) -> int:
    payload = f"{_compact_json(_error_object(code=code, message=message, retry=retry))}\n"
    try:
        _write(stream, payload)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except (BrokenPipeError, OSError, UnicodeError, ValueError):
        return EXIT_OUTPUT
    return exit_code


def _emit_help(stdout: TextIO, stderr: TextIO, help_text: str) -> int:
    try:
        _write(stdout, help_text)
    except KeyboardInterrupt:
        return _emit_error(
            stderr,
            exit_code=EXIT_INTERRUPTED,
            code="INTERRUPTED",
            message="writing help was interrupted",
            retry="none",
        )
    except (BrokenPipeError, OSError, UnicodeError, ValueError):
        return _emit_error(
            stderr,
            exit_code=EXIT_OUTPUT,
            code="OUTPUT_UNAVAILABLE",
            message="the help result could not be written",
            retry="none",
        )
    return EXIT_SUCCESS


def run_cli(  # noqa: PLR0911 - stable exit categories stay explicit at the boundary
    argv: Sequence[str],
    *,
    stdin: BinaryIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Run one CLI invocation against injected streams."""

    namespace: argparse.Namespace | None = None
    try:
        namespace = _parser().parse_args(list(argv))
        result = _execute(namespace, stdin=stdin)
        payload = _serialize(result)
    except _HelpRequestedError as help_request:
        return _emit_help(stdout, stderr, help_request.help_text)
    except _CliError as error:
        return _emit_error(
            stderr,
            exit_code=error.exit_code,
            code=error.code,
            message=str(error),
            retry=error.retry,
        )
    except ContractViolation as error:
        command = None if namespace is None else cast(str, namespace.command)
        return _emit_error(
            stderr,
            exit_code=EXIT_REQUEST,
            code=error.code,
            message=str(error),
            retry=_retry_guidance(error.code, command=command),
        )
    except RetrievalContractError as error:
        if error.code in _QUERY_REQUEST_CODES:
            return _emit_error(
                stderr,
                exit_code=EXIT_REQUEST,
                code=error.code,
                message=str(error),
                retry="none",
            )
        if error.code in _CONTENT_RETRIEVAL_CODES:
            return _emit_error(
                stderr,
                exit_code=EXIT_STORAGE,
                code=error.code,
                message=str(error),
                retry="none",
            )
        return _emit_error(
            stderr,
            exit_code=EXIT_INTERNAL,
            code="INTERNAL_ERROR",
            message="the CLI failed without exposing internal diagnostics",
            retry="none",
        )
    except LedgerStorageError as error:
        command = None if namespace is None else cast(str, namespace.command)
        return _emit_error(
            stderr,
            exit_code=_storage_exit_code(error.code),
            code=error.code,
            message=str(error),
            retry=_retry_guidance(error.code, command=command),
        )
    except KeyboardInterrupt:
        command = None if namespace is None else cast(str, namespace.command)
        return _emit_error(
            stderr,
            exit_code=EXIT_INTERRUPTED,
            code="INTERRUPTED",
            message="the operation was interrupted",
            retry=_command_retry(command, reopen=True),
        )
    except Exception:
        return _emit_error(
            stderr,
            exit_code=EXIT_INTERNAL,
            code="INTERNAL_ERROR",
            message="the CLI failed without exposing internal diagnostics",
            retry="none",
        )

    try:
        _write(stdout, payload)
    except KeyboardInterrupt:
        return _emit_error(
            stderr,
            exit_code=EXIT_INTERRUPTED,
            code="INTERRUPTED",
            message="writing the JSON result was interrupted",
            retry=result.output_retry,
        )
    except (BrokenPipeError, OSError, UnicodeError, ValueError):
        return _emit_error(
            stderr,
            exit_code=EXIT_OUTPUT,
            code="OUTPUT_UNAVAILABLE",
            message="the JSON result could not be written",
            retry=result.output_retry,
        )
    return EXIT_SUCCESS


def _neutralize_standard_streams() -> None:
    """Point failed standard descriptors at /dev/null before interpreter teardown."""

    descriptors: set[int] = set()
    for stream in (sys.stdout, sys.stderr):
        try:
            descriptors.add(stream.fileno())
        except (AttributeError, OSError, ValueError):
            continue
    if not descriptors:
        return
    try:
        null_descriptor = _open_null_descriptor()
    except OSError:
        return
    try:
        for descriptor in descriptors:
            if descriptor != null_descriptor:
                _replace_descriptor(null_descriptor, descriptor)
    except OSError:
        return
    finally:
        if null_descriptor not in descriptors:
            _close_descriptor_quietly(null_descriptor)


def _open_null_descriptor() -> int:
    return os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)


def _replace_descriptor(source: int, destination: int) -> None:
    os.dup2(source, destination)


def _close_descriptor_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        return


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""

    arguments = sys.argv[1:] if argv is None else argv
    stdin = cast(BinaryIO, getattr(sys.stdin, "buffer", sys.stdin))
    exit_code = run_cli(
        arguments,
        stdin=stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if exit_code in {EXIT_OUTPUT, EXIT_INTERRUPTED}:
        _neutralize_standard_streams()
    return exit_code
