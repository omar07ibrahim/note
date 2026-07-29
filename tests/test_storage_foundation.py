from __future__ import annotations

import fcntl
import gc
import os
import sqlite3
import stat
import threading
import traceback
import warnings
import weakref
from pathlib import Path
from typing import cast

import pytest

import recall_ledger.storage as storage_module
from recall_ledger import LedgerStorageError, SQLiteLedger
from recall_ledger.storage import (
    APPLICATION_ID,
    DATABASE_FILENAME,
    DEFAULT_BUSY_TIMEOUT_MS,
    LOCK_FILENAME,
    MAX_BUSY_TIMEOUT_MS,
    STORAGE_SCHEMA_VERSION,
)


def secure_directory(tmp_path: Path, name: str = "data") -> Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def raw_connection(directory: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(directory / DATABASE_FILENAME, isolation_level=None)
    (directory / DATABASE_FILENAME).chmod(0o600)
    return connection


def assert_safe_error(error: LedgerStorageError, private: str) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert private not in str(error)
    assert private not in rendered


def test_fresh_migration_profile_reopen_and_context_close(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)

    with SQLiteLedger.open(directory) as first:
        status = first.status()
        assert status.application_id == APPLICATION_ID
        assert status.schema_version == STORAGE_SCHEMA_VERSION
        assert status.journal_mode == "delete"
        assert status.synchronous == "FULL"
        assert status.foreign_keys is True
        assert status.trusted_schema is False
        assert status.cell_size_check is True
        assert status.mmap_size == 0
        assert status.read_uncommitted is False
        assert status.locking_mode == "normal"
        assert status.busy_timeout_ms == DEFAULT_BUSY_TIMEOUT_MS
        assert tuple(int(part) for part in status.sqlite_version.split(".")) >= (3, 37, 0)

        with SQLiteLedger.open(directory, busy_timeout_ms=17) as second:
            assert second.status().busy_timeout_ms == 17

    for name in (DATABASE_FILENAME, LOCK_FILENAME):
        file_status = (directory / name).stat()
        assert stat.S_ISREG(file_status.st_mode)
        assert stat.S_IMODE(file_status.st_mode) == 0o600
        assert file_status.st_nlink == 1

    reopened = SQLiteLedger.open(directory)
    connection = reopened._connection
    assert connection is not None
    migration = connection.execute("SELECT version, name, sha256 FROM schema_migrations").fetchone()
    assert migration is not None
    assert tuple(migration) == (
        STORAGE_SCHEMA_VERSION,
        storage_module._MIGRATION_NAME,
        storage_module._MIGRATION_DIGEST,
    )
    assert connection.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
    assert connection.getlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH) == 0
    reopened.close()
    reopened.close()
    with pytest.raises(LedgerStorageError) as closed:
        reopened.status()
    with pytest.raises(LedgerStorageError) as enter_closed:
        reopened.__enter__()
    assert closed.value.code == "LEDGER_CLOSED"
    assert enter_closed.value.code == "LEDGER_CLOSED"


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "../data",
        "relative/data",
        ":memory:",
        "/" + "tmp/../tmp/data",
        "/" + "tmp/\x00data",
        7,
    ],
)
def test_rejects_noncanonical_or_nonabsolute_data_directory(value: object) -> None:
    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(value)  # type: ignore[arg-type]
    assert captured.value.code == "INVALID_DATA_DIRECTORY"


@pytest.mark.parametrize("value", [-1, MAX_BUSY_TIMEOUT_MS + 1, True, 1.5, "10"])
def test_rejects_invalid_busy_timeout(tmp_path: Path, value: object) -> None:
    directory = secure_directory(tmp_path)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory, busy_timeout_ms=value)  # type: ignore[arg-type]

    assert captured.value.code == "INVALID_BUSY_TIMEOUT"
    assert not (directory / DATABASE_FILENAME).exists()


def test_rejects_missing_insecure_regular_or_symlink_directory(tmp_path: Path) -> None:
    missing = (tmp_path / "missing").resolve()
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    regular = tmp_path / "regular"
    regular.write_bytes(b"not a directory")
    target = secure_directory(tmp_path, "target")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target, target_is_directory=True)

    for path in (missing, insecure.resolve(), regular.resolve(), symlink.absolute()):
        with pytest.raises(LedgerStorageError) as captured:
            SQLiteLedger.open(path)
        assert captured.value.code == "UNSAFE_DATA_DIRECTORY"


@pytest.mark.parametrize("name", [DATABASE_FILENAME, LOCK_FILENAME])
@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo", "hardlink", "mode"])
def test_rejects_unsafe_fixed_storage_files(
    tmp_path: Path,
    name: str,
    kind: str,
) -> None:
    directory = secure_directory(tmp_path)
    target = directory / name
    if kind == "directory":
        target.mkdir(mode=0o700)
    elif kind == "symlink":
        source = directory / "source"
        source.write_bytes(b"")
        source.chmod(0o600)
        target.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    elif kind == "hardlink":
        source = directory / "source"
        source.write_bytes(b"")
        source.chmod(0o600)
        os.link(source, target)
    else:
        target.write_bytes(b"")
        target.chmod(0o640)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "UNSAFE_STORAGE_FILE"


