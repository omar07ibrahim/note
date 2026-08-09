"""Transaction-local intents, projections, and event persistence helpers."""

from __future__ import annotations

import sqlite3
import textwrap
import time
from itertools import pairwise
from typing import Final, cast

from .events import (
    MAX_RECORDED_AT_US,
    MAX_REVISION,
    CommandId,
    ContractViolation,
    EventKind,
    LedgerEvent,
    NoteContent,
    NoteId,
    TenantId,
    TombstoneReason,
    _validate_identifier,
    _validated_content_values,
    decode_event,
    new_note_id,
)
from .retrieval import LexicalQuery, lexical_token_streams, score_lexical_content
from .storage import (
    FTS5_CANDIDATE_SCHEMA_VERSION,
    FTS5_CANDIDATE_TOKENIZER,
    MAX_PAGE_SIZE,
    MAX_SEARCH_HEADS,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_LIVE_CONTENT_BYTES,
    Fts5CandidateAudit,
    HistoryPage,
    LedgerStorageError,
    SearchCitation,
    SearchHit,
    SearchResults,
    TransitionResult,
    _TransitionIntent,
)

_NOTE_ID_GENERATION_ATTEMPTS: Final = 4


def _sql(value: str) -> str:
    return textwrap.dedent(value).strip()


_EVENT_COLUMNS: Final = _sql(
    """
    tenant_id,
    note_id,
    revision,
    command_id,
    recorded_at_us,
    kind,
    previous_revision,
    previous_event_hash,
    event_hash,
    schema_version,
    event_bytes
    """
)
_JOINED_EVENT_COLUMNS: Final = _sql(
    """
    event.tenant_id AS tenant_id,
    event.note_id AS note_id,
    event.revision AS revision,
    event.command_id AS command_id,
    event.recorded_at_us AS recorded_at_us,
    event.kind AS kind,
    event.previous_revision AS previous_revision,
    event.previous_event_hash AS previous_event_hash,
    event.event_hash AS event_hash,
    event.schema_version AS schema_version,
    event.event_bytes AS event_bytes
    """
)
_LOAD_COMMAND_EVENT_SQL: Final = _sql(
    f"""
    SELECT {_EVENT_COLUMNS}
    FROM ledger_events
    WHERE tenant_id = ? AND command_id = ?
    """  # noqa: S608 - interpolated fragments are fixed module constants
)
_LOAD_EVENT_SQL: Final = _sql(
    f"""
    SELECT {_EVENT_COLUMNS}
    FROM ledger_events
    WHERE tenant_id = ? AND note_id = ? AND revision = ?
    """  # noqa: S608 - interpolated fragments are fixed module constants
)
_LOAD_HEAD_SQL: Final = _sql(
    f"""
    WITH latest AS (
        SELECT revision, event_hash
        FROM ledger_events
        WHERE tenant_id = ? AND note_id = ?
        ORDER BY revision DESC
        LIMIT 1
    )
    SELECT
        head.tenant_id AS head_tenant_id,
        head.note_id AS head_note_id,
        head.revision AS head_revision,
        head.event_hash AS head_event_hash,
        head.updated_at_us AS head_updated_at_us,
        head.is_tombstoned AS head_is_tombstoned,
        latest.revision AS latest_revision,
        latest.event_hash AS latest_event_hash,
        {_JOINED_EVENT_COLUMNS}
    FROM (SELECT 1) AS singleton
    LEFT JOIN note_heads AS head
      ON head.tenant_id = ? AND head.note_id = ?
    LEFT JOIN ledger_events AS event
      ON event.tenant_id = head.tenant_id
     AND event.note_id = head.note_id
     AND event.revision = head.revision
     AND event.event_hash = head.event_hash
    LEFT JOIN latest ON 1 = 1
    """  # noqa: S608 - interpolated fragments are fixed module constants
)
_LOAD_HISTORY_SQL: Final = _sql(
    f"""
    SELECT {_EVENT_COLUMNS}
    FROM ledger_events
    WHERE tenant_id = ? AND note_id = ? AND revision >= ?
    ORDER BY revision ASC
    LIMIT ?
    """  # noqa: S608 - interpolated fragments are fixed module constants
)
_LOAD_TENANT_HEAD_IDS_SQL: Final = _sql(
    """
    SELECT note_id
    FROM note_heads INDEXED BY note_heads_all_page
    WHERE tenant_id = ?
    ORDER BY updated_at_us DESC, note_id ASC
    LIMIT ?
    """
)
_LOAD_FIRST_TENANT_EVENT_NOTE_ID_SQL: Final = _sql(
    """
    SELECT note_id
    FROM ledger_events
    WHERE tenant_id = ?
    ORDER BY note_id ASC
    LIMIT 1
    """
)
_LOAD_NEXT_TENANT_EVENT_NOTE_ID_SQL: Final = _sql(
    """
    SELECT note_id
    FROM ledger_events
    WHERE tenant_id = ? AND note_id > ?
    ORDER BY note_id ASC
    LIMIT 1
    """
)


