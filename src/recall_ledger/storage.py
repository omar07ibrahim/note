"""Versioned, fail-closed SQLite storage for RecallLedger.

The data directory is deployment configuration, not request input.  This
module uses a fixed database basename inside an absolute, operator-owned 0700
directory.  Python's stdlib sqlite3 API cannot open from a caller-supplied file
descriptor or request SQLITE_OPEN_NOFOLLOW, so hostile same-UID processes and
untrusted writable ancestor directories remain outside this boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import stat
import textwrap
import threading
import warnings
import weakref
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from itertools import count
from pathlib import Path
from types import TracebackType
from typing import Final, Self, cast

from .events import (
    MAX_EVENT_BYTES,
    MAX_RECORDED_AT_US,
    MAX_REVISION,
    CommandId,
    EventKind,
    LedgerEvent,
    NoteContent,
    NoteId,
    TenantId,
    TombstoneReason,
)

DATABASE_FILENAME: Final = "recall-ledger.sqlite3"
LOCK_FILENAME: Final = ".recall-ledger.lock"
APPLICATION_ID: Final = 0x52434C44  # ASCII "RCLD"; a format marker, not authentication.
STORAGE_SCHEMA_VERSION: Final = 1
MINIMUM_SQLITE_VERSION: Final = (3, 37, 0)
DEFAULT_BUSY_TIMEOUT_MS: Final = 5_000
MAX_BUSY_TIMEOUT_MS: Final = 60_000
DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100
_DIRECTORY_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_SQLITE_SYNCHRONOUS_FULL: Final = 2

_DATABASE_SIDECARS: Final = (
    f"{DATABASE_FILENAME}-journal",
    f"{DATABASE_FILENAME}-shm",
    f"{DATABASE_FILENAME}-wal",
)
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS: Final = os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
_MIGRATION_DOMAIN: Final = b"recall-ledger:sqlite-migration:v1\x00"
_LIVE_RESOURCES: dict[int, _RegisteredLedger]  # assigned after the class definition
_FORK_SNAPSHOT: tuple[_RegisteredLedger, ...]
_FORK_QUARANTINE: tuple[_RegisteredLedger, ...]
_FAILED_CLOSE_QUARANTINE: list[tuple[sqlite3.Connection, int, int]] = []
_LIFECYCLE_LOCK: Final = threading.Lock()
_REGISTRATION_IDS: Final = count(1)
_REGISTRY_LOCK: Final = threading.Lock()


class LedgerStorageError(RuntimeError):
    """A bounded storage failure that never includes SQLite text or local paths."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _DatabaseState(Enum):
    EMPTY = "empty"
    CURRENT = "current"


class _WriteTransactionPhase(Enum):
    BEFORE_BEGIN = "before_begin"
    ACTIVE = "active"
    COMMIT_CALL = "commit_call"
    COMMIT_RETURNED = "commit_returned"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class StorageStatus:
    """Non-sensitive facts asserted for the current connection."""

    application_id: int
    schema_version: int
    journal_mode: str
    synchronous: str
    foreign_keys: bool
    trusted_schema: bool
    cell_size_check: bool
    mmap_size: int
    read_uncommitted: bool
    locking_mode: str
    busy_timeout_ms: int
    sqlite_version: str


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """A committed event and whether it came from exact command replay."""

    event: LedgerEvent
    replayed: bool


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One bounded, chain-verified history page."""

    tenant_id: TenantId
    note_id: NoteId
    events: tuple[LedgerEvent, ...]
    next_after_revision: int | None


@dataclass(frozen=True, slots=True)
class _TransitionIntent:
    """Validated inputs for one tenant-scoped state transition."""

    tenant_id: TenantId
    note_id: NoteId | None
    command_id: CommandId
    kind: EventKind
    expected_revision: int | None
    content: NoteContent | None
    tombstone_reason: TombstoneReason | None


@dataclass(frozen=True, slots=True)
class _OpenResources:
    connection: sqlite3.Connection
    directory: Path
    directory_fd: int
    lock_fd: int
    schema_cookie: int


@dataclass(frozen=True, slots=True)
class _RegisteredLedger:
    ledger_ref: weakref.ReferenceType[SQLiteLedger]
    connection: sqlite3.Connection
    directory_fd: int
    lock_fd: int


def _sql(value: str) -> str:
    return textwrap.dedent(value).strip()


_CREATE_MIGRATIONS = _sql(
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        name TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL UNIQUE
            CHECK (
                length(sha256) = 71
                AND substr(sha256, 1, 7) = 'sha256:'
                AND substr(sha256, 8) NOT GLOB '*[^0-9a-f]*'
            )
    ) STRICT
    """
)