def test_rejects_unsafe_preexisting_wal_sidecar(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory):
        pass
    sidecar = directory / f"{DATABASE_FILENAME}-wal"
    sidecar.write_bytes(b"not a wal")
    sidecar.chmod(0o640)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "UNSAFE_STORAGE_FILE"


def test_rejects_unclaimed_foreign_old_and_future_databases(tmp_path: Path) -> None:
    unclaimed = secure_directory(tmp_path, "unclaimed")
    with raw_connection(unclaimed) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    foreign = secure_directory(tmp_path, "foreign")
    with raw_connection(foreign) as connection:
        connection.execute("PRAGMA application_id = 1234")

    old = secure_directory(tmp_path, "old")
    with raw_connection(old) as connection:
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")

    future = secure_directory(tmp_path, "future")
    with raw_connection(future) as connection:
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {STORAGE_SCHEMA_VERSION + 1}")

    expectations = (
        (unclaimed, "UNCLAIMED_DATABASE"),
        (foreign, "WRONG_APPLICATION"),
        (old, "MIGRATION_DRIFT"),
        (future, "SCHEMA_TOO_NEW"),
    )
    for directory, code in expectations:
        with pytest.raises(LedgerStorageError) as captured:
            SQLiteLedger.open(directory)
        assert captured.value.code == code


def test_reserved_like_name_cannot_hide_an_unclaimed_object(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    with raw_connection(directory) as connection:
        connection.execute("CREATE TABLE sqliteXunexpected(value TEXT)")

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "UNCLAIMED_DATABASE"


def test_reserved_schema_injection_is_not_filtered_from_exact_inventory(
    tmp_path: Path,
) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory):
        pass
    with raw_connection(directory) as connection:
        version = connection.execute("PRAGMA schema_version").fetchone()
        assert version is not None
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            """
            INSERT INTO sqlite_schema(type, name, tbl_name, rootpage, sql)
            VALUES ('table', 'sqlite_evil', 'sqlite_evil', 0, ?)
            """,
            ("CREATE TABLE sqlite_evil(value TEXT)",),
        )
        connection.execute(f"PRAGMA schema_version = {int(version[0]) + 1}")
        connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "MIGRATION_DRIFT"


def test_rejects_schema_migration_and_foreign_key_drift(tmp_path: Path) -> None:
    schema = secure_directory(tmp_path, "schema")
    with SQLiteLedger.open(schema):
        pass
    with raw_connection(schema) as connection:
        connection.execute("CREATE TABLE unexpected(value TEXT)")
    with pytest.raises(LedgerStorageError) as schema_error:
        SQLiteLedger.open(schema)
    assert schema_error.value.code == "MIGRATION_DRIFT"

    migration = secure_directory(tmp_path, "migration")
    with SQLiteLedger.open(migration):
        pass
    with raw_connection(migration) as connection:
        connection.execute("UPDATE schema_migrations SET sha256 = ?", ("sha256:" + "0" * 64,))
    with pytest.raises(LedgerStorageError) as migration_error:
        SQLiteLedger.open(migration)
    assert migration_error.value.code == "MIGRATION_DRIFT"

    foreign_key = secure_directory(tmp_path, "foreign-key")
    with SQLiteLedger.open(foreign_key):
        pass
    with raw_connection(foreign_key) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO note_heads(
                tenant_id, note_id, revision, event_hash, updated_at_us, is_tombstoned
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "tn_" + "a" * 32,
                "nt_" + "b" * 32,
                1,
                "sha256:" + "c" * 64,
                1,
                0,
            ),
        )
    with pytest.raises(LedgerStorageError) as foreign_key_error:
        SQLiteLedger.open(foreign_key)
    assert foreign_key_error.value.code == "MIGRATION_DRIFT"


def test_detects_live_schema_cookie_change(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    with raw_connection(directory) as connection:
        connection.execute("CREATE TABLE noncooperative_change(value TEXT)")

    with pytest.raises(LedgerStorageError) as captured:
        ledger.status()

    assert captured.value.code == "MIGRATION_DRIFT"
    ledger.close()


@pytest.mark.parametrize(
    "pragma",
    [
        "application_id = 1234",
        f"user_version = {STORAGE_SCHEMA_VERSION + 1}",
    ],
)
def test_detects_live_format_marker_change(tmp_path: Path, pragma: str) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    with raw_connection(directory) as connection:
        connection.execute(f"PRAGMA {pragma}")

    with pytest.raises(LedgerStorageError) as captured:
        ledger.status()

    assert captured.value.code == "MIGRATION_DRIFT"
    ledger.close()


def test_shared_runtime_locks_coexist_and_exclusive_lock_blocks_open(
    tmp_path: Path,
) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as first, SQLiteLedger.open(directory) as second:
        assert first.status().schema_version == second.status().schema_version

    lock_fd = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LedgerStorageError) as captured:
            SQLiteLedger.open(directory)
        assert captured.value.code == "LEDGER_BUSY"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_second_connection_never_raw_opens_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    first = SQLiteLedger.open(directory)
    original_open = os.open
    database_opens: list[object] = []

    def record_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == DATABASE_FILENAME:
            database_opens.append(path)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", record_open)
    second = SQLiteLedger.open(directory)
    second.close()
    monkeypatch.undo()
    first.close()

    assert database_opens == []


