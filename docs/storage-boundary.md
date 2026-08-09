# SQLite storage boundary v1

RecallLedger's durable-storage boundary defines how a local SQLite database is
identified, migrated, opened, checked, and updated through atomic note
transitions. It exposes tenant-scoped head/history reads, a bounded lexical
reference search, and an ephemeral FTS5 candidate audit, but no persistent
retrieval index, projection rebuild, network service, or authorization adapter.

This module currently supports Linux/POSIX only. It depends on `fcntl`,
`flock`, `O_DIRECTORY`, `O_NOFOLLOW`, and `register_at_fork`; Windows is not a
supported storage target.

## Deployment path contract

The caller supplies one absolute deployment data directory. RecallLedger uses
only two fixed basenames inside it:

```text
.recall-ledger.lock
recall-ledger.sqlite3
```

The directory must already exist, be owned by the effective user, and have
mode `0700`. The database and lock must be owner-owned regular files with mode
`0600` and exactly one hard link. Existing SQLite `-wal`, `-shm`, and
`-journal` sidecars receive the same checks. Symlinks, directories, FIFOs,
devices, sockets, hard links, and group/other-accessible files fail closed.
The lock and a newly absent database are created with `O_EXCL`, `O_NOFOLLOW`,
and mode `0600`.

The directory is trusted deployment configuration, never a request field.
Python's standard `sqlite3` module cannot open SQLite from a prevalidated file
descriptor or request `SQLITE_OPEN_NOFOLLOW`. RecallLedger uses directory-
relative, no-following metadata checks and verifies pathname identity around
`sqlite3.connect`. It never raw-opens an existing database or SQLite sidecar:
closing such a descriptor can cancel POSIX locks owned by other SQLite
connections in the same process. A newly created empty database descriptor is
closed before SQLite connects. A process-wide lifecycle mutex serializes opens
through resource registration, so another `SQLiteLedger` cannot enter that
creation window. This boundary does not claim to defeat a hostile same-UID or
root process, an in-process component that bypasses `SQLiteLedger`, or an
attacker who can rename a writable ancestor directory. The operator must
control the configured directory and its ancestors.

## Cooperative runtime lock

Every runtime holds a shared `flock` on the fixed lock file for the lifetime of
its connection. During first-time initialization, the discovery connection is
cleanly closed before the shared lock is released. RecallLedger then acquires a
nonblocking exclusive lock, opens a migration connection, rechecks that the
database is still empty inside `BEGIN IMMEDIATE`, and applies the migration.
That connection is cleanly closed before the exclusive lock is released; only
then does the runtime reacquire a shared lock and open its final connection.
An uncertain close retains the current lock and connection in a process-local
quarantine instead of crossing a lock transition.

The lock is cooperative. A process that ignores it and edits the database
directly is outside the writer API, although schema-cookie, exact-schema,
foreign-key, canonical-event, latest-head, and projection checks detect
involved drift. A failed lock acquisition is bounded and does not retry
forever.

## Format and migration identity

SQLite `application_id` is the integer encoding of `RCLD`; `user_version` is
storage schema version 1. These are format discriminators, not authentication
or integrity proofs.

An empty database is initialized only when all three facts hold:

- `application_id = 0`;
- `user_version = 0`;
- `sqlite_schema` contains no application objects.

A nonempty unclaimed database, foreign application ID, missing/old migration,
future schema, unexpected object, altered SQL definition, checksum mismatch,
failed `quick_check`, or foreign-key violation is rejected. Migration
statements execute one at a time—never through `executescript`—inside an
explicit transaction. The stored migration row contains a domain-separated
SHA-256 of the exact ordered SQL payload. Application ID and user version are
written last, the complete schema is checked before commit, and failures
explicitly roll back.

The current schema contains:

- append-only-intended `ledger_events` rows keyed by
  `(tenant_id, note_id, revision)`;
- tenant-wide command uniqueness through `(tenant_id, command_id)`;
- an exact predecessor foreign key for non-root events;
- a bounded nonempty event-byte slot plus relational identity/index columns;
- one content-free `note_heads` projection pointer per tenant/note;
- deterministic keyset-pagination indexes for live and all-note views;
- a closed `schema_migrations` ledger.