_CREATE_EVENTS = _sql(
    f"""
    CREATE TABLE ledger_events (
        tenant_id TEXT NOT NULL
            CHECK (
                length(tenant_id) = 35
                AND substr(tenant_id, 1, 3) = 'tn_'
                AND substr(tenant_id, 4) NOT GLOB '*[^0-9a-f]*'
            ),
        note_id TEXT NOT NULL
            CHECK (
                length(note_id) = 35
                AND substr(note_id, 1, 3) = 'nt_'
                AND substr(note_id, 4) NOT GLOB '*[^0-9a-f]*'
            ),
        revision INTEGER NOT NULL
            CHECK (revision BETWEEN 1 AND {MAX_REVISION}),
        command_id TEXT NOT NULL
            CHECK (
                length(command_id) = 36
                AND substr(command_id, 1, 4) = 'cmd_'
                AND substr(command_id, 5) NOT GLOB '*[^0-9a-f]*'
            ),
        recorded_at_us INTEGER NOT NULL
            CHECK (recorded_at_us BETWEEN 0 AND {MAX_RECORDED_AT_US}),
        kind TEXT NOT NULL
            CHECK (kind IN ('note.created', 'note.revised', 'note.tombstoned')),
        previous_revision INTEGER,
        previous_event_hash TEXT,
        event_hash TEXT NOT NULL
            CHECK (
                length(event_hash) = 71
                AND substr(event_hash, 1, 7) = 'sha256:'
                AND substr(event_hash, 8) NOT GLOB '*[^0-9a-f]*'
            ),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        event_bytes BLOB NOT NULL
            CHECK (
                typeof(event_bytes) = 'blob'
                AND length(event_bytes) BETWEEN 1 AND {MAX_EVENT_BYTES}
            ),
        PRIMARY KEY (tenant_id, note_id, revision),
        UNIQUE (tenant_id, command_id),
        UNIQUE (tenant_id, note_id, revision, event_hash),
        CHECK (
            (
                revision = 1
                AND kind = 'note.created'
                AND previous_revision IS NULL
                AND previous_event_hash IS NULL
            )
            OR
            (
                revision > 1
                AND kind IN ('note.revised', 'note.tombstoned')
                AND previous_revision = revision - 1
                AND previous_event_hash IS NOT NULL
            )
        ),
        FOREIGN KEY (
            tenant_id, note_id, previous_revision, previous_event_hash
        ) REFERENCES ledger_events (
            tenant_id, note_id, revision, event_hash
        ) ON DELETE RESTRICT
    ) STRICT, WITHOUT ROWID
    """
)

_CREATE_HEADS = _sql(
    """
    CREATE TABLE note_heads (
        tenant_id TEXT NOT NULL,
        note_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        event_hash TEXT NOT NULL,
        updated_at_us INTEGER NOT NULL,
        is_tombstoned INTEGER NOT NULL CHECK (is_tombstoned IN (0, 1)),
        PRIMARY KEY (tenant_id, note_id),
        FOREIGN KEY (
            tenant_id, note_id, revision, event_hash
        ) REFERENCES ledger_events (
            tenant_id, note_id, revision, event_hash
        ) ON DELETE RESTRICT
    ) STRICT, WITHOUT ROWID
    """
)

_CREATE_LIVE_PAGE_INDEX = _sql(
    """
    CREATE INDEX note_heads_live_page
    ON note_heads (tenant_id, is_tombstoned, updated_at_us DESC, note_id ASC)
    """
)

_CREATE_ALL_PAGE_INDEX = _sql(
    """
    CREATE INDEX note_heads_all_page
    ON note_heads (tenant_id, updated_at_us DESC, note_id ASC)
    """
)

_MIGRATION_STATEMENTS: Final = (
    _CREATE_MIGRATIONS,
    _CREATE_EVENTS,
    _CREATE_HEADS,
    _CREATE_LIVE_PAGE_INDEX,
    _CREATE_ALL_PAGE_INDEX,
)
_MIGRATION_PAYLOAD: Final = b"\n-- recall-ledger statement boundary --\n".join(
    statement.encode("utf-8") for statement in _MIGRATION_STATEMENTS
)
_MIGRATION_DIGEST: Final = (
    "sha256:" + hashlib.sha256(_MIGRATION_DOMAIN + _MIGRATION_PAYLOAD).hexdigest()
)
_MIGRATION_NAME: Final = "initialize_transactional_event_ledger"

_EXPECTED_SCHEMA: Final = tuple(
    sorted(
        (
            ("index", "note_heads_all_page", "note_heads", _CREATE_ALL_PAGE_INDEX),
            ("index", "note_heads_live_page", "note_heads", _CREATE_LIVE_PAGE_INDEX),
            (
                "index",
                "sqlite_autoindex_ledger_events_2",
                "ledger_events",
                None,
            ),
            (
                "index",
                "sqlite_autoindex_ledger_events_3",
                "ledger_events",
                None,
            ),
            (
                "index",
                "sqlite_autoindex_schema_migrations_1",
                "schema_migrations",
                None,
            ),
            (
                "index",
                "sqlite_autoindex_schema_migrations_2",
                "schema_migrations",
                None,
            ),
            ("table", "ledger_events", "ledger_events", _CREATE_EVENTS),
            ("table", "note_heads", "note_heads", _CREATE_HEADS),
            (
                "table",
                "schema_migrations",
                "schema_migrations",
                _CREATE_MIGRATIONS,
            ),
        )
    )
)


