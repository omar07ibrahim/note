# Atomic transaction contract v1

This document describes the implemented Phase 2b mutation and read boundary.
It is narrower than an application authorization layer: `tenant_id` must
already have been resolved by trusted caller context. Opaque identifiers are
not credentials.

## Public operations

`SQLiteLedger` exposes six domain operations:

```python
create_note(tenant_id, command_id, content) -> TransitionResult
revise_note(tenant_id, note_id, command_id, expected_revision, content)
    -> TransitionResult
tombstone_note(tenant_id, note_id, command_id, expected_revision, reason)
    -> TransitionResult
get_note(tenant_id, note_id) -> LedgerEvent | None
get_head(tenant_id, note_id) -> LedgerEvent | None
read_history(tenant_id, note_id, after_revision=0, limit=50)
    -> HistoryPage | None
```

Arguments are keyword-only. Creation deliberately accepts neither a note ID nor
a timestamp. Storage generates the note ID with four bounded collision attempts
and reads its UTC microsecond clock exactly once, after command replay has been
excluded. Revision and tombstone timestamps are:

```text
max(current UTC microseconds, parent recorded_at_us)
```

This preserves causal ordering across a backward wall-clock adjustment without
claiming that the adjusted value is an external timestamp attestation.

`TransitionResult.replayed` is `False` for the transaction that applies a new
command and `True` when the exact stored command intent is returned. The event
itself is the durable command receipt.

## Write protocol

Every mutation follows one ordering:

```text
validate exact request types and bounds
assert a clean connection transaction state
BEGIN IMMEDIATE
recheck schema cookie, application_id, and user_version
look up (tenant_id, command_id)
    exact caller-controlled intent -> verified replay
    different intent -> IDEMPOTENCY_CONFLICT
for creation: generate a bounded-collision note ID and prove it unused
for a successor: load and verify the latest tenant/note head
    check missing, terminal, and expected-revision state
read the storage clock
derive a LedgerEvent from the verified parent
insert ledger_events
insert note_heads or compare-and-swap its full old state
re-read and reconcile the inserted event and latest head
recheck schema markers
COMMIT
require SQLite autocommit state
```

The command lookup is inside the write transaction and precedes note state,
identifier generation, and clock access. Therefore:

- a create retry returns its original generated note ID and timestamp;
- an old revise retry still returns its original revision after later changes;
- a tombstone retry returns the terminal event;
- a mismatched command cannot be reclassified by a later uniqueness error.

Exact intent comparison includes:

| Operation | Caller-controlled fields compared |
| --- | --- |
| create | tenant, command, event kind, exact title/body/ordered tags |
| revise | tenant, note, command, kind, expected parent revision, exact content |
| tombstone | tenant, note, command, kind, expected parent revision, reason |

Generated note ID, recorded time, revision, hashes, and canonical bytes are
results, not caller intent. A command ID is unique within one tenant and may be
used independently by another tenant.

## Optimistic concurrency and terminal state

Successors require an exact positive `expected_revision`. After the verified
head matches it, storage derives the successor from that decoded event. The
head update compares tenant ID, note ID, revision, event hash, timestamp, and
the live-state flag. A zero-row compare-and-swap at that point is integrity
drift, not an ordinary revision conflict.

A tombstone is terminal. New revise or tombstone commands fail without reading
the clock or adding a row. Earlier event bytes remain in history; the tombstone
contains only a bounded reason code. This is logical deletion, not physical
erasure.

## Stored-row reconciliation

Every event involved in a replay, head read, mutation, or history page is
decoded from `event_bytes` through the canonical event decoder. Storage then
compares the decoded value with all duplicated columns:

- tenant ID;
- note ID;
- revision;
- command ID;
- recorded timestamp;
- event kind;
- derived previous revision;
- previous event hash;
- event hash;
- schema version;
- exact canonical event bytes.

A head additionally must match the event identity, timestamp, hash, and
tombstone state. It must point at the latest stored revision. Events without a
head and a head behind a later event fail as `DATABASE_INTEGRITY`.

These checks detect drift in rows touched by an operation. They are not an
authenticated database proof and do not prevent a database owner from
replacing all mutually consistent state.

## Read semantics

`get_note` verifies the complete head before hiding a tombstone. It returns
`None` for absent, cross-tenant, and tombstoned notes, so the live-note surface
does not expose retained content.

`get_head` returns the verified terminal tombstone and is intended for callers
that need to distinguish logical deletion from absence.