_CREATE_FTS5_CANDIDATES_SQL: Final = _sql(
    """
    CREATE VIRTUAL TABLE temp.recall_ledger_fts5_candidates USING fts5(
        note_id UNINDEXED,
        title,
        body,
        tags,
        tokenize = 'ascii',
        detail = none,
        columnsize = 0
    )
    """
)
_INSERT_FTS5_CANDIDATE_SQL: Final = _sql(
    """
    INSERT INTO temp.recall_ledger_fts5_candidates(note_id, title, body, tags)
    VALUES (?, ?, ?, ?)
    """
)
_SELECT_FTS5_CANDIDATES_SQL: Final = _sql(
    """
    SELECT note_id
    FROM temp.recall_ledger_fts5_candidates
    WHERE recall_ledger_fts5_candidates MATCH ?
    ORDER BY note_id ASC
    """
)
_DROP_FTS5_CANDIDATES_SQL: Final = "DROP TABLE temp.recall_ledger_fts5_candidates"


def _create_intent(
    *,
    tenant_id: TenantId,
    command_id: CommandId,
    content: NoteContent,
) -> _TransitionIntent:
    _validate_identifier(tenant_id, "tenant_id")
    _validate_identifier(command_id, "command_id")
    _validated_content_values(content)
    return _TransitionIntent(
        tenant_id=tenant_id,
        note_id=None,
        command_id=command_id,
        kind=EventKind.CREATED,
        expected_revision=None,
        content=content,
        tombstone_reason=None,
    )


def _revise_intent(
    *,
    tenant_id: TenantId,
    note_id: NoteId,
    command_id: CommandId,
    expected_revision: int,
    content: NoteContent,
) -> _TransitionIntent:
    _validate_transition_identity(tenant_id, note_id, command_id)
    _validate_expected_revision(expected_revision)
    _validated_content_values(content)
    return _TransitionIntent(
        tenant_id=tenant_id,
        note_id=note_id,
        command_id=command_id,
        kind=EventKind.REVISED,
        expected_revision=expected_revision,
        content=content,
        tombstone_reason=None,
    )


def _tombstone_intent(
    *,
    tenant_id: TenantId,
    note_id: NoteId,
    command_id: CommandId,
    expected_revision: int,
    reason: TombstoneReason,
) -> _TransitionIntent:
    _validate_transition_identity(tenant_id, note_id, command_id)
    _validate_expected_revision(expected_revision)
    if type(reason) is not TombstoneReason:
        raise ContractViolation(
            "INVALID_TOMBSTONE_REASON",
            "tombstone reason is not supported",
        )
    return _TransitionIntent(
        tenant_id=tenant_id,
        note_id=note_id,
        command_id=command_id,
        kind=EventKind.TOMBSTONED,
        expected_revision=expected_revision,
        content=None,
        tombstone_reason=reason,
    )


def _validate_transition_identity(
    tenant_id: TenantId,
    note_id: NoteId,
    command_id: CommandId,
) -> None:
    _validate_tenant_note(tenant_id, note_id)
    _validate_identifier(command_id, "command_id")


def _validate_tenant_note(tenant_id: TenantId, note_id: NoteId) -> None:
    _validate_tenant(tenant_id)
    _validate_identifier(note_id, "note_id")


def _validate_tenant(tenant_id: TenantId) -> None:
    _validate_identifier(tenant_id, "tenant_id")