class SQLiteLedger:
    """One thread-bound connection to the fixed RecallLedger SQLite database."""

    __slots__ = (
        "__weakref__",
        "_connection",
        "_directory",
        "_directory_fd",
        "_finalizer",
        "_fork_invalidated",
        "_lock_fd",
        "_owner_pid",
        "_owner_thread",
        "_poisoned",
        "_registry_token",
        "_schema_cookie",
    )

    def __init__(
        self,
        *,
        resources: _OpenResources,
    ) -> None:
        self._connection: sqlite3.Connection | None = resources.connection
        self._directory = resources.directory
        self._directory_fd = resources.directory_fd
        self._lock_fd = resources.lock_fd
        self._fork_invalidated = False
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._poisoned = False
        self._registry_token = next(_REGISTRATION_IDS)
        self._schema_cookie = resources.schema_cookie
        with _REGISTRY_LOCK:
            _LIVE_RESOURCES[self._registry_token] = _RegisteredLedger(
                ledger_ref=weakref.ref(self),
                connection=resources.connection,
                directory_fd=resources.directory_fd,
                lock_fd=resources.lock_fd,
            )
        try:
            self._finalizer = weakref.finalize(
                self,
                _finalize_resources,
                self._registry_token,
            )
        except BaseException:
            with _REGISTRY_LOCK:
                _LIVE_RESOURCES.pop(self._registry_token, None)
            raise

    @classmethod
    def open(
        cls,
        data_directory: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> Self:
        """Open or initialize the ledger inside a trusted absolute directory."""

        directory = _validate_directory_argument(data_directory)
        timeout = _validate_busy_timeout(busy_timeout_ms)
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            raise LedgerStorageError(
                "SQLITE_TOO_OLD",
                "SQLite 3.37 or newer is required for the storage schema",
            )

        with _LIFECYCLE_LOCK:
            return cls._open_locked(directory, timeout)

    @classmethod
    def _open_locked(cls, directory: Path, timeout: int) -> Self:
        directory_fd = _open_directory(directory)
        lock_fd = -1
        connection: sqlite3.Connection | None = None
        try:
            lock_fd = _open_owned_regular(
                directory_fd,
                LOCK_FILENAME,
                create_if_missing=True,
            )
            _acquire_lock(lock_fd, fcntl.LOCK_SH, "LEDGER_BUSY")
            before_identity = _prepare_database(directory_fd)
            _inspect_sidecars(directory_fd)
            connection = _connect(directory / DATABASE_FILENAME, timeout)
            _assert_path_identity(directory_fd, DATABASE_FILENAME, before_identity)
            _configure_connection(connection, timeout)
            state = _classify_database(connection)
            if state is _DatabaseState.EMPTY:
                _close_before_lock_transition(connection)
                connection = None
                _unlock(lock_fd)
                _acquire_lock(lock_fd, fcntl.LOCK_EX, "MIGRATION_BUSY")

                connection = _connect(directory / DATABASE_FILENAME, timeout)
                _assert_path_identity(
                    directory_fd,
                    DATABASE_FILENAME,
                    before_identity,
                )
                _configure_connection(connection, timeout)
                _initialize_empty_database(connection)
                _close_before_lock_transition(connection)
                connection = None
                _unlock(lock_fd)
                _acquire_lock(lock_fd, fcntl.LOCK_SH, "LEDGER_BUSY")

                connection = _connect(directory / DATABASE_FILENAME, timeout)
                _assert_path_identity(
                    directory_fd,
                    DATABASE_FILENAME,
                    before_identity,
                )
                _configure_connection(connection, timeout)
                _verify_schema(connection)
            else:
                _verify_schema(connection)
            _configure_rollback_journal(connection)
            _assert_path_identity(directory_fd, DATABASE_FILENAME, before_identity)
            _inspect_sidecars(directory_fd)
            schema_cookie = _pragma_int(connection, "schema_version")
            return cls(
                resources=_OpenResources(
                    connection=connection,
                    directory=directory,
                    directory_fd=directory_fd,
                    lock_fd=lock_fd,
                    schema_cookie=schema_cookie,
                )
            )
        except BaseException:
            if connection is not None and _close_connection(connection):
                _FAILED_CLOSE_QUARANTINE.append((connection, lock_fd, directory_fd))
                raise LedgerStorageError(
                    "DATABASE_CLOSE_FAILED",
                    "the failed open left the SQLite connection quarantined",
                ) from None
            _close_fd(lock_fd)
            _close_fd(directory_fd)
            raise

    def status(self) -> StorageStatus:
        """Return the asserted connection profile without exposing its path."""

        connection = self._ready_connection()
        self._assert_schema_cookie(connection)
        return _connection_status(connection)

    def create_note(
        self,
        *,
        tenant_id: TenantId,
        command_id: CommandId,
        content: NoteContent,
    ) -> TransitionResult:
        """Atomically create one tenant-owned note or replay the exact command."""

        connection = self._ready_connection()
        from . import _ledger_operations as operations  # noqa: PLC0415

        intent = operations._create_intent(
            tenant_id=tenant_id,
            command_id=command_id,
            content=content,
        )
        self._assert_schema_cookie(connection)
        return self._apply_transition(connection, intent)

    def revise_note(
        self,
        *,
        tenant_id: TenantId,
        note_id: NoteId,
        command_id: CommandId,
        expected_revision: int,
        content: NoteContent,
    ) -> TransitionResult:
        """Atomically revise a live note at one exact expected revision."""

        connection = self._ready_connection()
        from . import _ledger_operations as operations  # noqa: PLC0415

        intent = operations._revise_intent(
            tenant_id=tenant_id,
            note_id=note_id,
            command_id=command_id,
            expected_revision=expected_revision,
            content=content,
        )
        self._assert_schema_cookie(connection)
        return self._apply_transition(connection, intent)

    def tombstone_note(
        self,
        *,
        tenant_id: TenantId,
        note_id: NoteId,
        command_id: CommandId,
        expected_revision: int,
        reason: TombstoneReason,
    ) -> TransitionResult:
        """Atomically append a content-free terminal tombstone."""

        connection = self._ready_connection()
        from . import _ledger_operations as operations  # noqa: PLC0415

        intent = operations._tombstone_intent(
            tenant_id=tenant_id,
            note_id=note_id,
            command_id=command_id,
            expected_revision=expected_revision,
            reason=reason,
        )
        self._assert_schema_cookie(connection)
        return self._apply_transition(connection, intent)

    def get_note(
        self,
        *,
        tenant_id: TenantId,
        note_id: NoteId,
    ) -> LedgerEvent | None:
        """Load a live head; absent, foreign, and tombstoned notes return None."""

        connection = self._ready_connection()
        from . import _ledger_operations as operations  # noqa: PLC0415

        operations._validate_tenant_note(tenant_id, note_id)
        self._assert_schema_cookie(connection)
        try:
            event = operations._load_head(connection, tenant_id, note_id)
        except sqlite3.Error as error:
            raise _mapped_database_error(error) from None
        if event is not None and event.kind is EventKind.TOMBSTONED:
            return None
        return event

    def get_head(
        self,
        *,
        tenant_id: TenantId,
        note_id: NoteId,
    ) -> LedgerEvent | None:
        """Load the verified current head, including a terminal tombstone."""

        connection = self._ready_connection()
        from . import _ledger_operations as operations  # noqa: PLC0415

        operations._validate_tenant_note(tenant_id, note_id)
        self._assert_schema_cookie(connection)
        try:
            return operations._load_head(connection, tenant_id, note_id)
        except sqlite3.Error as error:
            raise _mapped_database_error(error) from None

    def read_history(
        self,
        *,
        tenant_id: TenantId,
        note_id: NoteId,
        after_revision: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> HistoryPage | None:
        """Read one chain-verified page in a consistent SQLite snapshot."""

        connection = self._ready_connection()
        from . import _ledger_operations as operations  # noqa: PLC0415

        operations._validate_tenant_note(tenant_id, note_id)
        cursor = operations._validate_history_cursor(after_revision)
        page_size = operations._validate_history_limit(limit)
        self._assert_schema_cookie(connection)
        try:
            self._begin_transaction(connection, immediate=False)
            self._assert_schema_cookie(connection)
            page = operations._history_in_transaction(
                connection,
                tenant_id=tenant_id,
                note_id=note_id,
                after_revision=cursor,
                limit=page_size,
            )
            connection.execute("COMMIT")
            if _transaction_active(connection):
                self._poisoned = True
                raise LedgerStorageError(  # noqa: TRY301 - terminal proof stays guarded
                    "TRANSACTION_STATE_UNCERTAIN",
                    "the history snapshot did not end in a clean state",
                )
            return page  # noqa: TRY300 - result delivery is part of the guard
        except BaseException as error:
            poison_was_preexisting = self._poisoned
            self._poisoned = True
            replacement = self._settle_transaction_failure(
                connection,
                error,
                phase=None,
                poison_was_preexisting=poison_was_preexisting,
            )
            if replacement is not None:
                raise replacement from None
            raise

    def _apply_transition(
        self,
        connection: sqlite3.Connection,
        intent: _TransitionIntent,
    ) -> TransitionResult:
        from . import _ledger_operations as operations  # noqa: PLC0415

        phase = _WriteTransactionPhase.BEFORE_BEGIN
        try:
            self._begin_transaction(connection, immediate=True)
            phase = _WriteTransactionPhase.ACTIVE
            self._assert_schema_cookie(connection)
            result = operations._transition_in_transaction(connection, intent)
            self._assert_schema_cookie(connection)
            if result.replayed:
                self._rollback_or_poison(connection)
                return result
            phase = _WriteTransactionPhase.COMMIT_CALL
            connection.execute("COMMIT")
            phase = _WriteTransactionPhase.COMMIT_RETURNED
            if _transaction_active(connection):
                raise self._unknown_commit_error()  # noqa: TRY301 - guarded sentinel
            phase = _WriteTransactionPhase.COMMITTED
            return _deliver_committed_result(result)
        except BaseException as error:
            poison_was_preexisting = self._poisoned
            self._poisoned = True
            replacement = self._settle_transaction_failure(
                connection,
                error,
                phase=phase,
                poison_was_preexisting=poison_was_preexisting,
            )
            if replacement is not None:
                raise replacement from None
            raise

    def _begin_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        immediate: bool,
    ) -> None:
        if _transaction_active(connection):
            self._poisoned = True
            raise LedgerStorageError(
                "TRANSACTION_STATE_UNCERTAIN",
                "the SQLite connection was not in a clean transaction state",
            )
        statement = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        connection.execute(statement)
        if not _transaction_active(connection):
            self._poisoned = True
            raise LedgerStorageError(
                "TRANSACTION_STATE_UNCERTAIN",
                "the SQLite transaction did not start in a known state",
            )

    def _settle_transaction_failure(
        self,
        connection: sqlite3.Connection,
        error: BaseException,
        *,
        phase: _WriteTransactionPhase | None,
        poison_was_preexisting: bool,
    ) -> LedgerStorageError | None:
        if poison_was_preexisting:
            return None
        if phase in (
            _WriteTransactionPhase.COMMIT_RETURNED,
            _WriteTransactionPhase.COMMITTED,
        ):
            return self._unknown_commit_error()
        if phase is _WriteTransactionPhase.COMMIT_CALL:
            try:
                transaction_active = _transaction_active(connection)
            except BaseException:
                return self._unknown_commit_error()
            if not transaction_active:
                return self._unknown_commit_error()
        self._rollback_or_poison(connection, recoverable_poison=True)
        if isinstance(error, sqlite3.Error):
            return _mapped_database_error(error)
        return None

    def _unknown_commit_error(self) -> LedgerStorageError:
        self._poisoned = True
        return LedgerStorageError(
            "COMMIT_OUTCOME_UNKNOWN",
            "the transition outcome is unknown; retry its command on a new connection",
        )

    def _rollback_or_poison(
        self,
        connection: sqlite3.Connection,
        *,
        recoverable_poison: bool = False,
    ) -> None:
        was_poisoned = self._poisoned and not recoverable_poison
        self._poisoned = True
        try:
            clean = _rollback_to_clean(connection)
        except BaseException:
            clean = False
        if clean and not was_poisoned:
            self._poisoned = False
            return
        raise LedgerStorageError(
            "ROLLBACK_FAILED",
            "the failed operation left the transaction state uncertain",
        ) from None

    def close(self) -> None:
        """Close the owning-thread connection and its anchored descriptors."""

        with _LIFECYCLE_LOCK:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._connection is None:
            return
        self._assert_owner()
        connection = self._connection
        if _close_connection(connection):
            raise LedgerStorageError(
                "DATABASE_CLOSE_FAILED",
                "the SQLite connection could not be closed cleanly",
            )
        _close_fd(self._lock_fd)
        _close_fd(self._directory_fd)
        with _REGISTRY_LOCK:
            _LIVE_RESOURCES.pop(self._registry_token, None)
        self._finalizer.detach()
        self._connection = None
        self._lock_fd = -1
        self._directory_fd = -1

    def __enter__(self) -> Self:
        self._ready_connection()
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _assert_owner(self) -> None:
        if self._fork_invalidated or os.getpid() != self._owner_pid:
            raise LedgerStorageError(
                "PROCESS_MISMATCH",
                "a ledger connection cannot be reused after fork",
            )
        if threading.get_ident() != self._owner_thread:
            raise LedgerStorageError(
                "THREAD_MISMATCH",
                "a ledger connection belongs to its opening thread",
            )

    def _invalidate_after_fork(self) -> None:
        self._finalizer.detach()
        self._connection = None
        _close_fd(self._lock_fd)
        _close_fd(self._directory_fd)
        self._lock_fd = -1
        self._directory_fd = -1
        self._fork_invalidated = True

    def _ready_connection(self) -> sqlite3.Connection:
        self._assert_owner()
        if self._connection is None:
            raise LedgerStorageError(
                "LEDGER_CLOSED",
                "the ledger connection is already closed",
            )
        if self._poisoned:
            raise LedgerStorageError(
                "CONNECTION_POISONED",
                "the ledger connection must be closed and reopened",
            )
        return self._connection

    def _assert_schema_cookie(self, connection: sqlite3.Connection) -> None:
        if (
            _pragma_int(connection, "schema_version") != self._schema_cookie
            or _pragma_int(connection, "application_id") != APPLICATION_ID
            or _pragma_int(connection, "user_version") != STORAGE_SCHEMA_VERSION
        ):
            raise LedgerStorageError(
                "MIGRATION_DRIFT",
                "the storage schema changed while this connection was open",
            )


def _rollback_to_clean(connection: sqlite3.Connection) -> bool:
    try:
        if _transaction_active(connection):
            connection.execute("ROLLBACK")
        return not _transaction_active(connection)
    except BaseException:
        return False


def _transaction_active(connection: sqlite3.Connection) -> bool:
    return connection.in_transaction


def _deliver_committed_result(result: TransitionResult) -> TransitionResult:
    """Keep result delivery as an explicit fault boundary inside the write guard."""

    return result


def _mapped_database_error(error: sqlite3.Error) -> LedgerStorageError:
    error_code = getattr(error, "sqlite_errorcode", None)
    primary = error_code & 0xFF if type(error_code) is int else None
    if primary in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return LedgerStorageError("LEDGER_BUSY", "the SQLite writer is currently busy")
    if primary == sqlite3.SQLITE_FULL:
        return LedgerStorageError("DATABASE_FULL", "the SQLite storage is full")
    if primary == sqlite3.SQLITE_READONLY:
        return LedgerStorageError(
            "DATABASE_READ_ONLY",
            "the SQLite storage is not writable",
        )
    if primary in (
        sqlite3.SQLITE_CONSTRAINT,
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    ):
        return LedgerStorageError(
            "DATABASE_INTEGRITY",
            "the SQLite operation violated a storage invariant",
        )
    return LedgerStorageError(
        "DATABASE_OPERATION_FAILED",
        "the SQLite operation could not be completed",
    )


def _validate_directory_argument(value: str | Path) -> Path:
    if type(value) is str:
        raw = value
    elif isinstance(value, Path):
        raw = os.fspath(value)
    else:
        raise LedgerStorageError(
            "INVALID_DATA_DIRECTORY",
            "the data directory must be an absolute filesystem path",
        )
    if not raw or "\x00" in raw:
        raise LedgerStorageError(
            "INVALID_DATA_DIRECTORY",
            "the data directory must be an absolute filesystem path",
        )
    directory = Path(raw)
    if not directory.is_absolute() or ".." in directory.parts:
        raise LedgerStorageError(
            "INVALID_DATA_DIRECTORY",
            "the data directory must be an absolute filesystem path",
        )
    return directory


def _validate_busy_timeout(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_BUSY_TIMEOUT_MS:
        raise LedgerStorageError(
            "INVALID_BUSY_TIMEOUT",
            "busy timeout must be an exact integer in the supported range",
        )
    return value


def _open_directory(path: Path) -> int:
    descriptor = -1
    failed = False
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError:
        failed = True
    if failed:
        raise LedgerStorageError(
            "UNSAFE_DATA_DIRECTORY",
            "the configured data directory cannot be opened safely",
        )
    try:
        status = _fstat(descriptor, "UNSAFE_DATA_DIRECTORY")
    except LedgerStorageError:
        _close_fd(descriptor)
        raise
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE
    ):
        _close_fd(descriptor)
        raise LedgerStorageError(
            "UNSAFE_DATA_DIRECTORY",
            "the configured data directory must be owner-controlled mode 0700",
        )
    return descriptor


def _open_owned_regular(
    directory_fd: int,
    name: str,
    *,
    create_if_missing: bool,
) -> int:
    descriptor = -1
    missing = False
    failed = False
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        missing = True
    except OSError:
        failed = True
    if missing and create_if_missing:
        try:
            descriptor = os.open(
                name,
                _FILE_FLAGS | os.O_CREAT | os.O_EXCL,
                _FILE_MODE,
                dir_fd=directory_fd,
            )
        except OSError:
            failed = True
    elif missing:
        return -1
    if failed or descriptor < 0:
        raise LedgerStorageError(
            "UNSAFE_STORAGE_FILE",
            "a fixed storage file cannot be opened safely",
        )
    try:
        status = _fstat(descriptor, "UNSAFE_STORAGE_FILE")
    except LedgerStorageError:
        _close_fd(descriptor)
        raise
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != _FILE_MODE
    ):
        _close_fd(descriptor)
        raise LedgerStorageError(
            "UNSAFE_STORAGE_FILE",
            "storage files must be owner-only regular files with one link",
        )
    return descriptor