The tables are `STRICT` and `WITHOUT ROWID`. The transaction layer derives
events from a storage-loaded head, validates canonical bytes against every
duplicated column, inserts an event, and inserts or compare-and-swaps the head
in one `BEGIN IMMEDIATE` transaction. Tenant-wide command lookup precedes note
ID generation, clock reads, and current-state checks, so an exact retry returns
the original event even after the head advances. Direct SQL by a process that
ignores the API remains outside the invariant boundary; rows are
append-only-intended, not protected from the database owner by SQLite
permissions.

## Bounded reference search

`SQLiteLedger.search_notes` is a dependency-free correctness oracle, not an
index. It accepts exact raw query text, compiles the versioned Unicode profile
inside the storage API, and opens one deferred read transaction. Within that
single snapshot it:

1. enumerates every tenant head, including tombstones, through
   `note_heads_all_page` with a 1,001-row sentinel query;
2. walks distinct tenant event note IDs with bounded keyset seeks over the
   event primary-key prefix and rejects events without a head;
3. loads every inventoried head through the canonical latest-head verifier;
4. excludes a tombstone only after the decoded event and duplicated head flag
   agree;
5. accounts for exact UTF-8 bytes of every live title, body, and tag;
6. applies the deterministic integer scorer to every live head; and
7. sorts matches by score descending, current-head time descending, then note
   ID ascending before applying the caller's top-K limit.

The reference scan is capped at 1,000 total heads and 16 MiB of aggregate live
content. Those caps and retrieval normalization/token-stream bounds fail the
whole operation; no partial prefix is returned. Results expose the normalized
query profile, total matches, scan accounting, score breakdown, exact content,
and a citation containing tenant ID, note ID, current revision, and current
event hash. A citation is a consistency reference, not a signature or an
authorization receipt.

Only current heads are searched. Earlier revisions remain available through
privileged history but cannot contribute stale terms; a terminal tombstone
hides the note from search without erasing retained events. Another tenant's
heads, history volume, terms, or scores do not enter the scan and cannot alter
ranking. Current-head reconciliation is not a full replay of every retained
chain. The orphan keyset proof performs at most one indexed seek per distinct
event note plus a terminal seek, rather than scanning every revision.

`SQLiteLedger.audit_fts5_candidates` is the first index-calibration boundary.
Inside one deferred transaction it reuses the fully verified live corpus,
rebuilds a TEMP FTS5 table from inert encoded title/body/tag token streams, runs
the compiled quoted all-terms expression, and compares the complete ordered
candidate identities with a separately scored oracle set. The table uses the
`ascii` tokenizer with `detail=none` and `columnsize=0`, is dropped before
the transaction completes, and never alters the durable schema. Candidate
drift raises `FTS5_CANDIDATE_DRIFT`; unavailable FTS5 or SQLite failures are
sanitized by the existing storage settlement boundary.

This audit is not serving acceleration, a persistent projection, a benchmark,
or a compatibility claim for arbitrary SQLite builds. A future durable index
must persist the lexical contract version and Unicode profile, rebuild or fail
closed on mismatch, and pass candidates through the exact scorer. It must
remain observationally identical for hits, scores, ordering, revision
replacement, tombstones, and tenant-stuffing invariance.

The installed operator CLI exposes this exact path through `search
--query-file PATH|- [--limit COUNT] [--jsonl]`. Query bytes come from a bounded
strict UTF-8 regular file or standard input, never a raw argv value, and are
not trimmed. Compact or pretty JSON contains ranked hits plus scan accounting;
JSONL emits each ranked hit followed by one summary. The top-K limit never
reduces the complete integrity scan. Successful output includes full current
note content and reversible encoded query terms, so callers must treat it as a
privileged local surface rather than an authorization boundary.

The current CLI materializes the complete result after closing the ledger;
JSONL does not imply streaming, and JSON escaping can expand the bounded raw
corpus substantially. No small-memory or small-output claim is made.