def _validate_expected_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise LedgerStorageError(
            "INVALID_EXPECTED_REVISION",
            "expected revision must be a bounded positive exact integer",
        )
    return value


def _validate_history_cursor(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise LedgerStorageError(
            "INVALID_HISTORY_CURSOR",
            "history cursor must be a bounded non-negative exact integer",
        )
    return value


def _validate_history_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise LedgerStorageError(
            "INVALID_HISTORY_LIMIT",
            "history limit must be an exact integer in the supported range",
        )
    return value


def _validate_search_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SEARCH_LIMIT:
        raise LedgerStorageError(
            "INVALID_SEARCH_LIMIT",
            "search limit must be an exact integer in the supported range",
        )
    return value


def _validate_recorded_at_us(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RECORDED_AT_US:
        raise LedgerStorageError(
            "CLOCK_OUT_OF_RANGE",
            f"{field} is outside the supported timestamp range",
        )
    return value


def _utc_now_us() -> int:
    return time.time_ns() // 1_000


def _new_note_id() -> NoteId:
    return new_note_id()


def _next_recorded_at_us(parent: LedgerEvent | None) -> int:
    try:
        current = _utc_now_us()
    except Exception:
        raise LedgerStorageError(
            "CLOCK_UNAVAILABLE",
            "the storage clock is unavailable",
        ) from None
    current = _validate_recorded_at_us(current, field="storage clock")
    if parent is None:
        return current
    return max(current, parent.recorded_at_us)


def _transition_in_transaction(
    connection: sqlite3.Connection,
    intent: _TransitionIntent,
) -> TransitionResult:
    existing = _load_command_event(
        connection,
        tenant_id=intent.tenant_id,
        command_id=intent.command_id,
    )
    if existing is not None:
        if not _event_matches_intent(existing, intent):
            raise LedgerStorageError(
                "IDEMPOTENCY_CONFLICT",
                "the command identifier was already used for another intent",
            )
        _verify_replayed_projection(connection, existing)
        return TransitionResult(event=existing, replayed=True)

    if intent.kind is EventKind.CREATED:
        event = _create_event_for_intent(connection, intent)
        _insert_event(connection, event)
        _insert_head(connection, event)
    else:
        note_id = cast(NoteId, intent.note_id)
        parent = _load_head(connection, intent.tenant_id, note_id)
        if parent is None:
            raise LedgerStorageError(
                "NOTE_NOT_FOUND",
                "the tenant-scoped note does not exist",
            )
        if parent.kind is EventKind.TOMBSTONED:
            raise LedgerStorageError(
                "NOTE_TOMBSTONED",
                "the tenant-scoped note is already tombstoned",
            )
        if parent.revision != intent.expected_revision:
            raise LedgerStorageError(
                "REVISION_CONFLICT",
                "the note head no longer matches the expected revision",
            )
        if parent.revision >= MAX_REVISION:
            raise ContractViolation(
                "REVISION_EXHAUSTED",
                "the note revision counter cannot advance",
            )
        timestamp = _next_recorded_at_us(parent)
        if intent.kind is EventKind.REVISED:
            event = parent.revise(
                command_id=intent.command_id,
                recorded_at_us=timestamp,
                content=cast(NoteContent, intent.content),
            )
        else:
            event = parent.tombstone(
                command_id=intent.command_id,
                recorded_at_us=timestamp,
                reason=cast(TombstoneReason, intent.tombstone_reason),
            )
        _insert_event(connection, event)
        _cas_head(connection, previous=parent, event=event)

    stored = _load_event(
        connection,
        event.tenant_id,
        event.note_id,
        event.revision,
    )
    head = _load_head(connection, event.tenant_id, event.note_id)
    if stored != event or head != event:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the committed transition candidates failed exact verification",
        )
    return TransitionResult(event=event, replayed=False)


def _create_event_for_intent(
    connection: sqlite3.Connection,
    intent: _TransitionIntent,
) -> LedgerEvent:
    note_id: NoteId | None = None
    for _attempt in range(_NOTE_ID_GENERATION_ATTEMPTS):
        try:
            candidate = _new_note_id()
            _validate_identifier(candidate, "note_id")
        except Exception:
            raise LedgerStorageError(
                "ID_GENERATION_FAILED",
                "a storage-owned note identifier could not be generated",
            ) from None
        if not _note_storage_exists(connection, intent.tenant_id, candidate):
            note_id = candidate
            break
    if note_id is None:
        raise LedgerStorageError(
            "ID_GENERATION_EXHAUSTED",
            "storage-owned note identifier retries were exhausted",
        )
    return LedgerEvent.create(
        tenant_id=intent.tenant_id,
        note_id=note_id,
        command_id=intent.command_id,
        recorded_at_us=_next_recorded_at_us(None),
        content=cast(NoteContent, intent.content),
    )


def _event_matches_intent(event: LedgerEvent, intent: _TransitionIntent) -> bool:
    if (
        event.tenant_id != intent.tenant_id
        or event.command_id != intent.command_id
        or event.kind is not intent.kind
    ):
        return False
    if intent.kind is EventKind.CREATED:
        return event.content == intent.content
    return (
        event.note_id == intent.note_id
        and event.revision - 1 == intent.expected_revision
        and event.content == intent.content
        and event.tombstone_reason == intent.tombstone_reason
    )


def _verify_replayed_projection(
    connection: sqlite3.Connection,
    event: LedgerEvent,
) -> None:
    head = _load_head(connection, event.tenant_id, event.note_id)
    if head is None or head.revision < event.revision:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the command event is not represented by a valid note projection",
        )
    if event.kind is EventKind.TOMBSTONED and head != event:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "a terminal command event is not the current note head",
        )