def _owned_regular_status(
    directory_fd: int,
    name: str,
    *,
    missing_ok: bool,
) -> os.stat_result | None:
    status: os.stat_result | None = None
    missing = False
    failed = False
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        missing = True
    except OSError:
        failed = True
    if missing and missing_ok:
        return None
    if (
        failed
        or status is None
        or not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != _FILE_MODE
    ):
        raise LedgerStorageError(
            "UNSAFE_STORAGE_FILE",
            "storage files must be owner-only regular files with one link",
        )
    return status


def _prepare_database(directory_fd: int) -> tuple[int, int]:
    status = _owned_regular_status(
        directory_fd,
        DATABASE_FILENAME,
        missing_ok=True,
    )
    if status is not None:
        return status.st_dev, status.st_ino

    descriptor = -1
    try:
        descriptor = os.open(
            DATABASE_FILENAME,
            _FILE_FLAGS | os.O_CREAT | os.O_EXCL,
            _FILE_MODE,
            dir_fd=directory_fd,
        )
    except FileExistsError:
        status = _owned_regular_status(
            directory_fd,
            DATABASE_FILENAME,
            missing_ok=False,
        )
        status = cast(os.stat_result, status)
        return status.st_dev, status.st_ino
    except OSError:
        raise LedgerStorageError(
            "UNSAFE_STORAGE_FILE",
            "the fixed database file cannot be created safely",
        ) from None

    try:
        status = _fstat(descriptor, "UNSAFE_STORAGE_FILE")
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) != _FILE_MODE
        ):
            raise LedgerStorageError(
                "UNSAFE_STORAGE_FILE",
                "storage files must be owner-only regular files with one link",
            )
        return status.st_dev, status.st_ino
    finally:
        _close_fd(descriptor)