`read_history` opens one read transaction. A nonzero cursor at or below the
current head is loaded as an overlap anchor in the same snapshot, then the page
verifies contiguous revision numbers, predecessor hashes, nondecreasing
timestamps, and terminal semantics. A public page contains at most 100 events.
`next_after_revision` is set only when another event exists. An existing note
with a cursor at or beyond its head returns an empty page; an absent tenant/note
returns `None`.

History is a privileged storage surface because it includes content retained
before a tombstone. A future adapter must authorize it separately from
`get_note`.

## Failure-state protocol

SQLite's low-level autocommit state is checked before `BEGIN`, after rollback,
and after `COMMIT`.

- One outer failure guard spans the call that starts each transaction, body
  dispatch, replay rollback, commit call, terminal-state proof, and result
  delivery. `BaseException` paths such as `KeyboardInterrupt` therefore use
  the same fail-closed state machine as ordinary failures.
- Failure settlement marks the object poisoned before it probes or rolls back;
  it clears that provisional poison only after SQLite proves a clean rollback.
  An exception inside settlement therefore cannot expose an active,
  apparently reusable connection.
- A known pre-commit failure is returned only after rollback is proven.
- A failed rollback poisons the connection.
- For a write transition, a failed or interrupted `COMMIT` with no active
  transaction has an unknown durable outcome and poisons the connection. A
  write `COMMIT` that returns while a transaction still appears active is also
  outcome-unknown.
- A history transaction is read-only. If its interrupted `COMMIT` is followed
  by proof of a clean inactive state, the original exception propagates and
  the connection remains reusable; an unprovable or contradictory state still
  poisons it.
- A poisoned ledger rejects status, reads, and writes. Only `close()` remains
  valid.

After closing a poisoned object, the caller opens a new ledger and retries the
same command. If the prior commit succeeded, exact replay returns the stored
event. If it rolled back, the new transaction applies it once.

Errors use bounded codes and messages. They do not contain SQL text, SQLite
diagnostics, local paths, identifiers, or note content.

## Stable domain codes

Malformed event-domain values and exhausted event revisions raise
`ContractViolation`; exact value bounds and canonical semantics are specified
in the [event contract](event-contract.md). Its current machine-readable codes
are:

```text
BLANK_TEXT
DUPLICATE_COMMAND_ID
DUPLICATE_JSON_KEY
DUPLICATE_TAG
EVENT_HASH_MISMATCH
EVENT_TOO_LARGE
INVALID_CHAIN_LINK
INVALID_CHAIN_ROOT
INVALID_CREATED_PAYLOAD
INVALID_EVENT_BYTES
INVALID_EVENT_HASH
INVALID_EVENT_JSON
INVALID_EVENT_STATE
INVALID_EVENT_UTF8
INVALID_FIELD_TYPE
INVALID_IDENTIFIER
INVALID_KIND
INVALID_OBJECT
INVALID_OBJECT_KEYS
INVALID_RECORDED_AT
INVALID_REVISED_PAYLOAD
INVALID_REVISION
INVALID_SCHEMA_VERSION
INVALID_TAGS
INVALID_TEXT
INVALID_TOMBSTONE_PAYLOAD
INVALID_TOMBSTONE_REASON
INVALID_UNICODE
NON_CANONICAL_EVENT
NON_MONOTONIC_TIME
NOTE_TOMBSTONED
REVISION_EXHAUSTED
TEXT_TOO_LARGE
TOO_MANY_TAGS
```

Storage request bounds, state outcomes, and storage failures raise
`LedgerStorageError`.

Storage input and domain outcomes include:

```text
INVALID_EXPECTED_REVISION
INVALID_HISTORY_CURSOR
INVALID_HISTORY_LIMIT
CLOCK_UNAVAILABLE
CLOCK_OUT_OF_RANGE
ID_GENERATION_FAILED
ID_GENERATION_EXHAUSTED
NOTE_NOT_FOUND
NOTE_TOMBSTONED
REVISION_CONFLICT
IDEMPOTENCY_CONFLICT
```

Storage/failure outcomes include:

```text
LEDGER_BUSY
DATABASE_FULL
DATABASE_READ_ONLY
DATABASE_INTEGRITY
DATABASE_OPERATION_FAILED
ROLLBACK_FAILED
TRANSACTION_STATE_UNCERTAIN
COMMIT_OUTCOME_UNKNOWN
CONNECTION_POISONED
```

Callers should branch on `error.code`, not message text. No code is an
authorization decision or evidence that physical deletion occurred.