def _insert_event(connection: sqlite3.Connection, event: LedgerEvent) -> None:
    previous_revision = None if event.revision == 1 else event.revision - 1
    connection.execute(
        """
        INSERT INTO ledger_events(
            tenant_id,
            note_id,
            revision,
            command_id,
            recorded_at_us,
            kind,
            previous_revision,
            previous_event_hash,
            event_hash,
            schema_version,
            event_bytes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.tenant_id,
            event.note_id,
            event.revision,
            event.command_id,
            event.recorded_at_us,
            event.kind.value,
            previous_revision,
            event.previous_event_hash,
            event.event_hash,
            event.schema_version,
            event.to_bytes(),
        ),
    )


def _insert_head(connection: sqlite3.Connection, event: LedgerEvent) -> None:
    connection.execute(
        """
        INSERT INTO note_heads(
            tenant_id,
            note_id,
            revision,
            event_hash,
            updated_at_us,
            is_tombstoned
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event.tenant_id,
            event.note_id,
            event.revision,
            event.event_hash,
            event.recorded_at_us,
            int(event.kind is EventKind.TOMBSTONED),
        ),
    )


def _cas_head(
    connection: sqlite3.Connection,
    *,
    previous: LedgerEvent,
    event: LedgerEvent,
) -> None:
    cursor = connection.execute(
        """
        UPDATE note_heads
        SET revision = ?,
            event_hash = ?,
            updated_at_us = ?,
            is_tombstoned = ?
        WHERE tenant_id = ?
          AND note_id = ?
          AND revision = ?
          AND event_hash = ?
          AND updated_at_us = ?
          AND is_tombstoned = 0
        """,
        (
            event.revision,
            event.event_hash,
            event.recorded_at_us,
            int(event.kind is EventKind.TOMBSTONED),
            previous.tenant_id,
            previous.note_id,
            previous.revision,
            previous.event_hash,
            previous.recorded_at_us,
        ),
    )
    if cursor.rowcount != 1:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the note head compare-and-swap did not update exactly one row",
        )