def _fstat(descriptor: int, code: str) -> os.stat_result:
    status: os.stat_result | None = None
    with suppress(OSError):
        status = os.fstat(descriptor)
    if status is None:
        raise LedgerStorageError(code, "a storage object cannot be inspected safely")
    return status


def _assert_path_identity(
    directory_fd: int,
    name: str,
    expected: tuple[int, int],
) -> None:
    try:
        status = _owned_regular_status(directory_fd, name, missing_ok=False)
    except LedgerStorageError:
        raise LedgerStorageError(
            "STORAGE_IDENTITY_CHANGED",
            "the database path changed while it was being opened",
        ) from None
    status = cast(os.stat_result, status)
    if (status.st_dev, status.st_ino) != expected:
        raise LedgerStorageError(
            "STORAGE_IDENTITY_CHANGED",
            "the database path changed while it was being opened",
        )


def _inspect_sidecars(directory_fd: int) -> None:
    for name in _DATABASE_SIDECARS:
        _owned_regular_status(
            directory_fd,
            name,
            missing_ok=True,
        )


def _acquire_lock(descriptor: int, operation: int, code: str) -> None:
    failed = False
    try:
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
    except OSError:
        failed = True
    if failed:
        raise LedgerStorageError(code, "the cooperative storage lock is unavailable")