def test_concurrent_first_open_cannot_connect_before_creation_fd_closes(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    creation_fd_live = False
    creator_blocked, release_creator, second_started, connect_started, unsafe_connect = (
        threading.Event() for _ in range(5)
    )
    original_close_fd, original_connect = (
        storage_module._close_fd,
        storage_module._connect,
    )
    opened_versions: list[int] = []
    errors: list[LedgerStorageError] = []

    def controlled_close(descriptor: int) -> None:
        nonlocal creation_fd_live
        if threading.current_thread().name == "database-creator" and not creator_blocked.is_set():
            creation_fd_live = True
            creator_blocked.set()
            assert release_creator.wait(timeout=5)
            original_close_fd(descriptor)
            creation_fd_live = False
            return
        original_close_fd(descriptor)

    def observed_connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
        if creation_fd_live:
            unsafe_connect.set()
        connect_started.set()
        return original_connect(path, busy_timeout_ms)

    def open_ledger(*, second: bool) -> None:
        if second:
            second_started.set()
        try:
            ledger = SQLiteLedger.open(directory)
        except LedgerStorageError as error:
            errors.append(error)
            return
        try:
            opened_versions.append(ledger.status().schema_version)
        finally:
            ledger.close()

    monkeypatch.setattr(storage_module, "_close_fd", controlled_close)
    monkeypatch.setattr(storage_module, "_connect", observed_connect)
    creator = threading.Thread(
        target=open_ledger,
        kwargs={"second": False},
        name="database-creator",
    )
    contender = threading.Thread(
        target=open_ledger,
        kwargs={"second": True},
        name="database-contender",
    )
    creator.start()
    assert creator_blocked.wait(timeout=5)
    contender.start()
    assert second_started.wait(timeout=5)
    assert not connect_started.wait(timeout=0.1)

    release_creator.set()
    creator.join(timeout=5)
    contender.join(timeout=5)
    assert not creator.is_alive()
    assert not contender.is_alive()
    assert not unsafe_connect.is_set()
    assert connect_started.is_set()
    assert all(error.code in {"LEDGER_BUSY", "MIGRATION_BUSY"} for error in errors)
    assert opened_versions
    monkeypatch.undo()
    with SQLiteLedger.open(directory) as ledger:
        assert ledger.status().schema_version == STORAGE_SCHEMA_VERSION


def test_shared_lock_blocks_first_migration_upgrade(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    lock_path = directory / LOCK_FILENAME
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(LedgerStorageError) as captured:
            SQLiteLedger.open(directory)
        assert captured.value.code == "MIGRATION_BUSY"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_thread_process_and_closed_guards_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    errors: list[LedgerStorageError] = []

    def read_from_other_thread() -> None:
        try:
            ledger.status()
        except LedgerStorageError as error:
            errors.append(error)

    thread = threading.Thread(target=read_from_other_thread)
    thread.start()
    thread.join()
    assert [error.code for error in errors] == ["THREAD_MISMATCH"]

    monkeypatch.setattr(
        "recall_ledger.storage.os.getpid",
        lambda: ledger._owner_pid + 1,
    )
    with pytest.raises(LedgerStorageError) as process_error:
        ledger.status()
    assert process_error.value.code == "PROCESS_MISMATCH"
    monkeypatch.undo()
    ledger.close()


def test_at_fork_parent_callback_preserves_connection(
    tmp_path: Path,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)

    storage_module._before_fork()
    assert storage_module._FORK_SNAPSHOT
    storage_module._after_fork_parent()
    assert storage_module._FORK_SNAPSHOT == ()
    assert ledger.status().schema_version == STORAGE_SCHEMA_VERSION
    ledger.close()


def test_fork_snapshot_waits_until_open_connection_is_registered(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    connect_entered, release_connect, open_complete, snapshot_ready, release_snapshot = (
        threading.Event() for _ in range(5)
    )
    close_opener = threading.Event()
    original_connect = storage_module._connect
    connections: list[sqlite3.Connection] = []
    snapshots: list[tuple[storage_module._RegisteredLedger, ...]] = []
    errors: list[BaseException] = []

    def blocked_connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
        connection = original_connect(path, busy_timeout_ms)
        connections.append(connection)
        connect_entered.set()
        assert release_connect.wait(timeout=5)
        return connection

    def open_and_close() -> None:
        try:
            ledger = SQLiteLedger.open(directory)
            open_complete.set()
            assert close_opener.wait(timeout=5)
            ledger.close()
        except BaseException as error:
            errors.append(error)

    def snapshot_parent() -> None:
        try:
            storage_module._before_fork()
            snapshots.append(storage_module._FORK_SNAPSHOT)
            snapshot_ready.set()
            assert release_snapshot.wait(timeout=5)
            storage_module._after_fork_parent()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(storage_module, "_connect", blocked_connect)
    opener = threading.Thread(target=open_and_close)
    snapshotter = threading.Thread(target=snapshot_parent)
    opener.start()
    try:
        assert connect_entered.wait(timeout=5)
        snapshotter.start()
        assert not snapshot_ready.wait(timeout=0.1)
        release_connect.set()
        assert open_complete.wait(timeout=5)
        assert snapshot_ready.wait(timeout=5)
        assert len(snapshots) == 1
        assert len(snapshots[0]) == 1
        assert snapshots[0][0].connection is connections[-1]
    finally:
        release_connect.set()
        release_snapshot.set()
        close_opener.set()
    opener.join(timeout=5)
    snapshotter.join(timeout=5)
    assert not opener.is_alive()
    assert not snapshotter.is_alive()
    assert errors == []


def test_at_fork_child_callback_quarantines_without_sqlite_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    calls = 0

    def reject_close(_connection: sqlite3.Connection) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(storage_module, "_close_connection", reject_close)
    storage_module._before_fork()
    storage_module._after_fork_child()
    gc.collect()

    assert calls == 0
    assert len(storage_module._FORK_QUARANTINE) == 1
    registered = storage_module._FORK_QUARANTINE[0]
    assert registered.ledger_ref() is ledger
    assert registered.connection is connection
    with pytest.raises(LedgerStorageError) as captured:
        ledger.status()
    assert captured.value.code == "PROCESS_MISMATCH"
    ledger.close()
    monkeypatch.undo()
    connection.close()
    storage_module._FORK_QUARANTINE = ()


def test_at_fork_child_quarantines_resource_whose_owner_was_collected() -> None:
    class Marker:
        pass

    marker = Marker()
    marker_ref = weakref.ref(marker)
    del marker
    gc.collect()
    assert marker_ref() is None
    connection = sqlite3.connect(":memory:")
    lock_fd, directory_fd = os.pipe()
    registered = storage_module._RegisteredLedger(
        ledger_ref=cast(weakref.ReferenceType[SQLiteLedger], marker_ref),
        connection=connection,
        directory_fd=directory_fd,
        lock_fd=lock_fd,
    )
    storage_module._LIFECYCLE_LOCK.acquire()
    storage_module._REGISTRY_LOCK.acquire()
    storage_module._FORK_SNAPSHOT = (registered,)
    failed_connection = sqlite3.connect(":memory:")
    failed_lock_fd, failed_directory_fd = os.pipe()
    storage_module._FAILED_CLOSE_QUARANTINE.append(
        (failed_connection, failed_lock_fd, failed_directory_fd)
    )

    storage_module._after_fork_child()

    assert (registered,) == storage_module._FORK_QUARANTINE
    row = connection.execute("SELECT 1").fetchone()
    assert row is not None
    assert tuple(row) == (1,)
    assert storage_module._FAILED_CLOSE_QUARANTINE[-1] == (
        failed_connection,
        -1,
        -1,
    )
    assert failed_connection.execute("SELECT 1").fetchone() == (1,)
    for descriptor in (
        lock_fd,
        directory_fd,
        failed_lock_fd,
        failed_directory_fd,
    ):
        with pytest.raises(OSError):
            os.fstat(descriptor)
    connection.close()
    storage_module._FAILED_CLOSE_QUARANTINE.pop()
    failed_connection.close()
    storage_module._FORK_QUARANTINE = ()


def test_real_fork_does_not_retain_parent_storage_lock(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        os.close(release_write)
        exit_code = 2
        try:
            captured: LedgerStorageError | None = None
            try:
                ledger.status()
            except LedgerStorageError as error:
                captured = error
            gc.collect()
            quarantined = storage_module._FORK_QUARANTINE
            if (
                captured is not None
                and captured.code == "PROCESS_MISMATCH"
                and len(quarantined) == 1
                and quarantined[0].ledger_ref() is ledger
                and quarantined[0].connection is connection
            ):
                exit_code = 0
            os.write(ready_write, b"1")
            os.read(release_read, 1)
        finally:
            os.close(ready_write)
            os.close(release_read)
        os._exit(exit_code)

    os.close(ready_write)
    os.close(release_read)
    lock_fd = -1
    try:
        assert os.read(ready_read, 1) == b"1"
        assert ledger.status().schema_version == STORAGE_SCHEMA_VERSION
        ledger.close()
        lock_fd = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.write(release_write, b"1")
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(ready_read)
        os.close(release_write)


def test_rejects_old_sqlite_before_touching_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(
        "recall_ledger.storage.sqlite3.sqlite_version_info",
        (3, 36, 0),
    )

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "SQLITE_TOO_OLD"
    assert not (directory / DATABASE_FILENAME).exists()


def test_migration_statement_failure_rolls_back_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    original = storage_module._execute_migration_statement
    calls = 0

    def fail_second(connection: sqlite3.Connection, statement: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError
        original(connection, statement)

    monkeypatch.setattr(storage_module, "_execute_migration_statement", fail_second)
    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)
    assert captured.value.code == "MIGRATION_FAILED"

    with raw_connection(directory) as connection:
        count = connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        assert count == (0,)
        assert connection.execute("PRAGMA application_id").fetchone() == (0,)
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)

    monkeypatch.setattr(storage_module, "_execute_migration_statement", original)
    with SQLiteLedger.open(directory) as ledger:
        assert ledger.status().schema_version == STORAGE_SCHEMA_VERSION


def test_migration_rollback_failure_discards_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)

    def fail_statement(_connection: sqlite3.Connection, _statement: str) -> None:
        raise sqlite3.OperationalError

    monkeypatch.setattr(storage_module, "_execute_migration_statement", fail_statement)
    monkeypatch.setattr(storage_module, "_rollback", lambda _connection: True)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "ROLLBACK_FAILED"


def test_failed_discovery_close_retains_shared_lock_before_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    baseline = len(storage_module._FAILED_CLOSE_QUARANTINE)
    original_close = storage_module._close_connection
    monkeypatch.setattr(storage_module, "_close_connection", lambda _connection: True)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "DATABASE_CLOSE_FAILED"
    assert len(storage_module._FAILED_CLOSE_QUARANTINE) == baseline + 1
    connection, lock_fd, directory_fd = storage_module._FAILED_CLOSE_QUARANTINE[-1]
    os.fstat(lock_fd)
    os.fstat(directory_fd)

    competing_lock = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(competing_lock)

    monkeypatch.undo()
    storage_module._FAILED_CLOSE_QUARANTINE.pop()
    assert original_close(connection) is False
    storage_module._close_fd(lock_fd)
    storage_module._close_fd(directory_fd)


def test_failed_discovery_close_retries_before_releasing_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    baseline = len(storage_module._FAILED_CLOSE_QUARANTINE)
    original_close = storage_module._close_connection
    close_calls = 0

    def fail_then_close(connection: sqlite3.Connection) -> bool:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            return True

        competing_lock = os.open(
            directory / LOCK_FILENAME,
            os.O_RDWR | os.O_CLOEXEC,
        )
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(competing_lock)
        return original_close(connection)

    monkeypatch.setattr(storage_module, "_close_connection", fail_then_close)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "DATABASE_CLOSE_FAILED"
    assert close_calls == 2
    assert len(storage_module._FAILED_CLOSE_QUARANTINE) == baseline

    competing_lock = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(competing_lock, fcntl.LOCK_UN)
    finally:
        os.close(competing_lock)


def test_uncertain_migration_close_retains_exclusive_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    original_close = storage_module._close_connection

    def fail_statement(
        _connection: sqlite3.Connection,
        _statement: str,
    ) -> None:
        raise sqlite3.OperationalError

    close_calls = 0

    def fail_migration_close(connection: sqlite3.Connection) -> bool:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            return original_close(connection)
        return True

    monkeypatch.setattr(storage_module, "_execute_migration_statement", fail_statement)
    monkeypatch.setattr(storage_module, "_rollback", lambda _connection: True)
    monkeypatch.setattr(storage_module, "_close_connection", fail_migration_close)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "DATABASE_CLOSE_FAILED"
    connection, lock_fd, directory_fd = storage_module._FAILED_CLOSE_QUARANTINE[-1]
    competing_lock = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        os.close(competing_lock)

    monkeypatch.undo()
    storage_module._FAILED_CLOSE_QUARANTINE.pop()
    assert original_close(connection) is False
    storage_module._close_fd(lock_fd)
    storage_module._close_fd(directory_fd)


def test_open_error_does_not_disclose_database_path(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    private_path = str(directory)
    database = directory / DATABASE_FILENAME
    database.write_bytes(b"not sqlite")
    database.chmod(0o600)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert_safe_error(captured.value, private_path)
    assert captured.value.__context__ is None


def test_partial_constructor_failure_returns_registry_ownership_to_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    baseline_tokens = set(storage_module._LIVE_RESOURCES)
    monkeypatch.setattr(
        weakref,
        "finalize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )

    with pytest.raises(RuntimeError, match="injected"):
        SQLiteLedger.open(directory)

    assert set(storage_module._LIVE_RESOURCES) == baseline_tokens
    lock_fd = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)

    monkeypatch.undo()
    with SQLiteLedger.open(directory) as ledger:
        assert ledger.status().schema_version == STORAGE_SCHEMA_VERSION


def test_private_profile_helpers_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class MissingConfig:
        pass

    storage_module._configure_defensive_flags(MissingConfig())  # type: ignore[arg-type]
    assert storage_module._synchronous_name(99) == "UNKNOWN"

    class RejectedConfig:
        def setconfig(
            self,
            _operation: int,
            _enabled: bool,  # noqa: FBT001 - mirrors sqlite3's positional API
        ) -> None:
            pass

        def getconfig(self, _operation: int) -> bool:
            return False

    monkeypatch.setattr(
        "recall_ledger.storage.sqlite3.SQLITE_DBCONFIG_DEFENSIVE",
        999,
    )
    with pytest.raises(sqlite3.OperationalError):
        storage_module._configure_defensive_flags(RejectedConfig())  # type: ignore[arg-type]


def test_close_failure_retains_resources_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    descriptors = (ledger._lock_fd, ledger._directory_fd)
    original = storage_module._close_connection
    calls = 0

    def fail_once(candidate: sqlite3.Connection) -> bool:
        nonlocal calls
        calls += 1
        return True if calls == 1 else original(candidate)

    monkeypatch.setattr(storage_module, "_close_connection", fail_once)

    with pytest.raises(LedgerStorageError) as captured:
        ledger.close()

    assert captured.value.code == "DATABASE_CLOSE_FAILED"
    assert ledger._connection is connection
    assert ledger.status().schema_version == STORAGE_SCHEMA_VERSION
    for descriptor in descriptors:
        os.fstat(descriptor)

    competing_lock = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(competing_lock)

    ledger.close()
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_low_level_file_and_identity_failures_are_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open

    def reject_creation(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & os.O_CREAT:
            raise PermissionError
        return original_open(path, flags, mode, dir_fd=dir_fd)

    try:
        monkeypatch.setattr(os, "open", reject_creation)
        with pytest.raises(LedgerStorageError) as create_error:
            storage_module._open_owned_regular(
                directory_fd,
                "missing",
                create_if_missing=True,
            )
        assert create_error.value.code == "UNSAFE_STORAGE_FILE"
        monkeypatch.undo()

        monkeypatch.setattr(
            os,
            "fstat",
            lambda _descriptor: (_ for _ in ()).throw(OSError()),
        )
        with pytest.raises(LedgerStorageError) as stat_error:
            storage_module._fstat(directory_fd, "INSPECTION_FAILED")
        assert stat_error.value.code == "INSPECTION_FAILED"
        monkeypatch.undo()

        with pytest.raises(LedgerStorageError) as missing_error:
            storage_module._assert_path_identity(
                directory_fd,
                "missing",
                (1, 1),
            )
        assert missing_error.value.code == "STORAGE_IDENTITY_CHANGED"

        database = directory / DATABASE_FILENAME
        database.write_bytes(b"")
        database.chmod(0o600)
        with pytest.raises(LedgerStorageError) as changed_error:
            storage_module._assert_path_identity(
                directory_fd,
                DATABASE_FILENAME,
                (1, 1),
            )
        assert changed_error.value.code == "STORAGE_IDENTITY_CHANGED"
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("wrapper", ["directory", "file"])
def test_open_wrappers_close_descriptor_when_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
) -> None:
    directory = secure_directory(tmp_path)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    original_close = os.close
    closed: list[int] = []

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(
        os,
        "fstat",
        lambda _descriptor: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(os, "close", record_close)
    try:
        with pytest.raises(LedgerStorageError):
            if wrapper == "directory":
                storage_module._open_directory(directory)
            else:
                storage_module._open_owned_regular(
                    directory_fd,
                    "new-file",
                    create_if_missing=True,
                )
        assert closed
    finally:
        monkeypatch.undo()
        os.close(directory_fd)


def test_stat_only_validation_and_database_creation_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    original_stat = os.stat
    original_open = os.open
    original_status = storage_module._owned_regular_status
    try:
        assert (
            storage_module._open_owned_regular(
                directory_fd,
                "absent",
                create_if_missing=False,
            )
            == -1
        )
        with pytest.raises(LedgerStorageError) as missing:
            storage_module._owned_regular_status(
                directory_fd,
                "absent",
                missing_ok=False,
            )
        assert missing.value.code == "UNSAFE_STORAGE_FILE"

        def reject_stat(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            if path == "blocked":
                raise PermissionError
            return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "stat", reject_stat)
        with pytest.raises(LedgerStorageError) as blocked:
            storage_module._owned_regular_status(
                directory_fd,
                "blocked",
                missing_ok=True,
            )
        assert blocked.value.code == "UNSAFE_STORAGE_FILE"
        monkeypatch.undo()

        def reject_database_creation(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if path == DATABASE_FILENAME:
                raise PermissionError
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", reject_database_creation)
        with pytest.raises(LedgerStorageError) as creation:
            storage_module._prepare_database(directory_fd)
        assert creation.value.code == "UNSAFE_STORAGE_FILE"
        monkeypatch.undo()

        database = directory / DATABASE_FILENAME
        database.write_bytes(b"")
        database.chmod(0o600)
        status_calls = 0

        def race_status(
            candidate_fd: int,
            name: str,
            *,
            missing_ok: bool,
        ) -> os.stat_result | None:
            nonlocal status_calls
            status_calls += 1
            if status_calls == 1:
                return None
            return original_status(candidate_fd, name, missing_ok=missing_ok)

        monkeypatch.setattr(storage_module, "_owned_regular_status", race_status)
        identity = storage_module._prepare_database(directory_fd)
        file_status = database.stat()
        assert identity == (file_status.st_dev, file_status.st_ino)
        assert status_calls == 2
        monkeypatch.undo()

        database.unlink()
        monkeypatch.setattr(storage_module, "_fstat", lambda *_args: directory.stat())
        with pytest.raises(LedgerStorageError) as unsafe_created:
            storage_module._prepare_database(directory_fd)
        assert unsafe_created.value.code == "UNSAFE_STORAGE_FILE"
    finally:
        monkeypatch.undo()
        os.close(directory_fd)


def test_garbage_collection_releases_raw_fds_and_cooperative_lock(
    tmp_path: Path,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    descriptors = (
        ledger._lock_fd,
        ledger._directory_fd,
    )
    reference = weakref.ref(ledger)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        del ledger
        gc.collect()

    assert reference() is None
    assert any("unclosed RecallLedger" in str(item.message) for item in captured)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)

    lock_fd = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def test_resource_warning_policy_cannot_interrupt_finalizer_cleanup(
    tmp_path: Path,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    descriptors = (ledger._lock_fd, ledger._directory_fd)
    reference = weakref.ref(ledger)

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        del ledger
        gc.collect()

    assert reference() is None
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)

    lock_fd = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def test_finalizer_defers_without_deadlocking_active_lifecycle(
    tmp_path: Path,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    token = ledger._registry_token
    descriptors = (ledger._lock_fd, ledger._directory_fd)
    reference = weakref.ref(ledger)

    lifecycle_acquired = storage_module._LIFECYCLE_LOCK.acquire()
    assert lifecycle_acquired is True
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always", ResourceWarning)
            del ledger
            gc.collect()
    finally:
        storage_module._LIFECYCLE_LOCK.release()

    assert reference() is None
    assert token in storage_module._LIVE_RESOURCES
    assert storage_module._LIVE_RESOURCES[token].ledger_ref() is None
    row = connection.execute("SELECT 1").fetchone()
    assert row is not None
    assert tuple(row) == (1,)
    for descriptor in descriptors:
        os.fstat(descriptor)

    competing_lock = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(competing_lock)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        storage_module._finalize_resources(token)
    assert token not in storage_module._LIVE_RESOURCES
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_failed_finalizer_quarantines_connection_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    lock_fd = ledger._lock_fd
    directory_fd = ledger._directory_fd
    baseline = len(storage_module._FAILED_CLOSE_QUARANTINE)
    reference = weakref.ref(ledger)
    original_close = storage_module._close_connection
    monkeypatch.setattr(storage_module, "_close_connection", lambda _connection: True)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        del ledger
        gc.collect()

    assert reference() is None
    assert len(storage_module._FAILED_CLOSE_QUARANTINE) == baseline + 1
    assert storage_module._FAILED_CLOSE_QUARANTINE[-1] == (
        connection,
        lock_fd,
        directory_fd,
    )
    assert any("quarantined resources" in str(item.message) for item in captured)
    os.fstat(lock_fd)
    os.fstat(directory_fd)

    competing_lock = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(competing_lock)

    monkeypatch.undo()
    quarantined_connection, quarantined_lock, quarantined_directory = (
        storage_module._FAILED_CLOSE_QUARANTINE.pop()
    )
    assert original_close(quarantined_connection) is False
    storage_module._close_fd(quarantined_lock)
    storage_module._close_fd(quarantined_directory)


def test_failed_open_cleanup_quarantines_connection_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    baseline = len(storage_module._FAILED_CLOSE_QUARANTINE)
    original_close = storage_module._close_connection

    def reject_configuration(
        _connection: sqlite3.Connection,
        _busy_timeout_ms: int,
    ) -> None:
        raise LedgerStorageError("CONFIGURATION_REJECTED", "rejected safely")

    monkeypatch.setattr(storage_module, "_configure_connection", reject_configuration)
    monkeypatch.setattr(storage_module, "_close_connection", lambda _connection: True)

    with pytest.raises(LedgerStorageError) as captured:
        SQLiteLedger.open(directory)

    assert captured.value.code == "DATABASE_CLOSE_FAILED"
    assert captured.value.__suppress_context__ is True
    assert len(storage_module._FAILED_CLOSE_QUARANTINE) == baseline + 1
    connection, lock_fd, directory_fd = storage_module._FAILED_CLOSE_QUARANTINE[-1]
    os.fstat(lock_fd)
    os.fstat(directory_fd)

    competing_lock = os.open(directory / LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(competing_lock)

    monkeypatch.undo()
    storage_module._FAILED_CLOSE_QUARANTINE.pop()
    assert original_close(connection) is False
    storage_module._close_fd(lock_fd)
    storage_module._close_fd(directory_fd)


def test_connection_and_configuration_failures_are_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    with pytest.raises(LedgerStorageError) as connect_error:
        storage_module._connect(directory / DATABASE_FILENAME, 1)
    assert connect_error.value.code == "DATABASE_OPEN_FAILED"
    monkeypatch.undo()

    class BrokenLimits:
        def setlimit(self, _category: int, _value: int) -> int:
            raise sqlite3.OperationalError

    with pytest.raises(LedgerStorageError) as sqlite_error:
        storage_module._configure_connection(BrokenLimits(), 1)  # type: ignore[arg-type]
    assert sqlite_error.value.code == "DATABASE_CONFIGURATION_FAILED"

    class RejectedLimits:
        def setlimit(self, _category: int, value: int) -> int:
            return value

        def getlimit(self, _category: int) -> int:
            return -1

        def execute(self, _query: str) -> None:
            pass

    with pytest.raises(LedgerStorageError) as limit_error:
        storage_module._configure_connection(RejectedLimits(), 1)  # type: ignore[arg-type]
    assert limit_error.value.code == "DATABASE_CONFIGURATION_FAILED"


def test_configuration_rechecks_retained_pragmas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    invalid = storage_module.StorageStatus(
        application_id=0,
        schema_version=0,
        journal_mode="memory",
        synchronous="FULL",
        foreign_keys=False,
        trusted_schema=False,
        cell_size_check=True,
        mmap_size=0,
        read_uncommitted=False,
        locking_mode="normal",
        busy_timeout_ms=1,
        sqlite_version=sqlite3.sqlite_version,
    )
    monkeypatch.setattr(storage_module, "_connection_status", lambda _connection: invalid)
    try:
        with pytest.raises(LedgerStorageError) as captured:
            storage_module._configure_connection(connection, 1)
    finally:
        connection.close()
    assert captured.value.code == "DATABASE_CONFIGURATION_FAILED"


def test_defensive_flag_fallback_and_wal_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    monkeypatch.delattr(
        sqlite3,
        "SQLITE_DBCONFIG_DEFENSIVE",
    )
    storage_module._configure_defensive_flags(connection)
    connection.close()
    monkeypatch.undo()

    class FailingWal:
        def execute(self, _query: str) -> None:
            raise sqlite3.OperationalError

    with pytest.raises(LedgerStorageError) as sqlite_error:
        storage_module._configure_rollback_journal(FailingWal())  # type: ignore[arg-type]
    assert sqlite_error.value.code == "DATABASE_PROFILE_UNAVAILABLE"

    class Cursor:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self._row = row

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

    class WrongWal:
        def execute(self, query: str) -> Cursor:
            return Cursor(("wal",)) if "journal_mode" in query else Cursor(None)

    with pytest.raises(LedgerStorageError) as mode_error:
        storage_module._configure_rollback_journal(WrongWal())  # type: ignore[arg-type]
    assert mode_error.value.code == "DATABASE_PROFILE_UNAVAILABLE"

    class WalWithoutFull:
        def execute(self, query: str) -> Cursor:
            return Cursor(("delete",)) if "journal_mode" in query else Cursor(None)

    monkeypatch.setattr(storage_module, "_pragma_int", lambda _connection, _name: 1)
    with pytest.raises(LedgerStorageError) as full_error:
        storage_module._configure_rollback_journal(WalWithoutFull())  # type: ignore[arg-type]
    assert full_error.value.code == "DATABASE_PROFILE_UNAVAILABLE"


def test_migration_recheck_and_domain_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = secure_directory(tmp_path, "changed")
    calls = 0

    def state_changed(_connection: sqlite3.Connection) -> storage_module._DatabaseState:
        nonlocal calls
        calls += 1
        return (
            storage_module._DatabaseState.EMPTY
            if calls == 1
            else storage_module._DatabaseState.CURRENT
        )

    monkeypatch.setattr(storage_module, "_classify_database", state_changed)
    with pytest.raises(LedgerStorageError) as changed_error:
        SQLiteLedger.open(changed)
    assert changed_error.value.code == "MIGRATION_DRIFT"
    monkeypatch.undo()

    domain = secure_directory(tmp_path, "domain")

    def reject_domain(
        _connection: sqlite3.Connection,
        _statement: str,
    ) -> None:
        raise LedgerStorageError("DOMAIN_REJECTION", "migration rejected safely")

    monkeypatch.setattr(storage_module, "_execute_migration_statement", reject_domain)
    with pytest.raises(LedgerStorageError) as domain_error:
        SQLiteLedger.open(domain)
    assert domain_error.value.code == "DOMAIN_REJECTION"


def test_schema_and_scalar_failure_helpers_are_fail_closed(
    tmp_path: Path,
) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        connection = ledger._connection
        assert connection is not None
        connection.execute("PRAGMA application_id = 0")
        with pytest.raises(LedgerStorageError) as marker_error:
            storage_module._verify_schema(connection)
        assert marker_error.value.code == "MIGRATION_DRIFT"

    class ErrorConnection:
        in_transaction = True

        def execute(self, _query: str) -> None:
            raise sqlite3.OperationalError

    error_connection = ErrorConnection()
    typed_error_connection = cast(sqlite3.Connection, error_connection)
    for operation in ("integer", "text", "scalar"):
        with pytest.raises(LedgerStorageError) as captured:
            if operation == "integer":
                storage_module._pragma_int(typed_error_connection, "user_version")
            elif operation == "text":
                storage_module._pragma_text(typed_error_connection, "journal_mode")
            else:
                storage_module._scalar_int(typed_error_connection, "SELECT 1")
        assert captured.value.code == "DATABASE_INTEGRITY"

    no_transaction = sqlite3.connect(":memory:")
    assert storage_module._rollback(no_transaction) is False
    no_transaction.close()
    assert storage_module._rollback(typed_error_connection) is True


def test_schema_query_lock_and_close_adapter_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MarkerConnection:
        def execute(self, query: str) -> None:
            if query.startswith("PRAGMA"):
                return None
            raise sqlite3.OperationalError

    monkeypatch.setattr(
        storage_module,
        "_pragma_int",
        lambda _connection, name: (
            APPLICATION_ID if name == "application_id" else STORAGE_SCHEMA_VERSION
        ),
    )
    with pytest.raises(LedgerStorageError) as schema_error:
        storage_module._verify_schema(MarkerConnection())  # type: ignore[arg-type]
    assert schema_error.value.code == "MIGRATION_DRIFT"
    monkeypatch.undo()

    monkeypatch.setattr(
        fcntl,
        "flock",
        lambda *_args: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(LedgerStorageError) as unlock_error:
        storage_module._unlock(1)
    assert unlock_error.value.code == "LOCK_FAILED"
    monkeypatch.undo()

    class BrokenClose:
        def close(self) -> None:
            raise sqlite3.OperationalError

    assert storage_module._close_connection(BrokenClose()) is True  # type: ignore[arg-type]
    storage_module._finalize_resources(-1)