def _note_storage_exists(
    connection: sqlite3.Connection,
    tenant_id: TenantId,
    note_id: NoteId,
) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM ledger_events
            WHERE tenant_id = ? AND note_id = ?
            UNION ALL
            SELECT 1
            FROM note_heads
            WHERE tenant_id = ? AND note_id = ?
        )
        """,
        (tenant_id, note_id, tenant_id, note_id),
    ).fetchone()
    if row is None or type(row[0]) is not int or row[0] not in (0, 1):
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the note identifier inventory could not be verified",
        )
    return bool(row[0])


def _load_command_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: TenantId,
    command_id: CommandId,
) -> LedgerEvent | None:
    row = connection.execute(
        _LOAD_COMMAND_EVENT_SQL,
        (tenant_id, command_id),
    ).fetchone()
    return None if row is None else _decode_event_row(row)


def _load_event(
    connection: sqlite3.Connection,
    tenant_id: TenantId,
    note_id: NoteId,
    revision: int,
) -> LedgerEvent | None:
    row = connection.execute(
        _LOAD_EVENT_SQL,
        (tenant_id, note_id, revision),
    ).fetchone()
    return None if row is None else _decode_event_row(row)


def _load_head(
    connection: sqlite3.Connection,
    tenant_id: TenantId,
    note_id: NoteId,
) -> LedgerEvent | None:
    row = connection.execute(
        _LOAD_HEAD_SQL,
        (tenant_id, note_id, tenant_id, note_id),
    ).fetchone()
    if row is None:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the note projection probe returned no result",
        )
    if row["head_tenant_id"] is None:
        if row["latest_revision"] is not None or row["latest_event_hash"] is not None:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "ledger events exist without a note head",
            )
        return None
    return _decode_head_row(row)


def _decode_event_row(row: sqlite3.Row) -> LedgerEvent:
    raw = row["event_bytes"]
    if type(raw) is not bytes:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "stored event bytes are not an exact canonical blob",
        )
    try:
        event = decode_event(raw)
    except ContractViolation:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "stored event bytes failed canonical verification",
        ) from None
    expected_previous_revision = None if event.revision == 1 else event.revision - 1
    expected = (
        event.tenant_id,
        event.note_id,
        event.revision,
        event.command_id,
        event.recorded_at_us,
        event.kind.value,
        expected_previous_revision,
        event.previous_event_hash,
        event.event_hash,
        event.schema_version,
        event.to_bytes(),
    )
    stored = (
        row["tenant_id"],
        row["note_id"],
        row["revision"],
        row["command_id"],
        row["recorded_at_us"],
        row["kind"],
        row["previous_revision"],
        row["previous_event_hash"],
        row["event_hash"],
        row["schema_version"],
        raw,
    )
    if stored != expected:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "stored event columns do not match the canonical envelope",
        )
    return event


def _decode_head_row(row: sqlite3.Row) -> LedgerEvent:
    event = _decode_event_row(row)
    head = (
        row["head_tenant_id"],
        row["head_note_id"],
        row["head_revision"],
        row["head_event_hash"],
        row["head_updated_at_us"],
        row["head_is_tombstoned"],
    )
    expected = (
        event.tenant_id,
        event.note_id,
        event.revision,
        event.event_hash,
        event.recorded_at_us,
        int(event.kind is EventKind.TOMBSTONED),
    )
    if (
        head != expected
        or row["latest_revision"] != event.revision
        or row["latest_event_hash"] != event.event_hash
    ):
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the note head does not match the latest canonical event",
        )
    return event


def _history_in_transaction(
    connection: sqlite3.Connection,
    *,
    tenant_id: TenantId,
    note_id: NoteId,
    after_revision: int,
    limit: int,
) -> HistoryPage | None:
    head = _load_head(connection, tenant_id, note_id)
    if head is None:
        return None
    if after_revision > head.revision:
        return HistoryPage(
            tenant_id=tenant_id,
            note_id=note_id,
            events=(),
            next_after_revision=None,
        )

    include_anchor = after_revision > 0
    first_revision = after_revision if include_anchor else 1
    fetch_limit = limit + 1 + int(include_anchor)
    rows = connection.execute(
        _LOAD_HISTORY_SQL,
        (tenant_id, note_id, first_revision, fetch_limit),
    ).fetchall()
    decoded = tuple(_decode_event_row(row) for row in rows)
    if not decoded or decoded[0].revision != first_revision:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the history page is missing its required chain anchor",
        )
    _verify_event_sequence(decoded)

    candidates = decoded[1:] if include_anchor else decoded
    has_more = len(candidates) > limit
    events = candidates[:limit]
    if not has_more:
        last = events[-1] if events else decoded[0]
        if last != head:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "the final history page does not end at the current note head",
            )
    return HistoryPage(
        tenant_id=tenant_id,
        note_id=note_id,
        events=events,
        next_after_revision=events[-1].revision if has_more and events else None,
    )


def _search_in_transaction(
    connection: sqlite3.Connection,
    *,
    tenant_id: TenantId,
    query: LexicalQuery,
    limit: int,
) -> SearchResults:
    live_events, scanned_heads, content_bytes = _verified_search_corpus(
        connection,
        tenant_id,
    )

    hits: list[SearchHit] = []
    for event in live_events:
        content = cast(NoteContent, event.content)
        score = score_lexical_content(query, content)
        if score is None:
            continue
        hits.append(
            SearchHit(
                citation=SearchCitation(
                    tenant_id=event.tenant_id,
                    note_id=event.note_id,
                    revision=event.revision,
                    event_hash=event.event_hash,
                ),
                recorded_at_us=event.recorded_at_us,
                content=content,
                score=score,
            )
        )

    hits.sort(
        key=lambda hit: (
            -hit.score.total,
            -hit.recorded_at_us,
            hit.citation.note_id,
        )
    )
    return SearchResults(
        tenant_id=tenant_id,
        query=query,
        limit=limit,
        total_matches=len(hits),
        scanned_heads=scanned_heads,
        scanned_live_notes=len(live_events),
        scanned_content_bytes=content_bytes,
        hits=tuple(hits[:limit]),
    )


def _audit_fts5_candidates_in_transaction(
    connection: sqlite3.Connection,
    *,
    tenant_id: TenantId,
    query: LexicalQuery,
) -> Fts5CandidateAudit:
    live_events, scanned_heads, content_bytes = _verified_search_corpus(
        connection,
        tenant_id,
    )
    oracle_match_note_ids = tuple(
        sorted(
            event.note_id
            for event in live_events
            if score_lexical_content(query, cast(NoteContent, event.content)) is not None
        )
    )
    candidate_note_ids = _fts5_candidate_note_ids(connection, query, live_events)
    if candidate_note_ids != oracle_match_note_ids:
        raise LedgerStorageError(
            "FTS5_CANDIDATE_DRIFT",
            "the rebuilt FTS5 candidate set disagrees with the reference oracle",
        )

    runtime = connection.execute(
        "SELECT CAST(sqlite_version() AS TEXT), CAST(sqlite_source_id() AS TEXT)"
    ).fetchone()
    sqlite_version = cast(str, runtime[0])
    sqlite_source_id = cast(str, runtime[1])
    return Fts5CandidateAudit(
        tenant_id=tenant_id,
        query=query,
        schema_version=FTS5_CANDIDATE_SCHEMA_VERSION,
        tokenizer=FTS5_CANDIDATE_TOKENIZER,
        sqlite_version=sqlite_version,
        sqlite_source_id=sqlite_source_id,
        scanned_heads=scanned_heads,
        indexed_live_notes=len(live_events),
        scanned_content_bytes=content_bytes,
        oracle_match_note_ids=oracle_match_note_ids,
        candidate_note_ids=candidate_note_ids,
    )


def _fts5_candidate_note_ids(
    connection: sqlite3.Connection,
    query: LexicalQuery,
    live_events: tuple[LedgerEvent, ...],
) -> tuple[NoteId, ...]:
    connection.execute(_CREATE_FTS5_CANDIDATES_SQL)
    try:
        for event in live_events:
            streams = lexical_token_streams(cast(NoteContent, event.content))
            connection.execute(
                _INSERT_FTS5_CANDIDATE_SQL,
                (event.note_id, streams.title, streams.body, streams.tags),
            )
        rows = connection.execute(
            _SELECT_FTS5_CANDIDATES_SQL,
            (query.match_expression,),
        ).fetchall()
        values = tuple(row["note_id"] for row in rows)
    finally:
        connection.execute(_DROP_FTS5_CANDIDATES_SQL)
    return _validated_fts5_candidate_ids(values)


def _validated_fts5_candidate_ids(values: tuple[object, ...]) -> tuple[NoteId, ...]:
    if any(type(value) is not str for value in values):
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the FTS5 candidate index returned an invalid identity",
        )
    note_ids: list[NoteId] = []
    for value in cast(tuple[str, ...], values):
        try:
            _validate_identifier(value, "note_id")
        except ContractViolation:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "the FTS5 candidate index returned an invalid identity",
            ) from None
        note_ids.append(NoteId(value))
    result = tuple(note_ids)
    if result != tuple(sorted(set(result))):
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the FTS5 candidate index returned duplicate or unsorted identities",
        )
    return result


def _verified_search_corpus(
    connection: sqlite3.Connection,
    tenant_id: TenantId,
) -> tuple[tuple[LedgerEvent, ...], int, int]:
    note_ids = _load_search_note_ids(connection, tenant_id)
    _verify_search_event_inventory(connection, tenant_id, frozenset(note_ids))

    live_events: list[LedgerEvent] = []
    content_bytes = 0
    for note_id in note_ids:
        event = _load_head(connection, tenant_id, note_id)
        if event is None:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "an inventoried note head disappeared from the search snapshot",
            )
        if event.kind is EventKind.TOMBSTONED:
            continue
        if type(event.content) is not NoteContent:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "a live note head does not contain canonical note content",
            )
        title, body, tags = _validated_content_values(event.content)
        content_bytes += len(title.encode("utf-8")) + len(body.encode("utf-8"))
        content_bytes += sum(len(tag.encode("utf-8")) for tag in tags)
        if content_bytes > MAX_SEARCH_LIVE_CONTENT_BYTES:
            raise LedgerStorageError(
                "SEARCH_CORPUS_TOO_LARGE",
                "the live tenant corpus exceeds the reference search byte bound",
            )
        live_events.append(event)
    return tuple(live_events), len(note_ids), content_bytes


def _load_search_note_ids(
    connection: sqlite3.Connection,
    tenant_id: TenantId,
) -> tuple[NoteId, ...]:
    inventory_rows = connection.execute(
        _LOAD_TENANT_HEAD_IDS_SQL,
        (tenant_id, MAX_SEARCH_HEADS + 1),
    ).fetchall()
    if len(inventory_rows) > MAX_SEARCH_HEADS:
        raise LedgerStorageError(
            "SEARCH_INVENTORY_TOO_LARGE",
            "the tenant note inventory exceeds the reference search bound",
        )

    note_ids: list[NoteId] = []
    seen: set[NoteId] = set()
    for row in inventory_rows:
        value = row["note_id"]
        try:
            _validate_identifier(value, "note_id")
        except ContractViolation:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "the tenant note inventory contains an invalid identity",
            ) from None
        note_id = NoteId(value)
        if note_id in seen:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "the tenant note inventory contains a duplicate identity",
            )
        seen.add(note_id)
        note_ids.append(note_id)
    return tuple(note_ids)


def _verify_search_event_inventory(
    connection: sqlite3.Connection,
    tenant_id: TenantId,
    head_note_ids: frozenset[NoteId],
) -> None:
    event_note_cursor: str | None = None
    while True:
        if event_note_cursor is None:
            event_note_row = connection.execute(
                _LOAD_FIRST_TENANT_EVENT_NOTE_ID_SQL,
                (tenant_id,),
            ).fetchone()
        else:
            event_note_row = connection.execute(
                _LOAD_NEXT_TENANT_EVENT_NOTE_ID_SQL,
                (tenant_id, event_note_cursor),
            ).fetchone()
        if event_note_row is None:
            break
        event_note_value = event_note_row["note_id"]
        try:
            _validate_identifier(event_note_value, "note_id")
        except ContractViolation:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "the tenant event inventory contains an invalid identity",
            ) from None
        event_note_id = NoteId(event_note_value)
        if (
            event_note_cursor is not None and event_note_value <= event_note_cursor
        ) or event_note_id not in head_note_ids:
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "tenant ledger events exist outside the note head inventory",
            )
        event_note_cursor = event_note_value


def _verify_event_sequence(events: tuple[LedgerEvent, ...]) -> None:
    for previous, event in pairwise(events):
        if (
            previous.kind is EventKind.TOMBSTONED
            or event.revision != previous.revision + 1
            or event.previous_event_hash != previous.event_hash
            or event.recorded_at_us < previous.recorded_at_us
        ):
            raise LedgerStorageError(
                "DATABASE_INTEGRITY",
                "stored history does not form one continuous terminal-safe chain",
            )