def _connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    with suppress(sqlite3.Error):
        connection = sqlite3.connect(
            path,
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=True,
            uri=False,
        )
    if connection is None:
        raise LedgerStorageError(
            "DATABASE_OPEN_FAILED",
            "the SQLite database could not be opened",
        )
    connection.row_factory = sqlite3.Row
    return connection


def _configure_connection(
    connection: sqlite3.Connection,
    busy_timeout_ms: int,
) -> None:
    limits = (
        (sqlite3.SQLITE_LIMIT_ATTACHED, 0),
        (sqlite3.SQLITE_LIMIT_LENGTH, MAX_EVENT_BYTES + 65_536),
        (sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65_536),
        (sqlite3.SQLITE_LIMIT_COLUMN, 64),
        (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64),
        (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 8),
        (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 32),
        (sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH, 0),
        (sqlite3.SQLITE_LIMIT_WORKER_THREADS, 0),
    )
    failed = False
    try:
        for category, value in limits:
            connection.setlimit(category, value)
            if connection.getlimit(category) != value:
                failed = True
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA cell_size_check = ON")
        connection.execute("PRAGMA mmap_size = 0")
        connection.execute("PRAGMA read_uncommitted = OFF")
        connection.execute("PRAGMA locking_mode = NORMAL")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        _configure_defensive_flags(connection)
    except sqlite3.Error:
        failed = True
    if failed:
        raise LedgerStorageError(
            "DATABASE_CONFIGURATION_FAILED",
            "the required SQLite connection profile is unavailable",
        )
    status = _connection_status(connection)
    if (
        not status.foreign_keys
        or status.trusted_schema
        or not status.cell_size_check
        or status.mmap_size != 0
        or status.read_uncommitted
        or status.locking_mode != "normal"
        or status.busy_timeout_ms != busy_timeout_ms
    ):
        raise LedgerStorageError(
            "DATABASE_CONFIGURATION_FAILED",
            "the required SQLite connection profile was not retained",
        )


