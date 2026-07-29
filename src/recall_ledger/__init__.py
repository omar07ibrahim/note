"""Public contract for RecallLedger's tenant-scoped event stream."""

from .events import (
    CommandId,
    ContractViolation,
    EventKind,
    LedgerEvent,
    NoteContent,
    NoteId,
    TenantId,
    TombstoneReason,
    decode_event,
    new_command_id,
    new_note_id,
    new_tenant_id,
)
from .storage import (
    HistoryPage,
    LedgerStorageError,
    SQLiteLedger,
    StorageStatus,
    TransitionResult,
)

__all__ = [
    "CommandId",
    "ContractViolation",
    "EventKind",
    "HistoryPage",
    "LedgerEvent",
    "LedgerStorageError",
    "NoteContent",
    "NoteId",
    "SQLiteLedger",
    "StorageStatus",
    "TenantId",
    "TombstoneReason",
    "TransitionResult",
    "decode_event",
    "new_command_id",
    "new_note_id",
    "new_tenant_id",
]
