"""Static public-API probe, checked against source and installed wheels."""

from typing import assert_type

from recall_ledger import (
    CommandId,
    HistoryPage,
    LedgerEvent,
    LedgerStorageError,
    LexicalQuery,
    LexicalScore,
    LexicalTokenStreams,
    NoteContent,
    NoteId,
    SQLiteLedger,
    StorageStatus,
    TenantId,
    TombstoneReason,
    TransitionResult,
    compile_lexical_query,
    decode_event,
    lexical_token_streams,
    score_lexical_content,
)

event = LedgerEvent.create(
    tenant_id=TenantId("tn_0123456789abcdef0123456789abcdef"),
    note_id=NoteId("nt_fedcba9876543210fedcba9876543210"),
    command_id=CommandId("cmd_00000000000000000000000000000001"),
    recorded_at_us=1,
    content=NoteContent("title", "body"),
)

assert_type(event, LedgerEvent)
assert_type(event.to_bytes(), bytes)
assert_type(decode_event(event.to_bytes()), LedgerEvent)
assert event.content is not None
query = compile_lexical_query("stored title")
assert_type(query, LexicalQuery)
assert_type(lexical_token_streams(event.content), LexicalTokenStreams)
assert_type(score_lexical_content(query, event.content), LexicalScore | None)


def check_storage_api(data_directory: str) -> None:
    try:
        ledger = SQLiteLedger.open(data_directory, busy_timeout_ms=10)
    except LedgerStorageError as error:
        assert_type(error.code, str)
        return
    assert_type(ledger, SQLiteLedger)
    assert_type(ledger.status(), StorageStatus)
    created = ledger.create_note(
        tenant_id=TenantId("tn_0123456789abcdef0123456789abcdef"),
        command_id=CommandId("cmd_00000000000000000000000000000002"),
        content=NoteContent("stored title", "stored body"),
    )
    assert_type(created, TransitionResult)
    assert_type(created.event, LedgerEvent)
    assert_type(created.replayed, bool)
    revised = ledger.revise_note(
        tenant_id=created.event.tenant_id,
        note_id=created.event.note_id,
        command_id=CommandId("cmd_00000000000000000000000000000003"),
        expected_revision=created.event.revision,
        content=NoteContent("revised title", "revised body"),
    )
    assert_type(revised, TransitionResult)
    deleted = ledger.tombstone_note(
        tenant_id=revised.event.tenant_id,
        note_id=revised.event.note_id,
        command_id=CommandId("cmd_00000000000000000000000000000004"),
        expected_revision=revised.event.revision,
        reason=TombstoneReason.USER_REQUEST,
    )
    assert_type(deleted, TransitionResult)
    assert_type(
        ledger.get_note(
            tenant_id=deleted.event.tenant_id,
            note_id=deleted.event.note_id,
        ),
        LedgerEvent | None,
    )
    assert_type(
        ledger.get_head(
            tenant_id=deleted.event.tenant_id,
            note_id=deleted.event.note_id,
        ),
        LedgerEvent | None,
    )
    history = ledger.read_history(
        tenant_id=deleted.event.tenant_id,
        note_id=deleted.event.note_id,
    )
    assert_type(history, HistoryPage | None)
    ledger.close()