def _configure_defensive_flags(connection: sqlite3.Connection) -> None:
    setconfig = getattr(connection, "setconfig", None)
    getconfig = getattr(connection, "getconfig", None)
    if setconfig is None or getconfig is None:
        return
    flags = (
        ("SQLITE_DBCONFIG_DEFENSIVE", True),
        ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
        ("SQLITE_DBCONFIG_DQS_DDL", False),
        ("SQLITE_DBCONFIG_DQS_DML", False),
        ("SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION", False),
        ("SQLITE_DBCONFIG_WRITABLE_SCHEMA", False),
        ("SQLITE_DBCONFIG_ENABLE_TRIGGER", False),
        ("SQLITE_DBCONFIG_ENABLE_VIEW", False),
    )
    for name, enabled in flags:
        operation = getattr(sqlite3, name, None)
        if type(operation) is not int:
            continue
        setconfig(operation, enabled)
        if bool(getconfig(operation)) is not enabled:
            raise sqlite3.OperationalError


def _configure_rollback_journal(connection: sqlite3.Connection) -> None:
    failed = False
    journal_mode = ""
    try:
        row = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        journal_mode = "" if row is None else str(row[0]).lower()
        connection.execute("PRAGMA synchronous = FULL")
    except sqlite3.Error:
        failed = True
    if (
        failed
        or journal_mode != "delete"
        or _pragma_int(connection, "synchronous") != _SQLITE_SYNCHRONOUS_FULL
    ):
        raise LedgerStorageError(
            "DATABASE_PROFILE_UNAVAILABLE",
            "the required rollback-journal and FULL profile is unavailable",
        )


def _classify_database(connection: sqlite3.Connection) -> _DatabaseState:
    application_id = _pragma_int(connection, "application_id")
    user_version = _pragma_int(connection, "user_version")
    object_count = _scalar_int(connection, "SELECT count(*) FROM sqlite_schema")
    if application_id == 0 and user_version == 0 and object_count == 0:
        return _DatabaseState.EMPTY
    if application_id == APPLICATION_ID:
        if user_version > STORAGE_SCHEMA_VERSION:
            raise LedgerStorageError(
                "SCHEMA_TOO_NEW",
                "the database schema is newer than this runtime",
            )
        if user_version != STORAGE_SCHEMA_VERSION:
            raise LedgerStorageError(
                "MIGRATION_DRIFT",
                "the database migration state is unsupported",
            )
        return _DatabaseState.CURRENT
    if application_id == 0:
        raise LedgerStorageError(
            "UNCLAIMED_DATABASE",
            "a nonempty unrecognized database will not be adopted",
        )
    raise LedgerStorageError(
        "WRONG_APPLICATION",
        "the database belongs to a different application format",
    )


def _initialize_empty_database(
    connection: sqlite3.Connection,
) -> None:
    _configure_rollback_journal(connection)
    failure: LedgerStorageError | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _classify_database(connection) is not _DatabaseState.EMPTY:
            failure = LedgerStorageError(
                "MIGRATION_DRIFT",
                "the database changed before initialization",
            )
        else:
            for statement in _MIGRATION_STATEMENTS:
                _execute_migration_statement(connection, statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, sha256)
                VALUES (?, ?, ?)
                """,
                (STORAGE_SCHEMA_VERSION, _MIGRATION_NAME, _MIGRATION_DIGEST),
            )
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {STORAGE_SCHEMA_VERSION}")
            _verify_schema(connection)
            connection.execute("COMMIT")
    except sqlite3.Error:
        failure = LedgerStorageError(
            "MIGRATION_FAILED",
            "the database migration could not be committed",
        )
    except LedgerStorageError as error:
        failure = error
    if failure is not None:
        rollback_failed = _rollback(connection)
        if rollback_failed:
            raise LedgerStorageError(
                "ROLLBACK_FAILED",
                "the failed migration left the connection state uncertain",
            )
        raise failure


def _close_before_lock_transition(connection: sqlite3.Connection) -> None:
    if _close_connection(connection):
        raise LedgerStorageError(
            "DATABASE_CLOSE_FAILED",
            "the SQLite connection could not be closed before a lock transition",
        )


def _execute_migration_statement(
    connection: sqlite3.Connection,
    statement: str,
) -> None:
    connection.execute(statement)


def _verify_schema(connection: sqlite3.Connection) -> None:
    application_id = _pragma_int(connection, "application_id")
    user_version = _pragma_int(connection, "user_version")
    if application_id != APPLICATION_ID or user_version != STORAGE_SCHEMA_VERSION:
        raise LedgerStorageError(
            "MIGRATION_DRIFT",
            "the database format markers do not match the supported schema",
        )

    failed = False
    schema_rows: tuple[tuple[str, str, str, str | None], ...] = ()
    migration_rows: tuple[tuple[int, str, str], ...] = ()
    quick_check = ""
    foreign_key_row: sqlite3.Row | None = None
    try:
        schema_rows = tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_schema
                ORDER BY type, name, tbl_name, sql
                """
            )
        )
        migration_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
            )
        )
        check_row = connection.execute("PRAGMA quick_check(1)").fetchone()
        quick_check = "" if check_row is None else str(check_row[0])
        foreign_key_row = connection.execute("PRAGMA foreign_key_check").fetchone()
    except sqlite3.Error:
        failed = True
    if (
        failed
        or schema_rows != _EXPECTED_SCHEMA
        or migration_rows != ((STORAGE_SCHEMA_VERSION, _MIGRATION_NAME, _MIGRATION_DIGEST),)
        or quick_check != "ok"
        or foreign_key_row is not None
    ):
        raise LedgerStorageError(
            "MIGRATION_DRIFT",
            "the database schema or migration ledger failed validation",
        )