## Connection profile

Every connection is autocommit-mode with manual transaction control,
`check_same_thread=True`, and URI parsing disabled. It is bound to the opening
process and thread. Reuse after `fork`, cross-thread use, or use after close
fails before a database operation.

Open ledger objects must be closed before `fork`. SQLite explicitly forbids
using or even calling `sqlite3_close()` on a parent-opened connection in the
child. RecallLedger's child hook therefore makes an accidentally inherited
handle inaccessible, retains it in a process-local quarantine without invoking
SQLite, and closes only RecallLedger's separate lock and directory anchors.
The child must then immediately `exec` or call `os._exit`; continuing Python,
garbage collection of the quarantine, `sys.exit`, and normal interpreter
teardown are unsupported. Prefer multiprocessing's `spawn` start method.

The following profile is set and read back:

```text
foreign_keys      ON
trusted_schema    OFF
cell_size_check   ON
mmap_size         0
read_uncommitted  OFF
locking_mode      NORMAL
journal_mode      DELETE
synchronous       FULL
busy_timeout      bounded: 0..60000 ms
```

SQLite runtime limits disable attached databases and worker threads, set
trigger depth to zero, and cap SQL length, values, columns, parameters,
compound selects, and expression depth. Where Python exposes SQLite defensive
configuration, RecallLedger also enables defensive mode and disables trusted
schema, double-quoted string literals, extension loading, writable schema,
triggers, and views.

Rollback-journal `DELETE` mode is intentional. SQLite
[disclosed a rare multi-connection WAL-reset corruption race](https://www.sqlite.org/wal.html#the_wal_reset_bug)
affecting 3.7.0 through 3.51.2, with only specific patched backports. DELETE
mode preserves the broader SQLite 3.37+ stdlib contract without pretending the
current runtime is patched.
`FULL` synchronous mode relies on the operating system, filesystem, and
hardware honoring sync requests; it is not a power-loss attestation.

Schema verification includes `quick_check` and `foreign_key_check`, so opening
cost grows with the ledger. `SQLiteLedger` is intended to be long-lived; a
future explicit maintenance API will separate deep scans from routine opens.
The same process serializes open, close, finalizer ownership transfer, and fork
snapshots, so a slow open delays those lifecycle operations rather than
exposing an unregistered SQLite handle.

Explicit context-manager or `close()` ownership is required. Garbage-collection
cleanup is only a last-resort safety net: it never waits on a lifecycle
operation already in progress, and it retains a safe process-lifetime
quarantine when ownership or connection closure is uncertain.

A transition returns only after `COMMIT` completes and SQLite reports no active
transaction. A failed pre-commit operation is rolled back and the inactive
state is verified. If rollback cannot be proven, or a failed/interrupting
`COMMIT` leaves the outcome unknown, the ledger object is poisoned: every
operation except `close()` fails. The caller must close it, open a new
connection, and retry the same command ID; exact idempotency then resolves
whether the original transaction committed. Connection poisoning does not
claim to repair an underlying disk, filesystem, or SQLite failure.

## Data and deletion non-claims

The event table is designed to retain earlier event bytes after a logical
tombstone. SQLite pages, WAL, journals, backups, replicas, filesystem
snapshots, storage media, and process memory may retain older note content.
Neither this storage foundation nor a future tombstone is physical erasure.

Application IDs, migration hashes, event hashes, and schema fingerprints are
unsigned consistency relationships. They do not authenticate Omar, authorize a
tenant, attest the host, prevent a database owner from replacing all related
state, or create an authenticated latest checkpoint.

## Reproduce the current gate

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --editable '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
```

The tests exercise fresh initialization and reopen, exact migration/profile
checks, cooperative-lock behavior, unsafe file types and permissions, foreign
and future databases, migration rollback, atomic transitions, exact replay and
conflict classification, tenant isolation, competing writers, clock rollback,
tombstones, history boundaries, row/head corruption, uncertain transaction
outcomes, thread/process ownership, safe error surfaces, and every
source/branch path.