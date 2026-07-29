"""Static public-API probe, checked against source and installed wheels."""

from typing import assert_type

from recall_ledger import (
    CommandId,
    LedgerEvent,
    NoteContent,
    NoteId,
    TenantId,
    decode_event,
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