def _connection_status(connection: sqlite3.Connection) -> StorageStatus:
    return StorageStatus(
        application_id=_pragma_int(connection, "application_id"),
        schema_version=_pragma_int(connection, "user_version"),
        journal_mode=_pragma_text(connection, "journal_mode").lower(),
        synchronous=_synchronous_name(_pragma_int(connection, "synchronous")),
        foreign_keys=bool(_pragma_int(connection, "foreign_keys")),
        trusted_schema=bool(_pragma_int(connection, "trusted_schema")),
        cell_size_check=bool(_pragma_int(connection, "cell_size_check")),
        mmap_size=_pragma_int(connection, "mmap_size"),
        read_uncommitted=bool(_pragma_int(connection, "read_uncommitted")),
        locking_mode=_pragma_text(connection, "locking_mode").lower(),
        busy_timeout_ms=_pragma_int(connection, "busy_timeout"),
        sqlite_version=sqlite3.sqlite_version,
    )


def _synchronous_name(value: int) -> str:
    names = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}
    return names.get(value, "UNKNOWN")


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    value: object | None = None
    failed = False
    try:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        value = None if row is None else row[0]
    except sqlite3.Error:
        failed = True
    if failed or type(value) is not int:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "a required integer database property is unavailable",
        )
    return value


def _pragma_text(connection: sqlite3.Connection, name: str) -> str:
    value: object | None = None
    failed = False
    try:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        value = None if row is None else row[0]
    except sqlite3.Error:
        failed = True
    if failed or type(value) is not str:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "a required text database property is unavailable",
        )
    return value


def _scalar_int(connection: sqlite3.Connection, query: str) -> int:
    value: object | None = None
    failed = False
    try:
        row = connection.execute(query).fetchone()
        value = None if row is None else row[0]
    except sqlite3.Error:
        failed = True
    if failed or type(value) is not int:
        raise LedgerStorageError(
            "DATABASE_INTEGRITY",
            "a required database count is unavailable",
        )
    return value


def _rollback(connection: sqlite3.Connection) -> bool:
    if not connection.in_transaction:
        return False
    failed = False
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        failed = True
    return failed


def _unlock(descriptor: int) -> None:
    failed = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        failed = True
    if failed:
        raise LedgerStorageError(
            "LOCK_FAILED",
            "the cooperative storage lock could not be released",
        )


def _close_connection(connection: sqlite3.Connection) -> bool:
    failed = False
    try:
        connection.close()
    except sqlite3.Error:
        failed = True
    return failed


def _close_fd(descriptor: int) -> None:
    if descriptor < 0:
        return
    with suppress(OSError):
        os.close(descriptor)


def _finalize_resources(registry_token: int) -> None:
    if not _LIFECYCLE_LOCK.acquire(blocking=False):
        return
    close_failed = False
    try:
        with _REGISTRY_LOCK:
            registered = _LIVE_RESOURCES.get(registry_token)
        if registered is None:
            return
        close_failed = True
        with suppress(BaseException):
            close_failed = _close_connection(registered.connection)
        if close_failed:
            _FAILED_CLOSE_QUARANTINE.append(
                (
                    registered.connection,
                    registered.lock_fd,
                    registered.directory_fd,
                )
            )
        else:
            _close_fd(registered.lock_fd)
            _close_fd(registered.directory_fd)
        with _REGISTRY_LOCK:
            _LIVE_RESOURCES.pop(registry_token, None)
    finally:
        _LIFECYCLE_LOCK.release()
    if close_failed:
        _warn_resource(
            "RecallLedger quarantined resources after an uncertain SQLite close",
        )
    _warn_resource("unclosed RecallLedger SQLiteLedger")


def _warn_resource(message: str) -> None:
    with suppress(BaseException):
        warnings.warn(message, ResourceWarning, stacklevel=3)


def _before_fork() -> None:
    global _FORK_SNAPSHOT  # noqa: PLW0603 - at-fork state is process-global

    _LIFECYCLE_LOCK.acquire()
    _REGISTRY_LOCK.acquire()
    _FORK_SNAPSHOT = tuple(_LIVE_RESOURCES.values())


def _after_fork_parent() -> None:
    global _FORK_SNAPSHOT  # noqa: PLW0603 - at-fork state is process-global

    _FORK_SNAPSHOT = ()
    _REGISTRY_LOCK.release()
    _LIFECYCLE_LOCK.release()


def _after_fork_child() -> None:
    global _FORK_QUARANTINE, _FORK_SNAPSHOT  # noqa: PLW0603

    try:
        _FORK_QUARANTINE = _FORK_SNAPSHOT
        for registered in _FORK_QUARANTINE:
            ledger = registered.ledger_ref()
            if ledger is None:
                _close_fd(registered.lock_fd)
                _close_fd(registered.directory_fd)
            else:
                ledger._invalidate_after_fork()
        _LIVE_RESOURCES.clear()
        for index, (connection, lock_fd, directory_fd) in enumerate(_FAILED_CLOSE_QUARANTINE):
            _close_fd(lock_fd)
            _close_fd(directory_fd)
            _FAILED_CLOSE_QUARANTINE[index] = (connection, -1, -1)
    finally:
        _FORK_SNAPSHOT = ()
        _REGISTRY_LOCK.release()
        _LIFECYCLE_LOCK.release()


_LIVE_RESOURCES = {}
_FORK_SNAPSHOT = ()
_FORK_QUARANTINE = ()
os.register_at_fork(
    before=_before_fork,
    after_in_parent=_after_fork_parent,
    after_in_child=_after_fork_child,
)
