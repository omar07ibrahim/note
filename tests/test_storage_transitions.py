from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import threading
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import recall_ledger.events as events_module
import recall_ledger.storage as storage_module
from recall_ledger import (
    CommandId,
    ContractViolation,
    EventKind,
    HistoryPage,
    LedgerEvent,
    LedgerStorageError,
    NoteContent,
    NoteId,
    SQLiteLedger,
    TenantId,
    TombstoneReason,
    TransitionResult,
)
from recall_ledger.events import MAX_RECORDED_AT_US, MAX_REVISION

TENANT_A = TenantId("tn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
TENANT_B = TenantId("tn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
NOTE_A = NoteId("nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
NOTE_B = NoteId("nt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
CREATE = CommandId("cmd_00000000000000000000000000000001")
REVISE = CommandId("cmd_00000000000000000000000000000002")
DELETE = CommandId("cmd_00000000000000000000000000000003")
OTHER = CommandId("cmd_00000000000000000000000000000004")
CONTENT_V1 = NoteContent("Isolation plan", "Treat retrieved text as data.", ("safety",))
CONTENT_V2 = NoteContent("Isolation plan v2", "Keep provenance.", ("safety", "sqlite"))


def secure_directory(tmp_path: Path, name: str = "data") -> Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


class Sequence:
    def __init__(self, values: Iterator[Any]) -> None:
        self._values = values
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return next(self._values)


def sqlite_failure(primary_code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("private sqlite diagnostic")
    error.sqlite_errorcode = primary_code
    return error


class ConnectionProxy:
    def __init__(self, connection: sqlite3.Connection, mode: str) -> None:
        self.connection = connection
        self.mode = mode
        self.forced_active = False
        self.commit_called = False
        self.commit_returned = False
        self.rollback_returned = False
        self.state_probe_raised = False

    @property
    def in_transaction(self) -> bool:
        active = self.forced_active or self.connection.in_transaction
        interrupt_probe = (
            (self.mode == "begin_probe_base" and active)
            or (self.mode == "commit_call_probe_base" and self.commit_called)
            or (self.mode == "commit_probe_base" and self.commit_returned)
            or (self.mode == "rollback_probe_base" and self.rollback_returned)
        )
        if interrupt_probe and not self.state_probe_raised:
            self.state_probe_raised = True
            raise KeyboardInterrupt
        return active

    def execute(  # noqa: PLR0912 - adversarial proxy enumerates transaction edges
        self,
        statement: str,
        parameters: Any = (),
    ) -> Any:
        operation = statement.strip().upper()
        if operation in {"BEGIN", "BEGIN IMMEDIATE"}:
            if self.mode == "begin_sqlite_before":
                raise sqlite_failure(sqlite3.SQLITE_BUSY)
            if self.mode == "begin_base_before":
                raise KeyboardInterrupt
            if self.mode in {"begin_sqlite_after", "begin_base_after"}:
                result = self.connection.execute(statement, parameters)
                if self.mode == "begin_sqlite_after":
                    raise sqlite_failure(sqlite3.SQLITE_BUSY)
                raise KeyboardInterrupt
            if self.mode == "begin_noop":
                return self.connection.execute("SELECT 1")
        if operation == "COMMIT":
            self.commit_called = True
            if self.mode == "commit_call_probe_base":
                raise KeyboardInterrupt
            if self.mode in {"commit_sqlite_before", "commit_base_before"}:
                if self.mode == "commit_sqlite_before":
                    raise sqlite_failure(sqlite3.SQLITE_BUSY)
                raise KeyboardInterrupt
            if self.mode in {
                "commit_sqlite_after",
                "commit_base_after",
                "commit_lingers",
            }:
                result = self.connection.execute(statement, parameters)
                self.commit_returned = True
                if self.mode == "commit_sqlite_after":
                    raise sqlite_failure(sqlite3.SQLITE_IOERR)
                if self.mode == "commit_base_after":
                    raise KeyboardInterrupt
                self.forced_active = True
                return result
            if self.mode == "commit_rollback_after":
                self.connection.execute("ROLLBACK")
                raise sqlite_failure(sqlite3.SQLITE_IOERR)
            result = self.connection.execute(statement, parameters)
            self.commit_returned = True
            return result
        if operation == "ROLLBACK":
            if self.mode == "rollback_raise":
                raise sqlite_failure(sqlite3.SQLITE_IOERR)
            result = self.connection.execute(statement, parameters)
            self.rollback_returned = True
            if self.mode == "rollback_lingers":
                self.forced_active = True
            return result
        return self.connection.execute(statement, parameters)

    def close(self) -> None:
        self.connection.close()


def install_proxy(ledger: SQLiteLedger, mode: str) -> ConnectionProxy:
    connection = ledger._connection
    assert connection is not None
    proxy = ConnectionProxy(connection, mode)
    ledger._connection = cast(sqlite3.Connection, proxy)
    return proxy


def table_snapshot(ledger: SQLiteLedger) -> tuple[tuple[object, ...], ...]:
    connection = ledger._connection
    assert connection is not None
    events = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT * FROM ledger_events ORDER BY tenant_id, note_id, revision"
        )
    )
    heads = tuple(
        tuple(row)
        for row in connection.execute("SELECT * FROM note_heads ORDER BY tenant_id, note_id")
    )
    return events + heads


def assert_safe_error(error: BaseException, *private_values: str) -> None:
    rendered = "".join(traceback.format_exception(error))
    for private in private_values:
        assert private not in str(error)
        assert private not in rendered


def canonical_created_event() -> LedgerEvent:
    return LedgerEvent.create(
        tenant_id=TENANT_A,
        note_id=NOTE_A,
        command_id=CREATE,
        recorded_at_us=10,
        content=CONTENT_V1,
    )


def crafted_revision(
    parent: LedgerEvent,
    *,
    revision: int,
    recorded_at_us: int,
    previous_event_hash: str,
) -> LedgerEvent:
    material: events_module.JsonObject = {
        "command_id": REVISE,
        "content": {
            "body": CONTENT_V2.body,
            "tags": list(CONTENT_V2.tags),
            "title": CONTENT_V2.title,
        },
        "kind": EventKind.REVISED.value,
        "note_id": parent.note_id,
        "previous_event_hash": previous_event_hash,
        "recorded_at_us": recorded_at_us,
        "revision": revision,
        "schema_version": 1,
        "tenant_id": parent.tenant_id,
        "tombstone_reason": None,
    }
    return LedgerEvent(
        tenant_id=parent.tenant_id,
        note_id=parent.note_id,
        command_id=REVISE,
        revision=revision,
        recorded_at_us=recorded_at_us,
        kind=EventKind.REVISED,
        content=CONTENT_V2,
        tombstone_reason=None,
        previous_event_hash=previous_event_hash,
        event_hash=events_module._digest(events_module._canonical_bytes(material)),
    )


def test_atomic_chain_replay_visibility_history_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ids = Sequence(iter((NOTE_A,)))
    clock = Sequence(iter((100, 90, 101)))
    monkeypatch.setattr(storage_module, "_new_note_id", ids)
    monkeypatch.setattr(storage_module, "_utc_now_us", clock)

    with SQLiteLedger.open(directory) as ledger:
        created = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        assert created == TransitionResult(event=created.event, replayed=False)
        assert created.event.note_id == NOTE_A
        assert created.event.recorded_at_us == 100
        assert ids.calls == 1
        assert clock.calls == 1

        immediate_replay = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        assert immediate_replay == TransitionResult(event=created.event, replayed=True)
        assert ids.calls == 1
        assert clock.calls == 1

        revised = ledger.revise_note(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            command_id=REVISE,
            expected_revision=1,
            content=CONTENT_V2,
        )
        assert revised.event.revision == 2
        assert revised.event.recorded_at_us == 100
        assert revised.event.previous_event_hash == created.event.event_hash

        tombstoned = ledger.tombstone_note(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            command_id=DELETE,
            expected_revision=2,
            reason=TombstoneReason.USER_REQUEST,
        )
        assert tombstoned.event.revision == 3
        assert tombstoned.event.recorded_at_us == 101
        assert tombstoned.event.content is None
        assert b"Isolation plan" not in tombstoned.event.to_bytes()
        assert ledger.get_note(tenant_id=TENANT_A, note_id=NOTE_A) is None
        assert ledger.get_head(tenant_id=TENANT_A, note_id=NOTE_A) == tombstoned.event

        first = ledger.read_history(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            limit=2,
        )
        assert first == HistoryPage(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            events=(created.event, revised.event),
            next_after_revision=2,
        )
        second = ledger.read_history(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            after_revision=2,
            limit=2,
        )
        assert second == HistoryPage(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            events=(tombstoned.event,),
            next_after_revision=None,
        )
        assert ledger.read_history(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            after_revision=3,
        ) == HistoryPage(TENANT_A, NOTE_A, (), None)
        assert ledger.read_history(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            after_revision=4,
        ) == HistoryPage(TENANT_A, NOTE_A, (), None)

        for replay, expected in (
            (
                ledger.create_note(
                    tenant_id=TENANT_A,
                    command_id=CREATE,
                    content=CONTENT_V1,
                ),
                created.event,
            ),
            (
                ledger.revise_note(
                    tenant_id=TENANT_A,
                    note_id=NOTE_A,
                    command_id=REVISE,
                    expected_revision=1,
                    content=CONTENT_V2,
                ),
                revised.event,
            ),
            (
                ledger.tombstone_note(
                    tenant_id=TENANT_A,
                    note_id=NOTE_A,
                    command_id=DELETE,
                    expected_revision=2,
                    reason=TombstoneReason.USER_REQUEST,
                ),
                tombstoned.event,
            ),
        ):
            assert replay == TransitionResult(event=expected, replayed=True)
        assert ids.calls == 1
        assert clock.calls == 3

    with SQLiteLedger.open(directory) as reopened:
        assert reopened.get_head(tenant_id=TENANT_A, note_id=NOTE_A) == tombstoned.event
        history = reopened.read_history(tenant_id=TENANT_A, note_id=NOTE_A)
        assert history is not None
        assert history.events == (created.event, revised.event, tombstoned.event)


def test_idempotency_conflicts_precede_state_and_clock_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ids = Sequence(iter((NOTE_A,)))
    clock = Sequence(iter((10, 11, 12)))
    monkeypatch.setattr(storage_module, "_new_note_id", ids)
    monkeypatch.setattr(storage_module, "_utc_now_us", clock)

    with SQLiteLedger.open(directory) as ledger:
        ledger.create_note(tenant_id=TENANT_A, command_id=CREATE, content=CONTENT_V1)
        ledger.revise_note(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            command_id=REVISE,
            expected_revision=1,
            content=CONTENT_V2,
        )
        ledger.tombstone_note(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            command_id=DELETE,
            expected_revision=2,
            reason=TombstoneReason.USER_REQUEST,
        )
        baseline = table_snapshot(ledger)

        operations: tuple[Callable[[], object], ...] = (
            lambda: ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V2,
            ),
            lambda: ledger.revise_note(
                tenant_id=TENANT_A,
                note_id=NOTE_B,
                command_id=REVISE,
                expected_revision=1,
                content=CONTENT_V2,
            ),
            lambda: ledger.revise_note(
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                command_id=REVISE,
                expected_revision=2,
                content=CONTENT_V2,
            ),
            lambda: ledger.revise_note(
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                command_id=REVISE,
                expected_revision=1,
                content=CONTENT_V1,
            ),
            lambda: ledger.tombstone_note(
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                command_id=REVISE,
                expected_revision=1,
                reason=TombstoneReason.USER_REQUEST,
            ),
            lambda: ledger.tombstone_note(
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                command_id=DELETE,
                expected_revision=1,
                reason=TombstoneReason.USER_REQUEST,
            ),
            lambda: ledger.tombstone_note(
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                command_id=DELETE,
                expected_revision=2,
                reason=TombstoneReason.ADMINISTRATIVE,
            ),
        )
        for operation in operations:
            with pytest.raises(LedgerStorageError) as captured:
                operation()
            assert captured.value.code == "IDEMPOTENCY_CONFLICT"

        assert ids.calls == 1
        assert clock.calls == 3
        assert table_snapshot(ledger) == baseline


def test_new_commands_enforce_missing_revision_and_terminal_state_without_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ids = Sequence(iter((NOTE_A,)))
    clock = Sequence(iter((10, 11)))
    monkeypatch.setattr(storage_module, "_new_note_id", ids)
    monkeypatch.setattr(storage_module, "_utc_now_us", clock)

    with SQLiteLedger.open(directory) as ledger:
        with pytest.raises(LedgerStorageError) as missing:
            ledger.revise_note(
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                command_id=REVISE,
                expected_revision=1,
                content=CONTENT_V2,
            )
        assert missing.value.code == "NOTE_NOT_FOUND"
        assert clock.calls == 0

        ledger.create_note(tenant_id=TENANT_A, command_id=CREATE, content=CONTENT_V1)
        for expected in (2, MAX_REVISION):
            with pytest.raises(LedgerStorageError) as conflict:
                ledger.revise_note(
                    tenant_id=TENANT_A,
                    note_id=NOTE_A,
                    command_id=REVISE,
                    expected_revision=expected,
                    content=CONTENT_V2,
                )
            assert conflict.value.code == "REVISION_CONFLICT"
        assert clock.calls == 1

        deleted = ledger.tombstone_note(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            command_id=DELETE,
            expected_revision=1,
            reason=TombstoneReason.RETENTION_POLICY,
        )
        assert deleted.event.kind is EventKind.TOMBSTONED
        operations: tuple[tuple[CommandId, Callable[[], object]], ...] = (
            (
                REVISE,
                lambda: ledger.revise_note(
                    tenant_id=TENANT_A,
                    note_id=NOTE_A,
                    command_id=REVISE,
                    expected_revision=2,
                    content=CONTENT_V2,
                ),
            ),
            (
                OTHER,
                lambda: ledger.tombstone_note(
                    tenant_id=TENANT_A,
                    note_id=NOTE_A,
                    command_id=OTHER,
                    expected_revision=2,
                    reason=TombstoneReason.ADMINISTRATIVE,
                ),
            ),
        )
        for command, operation in operations:
            assert command
            with pytest.raises(LedgerStorageError) as terminal:
                operation()
            assert terminal.value.code == "NOTE_TOMBSTONED"
        assert clock.calls == 2


def test_exhausted_revision_fails_before_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = storage_module._revise_intent(
        tenant_id=TENANT_A,
        note_id=NOTE_A,
        command_id=REVISE,
        expected_revision=MAX_REVISION,
        content=CONTENT_V2,
    )
    parent = cast(
        LedgerEvent,
        SimpleNamespace(kind=EventKind.CREATED, revision=MAX_REVISION),
    )
    monkeypatch.setattr(storage_module, "_load_command_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(storage_module, "_load_head", lambda *_arguments: parent)
    monkeypatch.setattr(
        storage_module,
        "_utc_now_us",
        lambda: (_ for _ in ()).throw(AssertionError("clock must stay unread")),
    )

    with pytest.raises(ContractViolation) as captured:
        storage_module._transition_in_transaction(
            cast(sqlite3.Connection, object()),
            intent,
        )
    assert captured.value.code == "REVISION_EXHAUSTED"


def test_tenant_isolation_allows_same_identifiers_without_cross_tenant_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    clock = Sequence(iter((10, 20)))
    monkeypatch.setattr(storage_module, "_utc_now_us", clock)

    with SQLiteLedger.open(directory) as ledger:
        first = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        second = ledger.create_note(
            tenant_id=TENANT_B,
            command_id=CREATE,
            content=CONTENT_V2,
        )
        assert first.event.note_id == second.event.note_id == NOTE_A
        assert first.event.content != second.event.content
        assert ledger.get_note(tenant_id=TENANT_A, note_id=NOTE_A) == first.event
        assert ledger.get_note(tenant_id=TENANT_B, note_id=NOTE_A) == second.event
        assert ledger.get_note(tenant_id=TENANT_A, note_id=NOTE_B) is None
        assert ledger.get_note(tenant_id=TENANT_B, note_id=NOTE_B) is None
        assert ledger.read_history(tenant_id=TENANT_A, note_id=NOTE_B) is None

        with pytest.raises(LedgerStorageError) as missing:
            ledger.revise_note(
                tenant_id=TENANT_B,
                note_id=NOTE_B,
                command_id=OTHER,
                expected_revision=1,
                content=CONTENT_V1,
            )
        assert missing.value.code == "NOTE_NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "arguments", "code"),
    [
        (
            "revise_note",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "command_id": REVISE,
                "expected_revision": False,
                "content": CONTENT_V2,
            },
            "INVALID_EXPECTED_REVISION",
        ),
        (
            "revise_note",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "command_id": REVISE,
                "expected_revision": 0,
                "content": CONTENT_V2,
            },
            "INVALID_EXPECTED_REVISION",
        ),
        (
            "tombstone_note",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "command_id": DELETE,
                "expected_revision": MAX_REVISION + 1,
                "reason": TombstoneReason.USER_REQUEST,
            },
            "INVALID_EXPECTED_REVISION",
        ),
        (
            "tombstone_note",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "command_id": DELETE,
                "expected_revision": 1,
                "reason": "user_request",
            },
            "INVALID_TOMBSTONE_REASON",
        ),
        (
            "read_history",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "after_revision": True,
            },
            "INVALID_HISTORY_CURSOR",
        ),
        (
            "read_history",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "after_revision": -1,
            },
            "INVALID_HISTORY_CURSOR",
        ),
        (
            "read_history",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "limit": 0,
            },
            "INVALID_HISTORY_LIMIT",
        ),
        (
            "read_history",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "limit": 101,
            },
            "INVALID_HISTORY_LIMIT",
        ),
        (
            "read_history",
            {
                "tenant_id": TENANT_A,
                "note_id": NOTE_A,
                "limit": 1.0,
            },
            "INVALID_HISTORY_LIMIT",
        ),
    ],
)
def test_transition_and_history_arguments_are_exact(
    tmp_path: Path,
    method: str,
    arguments: dict[str, object],
    code: str,
) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        with pytest.raises((LedgerStorageError, ContractViolation)) as captured:
            getattr(ledger, method)(**arguments)
        assert isinstance(captured.value, LedgerStorageError | ContractViolation)
        assert captured.value.code == code


@pytest.mark.parametrize("clock_value", [False, -1, MAX_RECORDED_AT_US + 1])
def test_clock_values_fail_closed_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_value: object,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: clock_value)

    with SQLiteLedger.open(directory) as ledger:
        before = table_snapshot(ledger)
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "CLOCK_OUT_OF_RANGE"
        assert table_snapshot(ledger) == before


def test_clock_exception_is_safe_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    private = "private-clock-diagnostic"
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)

    def fail_clock() -> int:
        raise RuntimeError(private)

    monkeypatch.setattr(storage_module, "_utc_now_us", fail_clock)
    with SQLiteLedger.open(directory) as ledger:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "CLOCK_UNAVAILABLE"
        assert_safe_error(captured.value, private, str(directory))
        assert table_snapshot(ledger) == ()


def test_note_id_collisions_are_tenant_scoped_bounded_and_clock_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    ids = Sequence(iter((NOTE_A, NOTE_A, NOTE_B, NOTE_A)))
    clock = Sequence(iter((10, 11, 12)))
    monkeypatch.setattr(storage_module, "_new_note_id", ids)
    monkeypatch.setattr(storage_module, "_utc_now_us", clock)

    with SQLiteLedger.open(directory) as ledger:
        first = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        second = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=OTHER,
            content=CONTENT_V2,
        )
        cross_tenant = ledger.create_note(
            tenant_id=TENANT_B,
            command_id=CREATE,
            content=CONTENT_V2,
        )
        assert first.event.note_id == NOTE_A
        assert second.event.note_id == NOTE_B
        assert cross_tenant.event.note_id == NOTE_A
        assert ids.calls == 4
        assert clock.calls == 3

        monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
        before = table_snapshot(ledger)
        with pytest.raises(LedgerStorageError) as exhausted:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CommandId("cmd_00000000000000000000000000000005"),
                content=CONTENT_V1,
            )
        assert exhausted.value.code == "ID_GENERATION_EXHAUSTED"
        assert clock.calls == 3
        assert table_snapshot(ledger) == before


@pytest.mark.parametrize(
    "factory",
    [
        lambda: NoteId("invalid"),
        lambda: (_ for _ in ()).throw(RuntimeError("private factory detail")),
    ],
)
def test_note_id_factory_failures_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: object,
) -> None:
    directory = secure_directory(tmp_path)
    clock = Sequence(iter((10,)))
    monkeypatch.setattr(storage_module, "_new_note_id", factory)
    monkeypatch.setattr(storage_module, "_utc_now_us", clock)

    with SQLiteLedger.open(directory) as ledger:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "ID_GENERATION_FAILED"
        assert_safe_error(captured.value, "private factory detail", str(directory))
        assert clock.calls == 0
        assert table_snapshot(ledger) == ()


def test_competing_writers_serialize_revision_and_exact_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    with SQLiteLedger.open(directory) as ledger:
        ledger.create_note(tenant_id=TENANT_A, command_id=CREATE, content=CONTENT_V1)

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []
    guard = threading.Lock()

    def worker(command_id: CommandId) -> None:
        try:
            with SQLiteLedger.open(directory) as ledger:
                barrier.wait()
                result = ledger.revise_note(
                    tenant_id=TENANT_A,
                    note_id=NOTE_A,
                    command_id=command_id,
                    expected_revision=1,
                    content=CONTENT_V2,
                )
                outcome: tuple[str, object] = ("ok", result)
        except LedgerStorageError as error:
            outcome = ("error", error.code)
        with guard:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=worker, args=(REVISE,)),
        threading.Thread(target=worker, args=(OTHER,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(kind for kind, _value in outcomes) == ["error", "ok"]
    assert ("error", "REVISION_CONFLICT") in outcomes
    with SQLiteLedger.open(directory) as ledger:
        head = ledger.get_head(tenant_id=TENANT_A, note_id=NOTE_A)
        assert head is not None
        assert head.revision == 2

    barrier = threading.Barrier(2)
    outcomes.clear()

    def replay_worker() -> None:
        try:
            with SQLiteLedger.open(directory) as ledger:
                barrier.wait()
                result = ledger.create_note(
                    tenant_id=TENANT_B,
                    command_id=CREATE,
                    content=CONTENT_V1,
                )
                outcome: tuple[str, object] = ("ok", result.replayed)
        except LedgerStorageError as error:
            outcome = ("error", error.code)
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=replay_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [("ok", False), ("ok", True)]


def test_concurrent_same_command_with_different_intent_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []
    guard = threading.Lock()

    def worker(content: NoteContent) -> None:
        try:
            with SQLiteLedger.open(directory) as ledger:
                barrier.wait()
                result = ledger.create_note(
                    tenant_id=TENANT_A,
                    command_id=CREATE,
                    content=content,
                )
                outcome: tuple[str, object] = ("ok", result.event.content)
        except LedgerStorageError as error:
            outcome = ("error", error.code)
        with guard:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=worker, args=(CONTENT_V1,)),
        threading.Thread(target=worker, args=(CONTENT_V2,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(kind for kind, _value in outcomes) == ["error", "ok"]
    assert ("error", "IDEMPOTENCY_CONFLICT") in outcomes
    with SQLiteLedger.open(directory) as ledger:
        snapshot = table_snapshot(ledger)
        assert len(snapshot) == 2


def test_real_writer_contention_is_bounded_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)

    with (
        SQLiteLedger.open(directory) as first,
        SQLiteLedger.open(
            directory,
            busy_timeout_ms=0,
        ) as second,
    ):
        first.create_note(tenant_id=TENANT_A, command_id=CREATE, content=CONTENT_V1)
        first_connection = first._connection
        assert first_connection is not None
        first_connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(LedgerStorageError) as busy:
            second.revise_note(
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                command_id=REVISE,
                expected_revision=1,
                content=CONTENT_V2,
            )
        assert busy.value.code == "LEDGER_BUSY"
        first_connection.execute("ROLLBACK")
        result = second.revise_note(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            command_id=REVISE,
            expected_revision=1,
            content=CONTENT_V2,
        )
        assert result.event.revision == 2


@pytest.mark.parametrize(
    ("crash_point", "exit_code", "committed"),
    [
        ("before_commit", 23, "rolled_back"),
        ("after_commit", 24, "committed"),
    ],
)
def test_process_crash_reconciles_by_exact_command_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    exit_code: int,
    committed: str,
) -> None:
    directory = secure_directory(tmp_path, crash_point)
    with SQLiteLedger.open(directory):
        pass

    child = textwrap.dedent(
        """
        import os
        import sys

        import recall_ledger.storage as storage
        from recall_ledger import CommandId, NoteContent, NoteId, SQLiteLedger, TenantId

        tenant = TenantId("tn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        command = CommandId("cmd_00000000000000000000000000000001")
        note = NoteId("nt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        storage._new_note_id = lambda: note
        storage._utc_now_us = lambda: 10
        ledger = SQLiteLedger.open(sys.argv[1])

        if sys.argv[2] == "before_commit":
            original = storage._insert_head

            def crash_after_head(connection, event):
                original(connection, event)
                os._exit(23)

            storage._insert_head = crash_after_head
        else:
            connection = ledger._connection

            class CommitCrash:
                @property
                def in_transaction(self):
                    return connection.in_transaction

                def execute(self, statement, parameters=()):
                    result = connection.execute(statement, parameters)
                    if statement.strip().upper() == "COMMIT":
                        os._exit(24)
                    return result

                def close(self):
                    connection.close()

            ledger._connection = CommitCrash()

        ledger.create_note(
            tenant_id=tenant,
            command_id=command,
            content=NoteContent("Isolation plan", "Treat retrieved text as data.", ("safety",)),
        )
        raise AssertionError("the child did not crash at the requested boundary")
        """
    )
    completed = subprocess.run(  # noqa: S603 - executable and script are test-owned
        [sys.executable, "-c", child, str(directory), crash_point],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == exit_code
    assert completed.stdout == ""
    assert completed.stderr == ""

    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    with SQLiteLedger.open(directory) as ledger:
        retry = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        assert retry.replayed is (committed == "committed")
        connection = ledger._connection
        assert connection is not None
        quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
        assert quick_check is not None
        assert tuple(quick_check) == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("begin_sqlite_before", "LEDGER_BUSY"),
        ("begin_sqlite_after", "LEDGER_BUSY"),
        ("begin_noop", "TRANSACTION_STATE_UNCERTAIN"),
    ],
)
def test_begin_failure_states_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: str,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, mode)
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == expected
        assert proxy.connection.in_transaction is False
        if mode == "begin_noop":
            with pytest.raises(LedgerStorageError) as poisoned:
                ledger.status()
            assert poisoned.value.code == "CONNECTION_POISONED"
        else:
            assert ledger.status().schema_version == 1
    finally:
        ledger.close()


def test_begin_base_exception_rolls_back_and_preexisting_state_poisons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)

    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, "begin_base_after")
    try:
        with pytest.raises(KeyboardInterrupt):
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert proxy.connection.in_transaction is False
        assert ledger.status().schema_version == 1
    finally:
        ledger.close()

    ledger = SQLiteLedger.open(directory)
    install_proxy(ledger, "begin_base_before")
    try:
        with pytest.raises(KeyboardInterrupt):
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert ledger.status().schema_version == 1
    finally:
        ledger.close()

    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    connection.execute("BEGIN")
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "TRANSACTION_STATE_UNCERTAIN"
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.get_head(tenant_id=TENANT_A, note_id=NOTE_A)
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("mode", "outcome", "exception_kind"),
    [
        ("commit_sqlite_before", "rolled_back", "sqlite"),
        ("commit_sqlite_after", "committed", "sqlite"),
        ("commit_rollback_after", "rolled_back", "sqlite"),
        ("commit_lingers", "committed", "sqlite"),
        ("commit_base_before", "rolled_back", "base"),
        ("commit_base_after", "committed", "base"),
    ],
)
def test_commit_failure_states_require_safe_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    outcome: str,
    exception_kind: str,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, mode)
    try:
        if exception_kind == "base" and outcome == "rolled_back":
            with pytest.raises(KeyboardInterrupt):
                ledger.create_note(
                    tenant_id=TENANT_A,
                    command_id=CREATE,
                    content=CONTENT_V1,
                )
            assert ledger.status().schema_version == 1
        else:
            with pytest.raises(LedgerStorageError) as captured:
                ledger.create_note(
                    tenant_id=TENANT_A,
                    command_id=CREATE,
                    content=CONTENT_V1,
                )
            expected = "LEDGER_BUSY" if mode == "commit_sqlite_before" else "COMMIT_OUTCOME_UNKNOWN"
            assert captured.value.code == expected
            if expected == "COMMIT_OUTCOME_UNKNOWN":
                with pytest.raises(LedgerStorageError) as poisoned:
                    ledger.status()
                assert poisoned.value.code == "CONNECTION_POISONED"
            else:
                assert ledger.status().schema_version == 1
        assert proxy.connection.in_transaction is False
    finally:
        ledger.close()

    with SQLiteLedger.open(directory) as reopened:
        retry = reopened.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        assert retry.replayed is (outcome == "committed")


def test_transition_guard_rolls_back_interrupt_during_replay_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_transition = storage_module._transition_in_transaction

    class InterruptingResult:
        @property
        def replayed(self) -> bool:
            raise KeyboardInterrupt

    def interrupt_after_transition(
        connection: sqlite3.Connection,
        intent: object,
    ) -> InterruptingResult:
        original_transition(connection, cast(Any, intent))
        return InterruptingResult()

    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_transition_in_transaction", interrupt_after_transition)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    with SQLiteLedger.open(directory) as ledger:
        connection = ledger._connection
        assert connection is not None
        with pytest.raises(KeyboardInterrupt):
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert connection.in_transaction is False
        assert table_snapshot(ledger) == ()
        assert ledger.status().schema_version == 1


@pytest.mark.parametrize("operation", ["write", "history"])
def test_outer_guard_covers_interrupt_after_begin_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    directory = secure_directory(tmp_path)
    original_begin = SQLiteLedger._begin_transaction

    def interrupt_after_begin(
        self: SQLiteLedger,
        connection: sqlite3.Connection,
        *,
        immediate: bool,
    ) -> None:
        original_begin(self, connection, immediate=immediate)
        raise KeyboardInterrupt

    monkeypatch.setattr(SQLiteLedger, "_begin_transaction", interrupt_after_begin)
    with SQLiteLedger.open(directory) as ledger:
        connection = ledger._connection
        assert connection is not None
        with pytest.raises(KeyboardInterrupt):
            if operation == "write":
                ledger.create_note(
                    tenant_id=TENANT_A,
                    command_id=CREATE,
                    content=CONTENT_V1,
                )
            else:
                ledger.read_history(tenant_id=TENANT_A, note_id=NOTE_A)
        assert connection.in_transaction is False
        assert table_snapshot(ledger) == ()
        assert ledger.status().schema_version == 1


@pytest.mark.parametrize("operation", ["write", "history"])
def test_post_begin_state_probe_interrupt_is_contained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, "begin_probe_base")
    try:
        with pytest.raises(KeyboardInterrupt):
            if operation == "write":
                ledger.create_note(
                    tenant_id=TENANT_A,
                    command_id=CREATE,
                    content=CONTENT_V1,
                )
            else:
                ledger.read_history(tenant_id=TENANT_A, note_id=NOTE_A)
        assert proxy.connection.in_transaction is False
        assert ledger.status().schema_version == 1
    finally:
        ledger.close()


def test_write_post_commit_probe_interrupt_is_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, "commit_probe_base")
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "COMMIT_OUTCOME_UNKNOWN"
        assert proxy.connection.in_transaction is False
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()

    with SQLiteLedger.open(directory) as reopened:
        retry = reopened.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        assert retry.replayed is True


def test_commit_failure_with_unreadable_state_is_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, "commit_call_probe_base")
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "COMMIT_OUTCOME_UNKNOWN"
        assert proxy.connection.in_transaction is True
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()

    with SQLiteLedger.open(directory) as reopened:
        retry = reopened.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        )
        assert retry.replayed is False


def test_interrupt_after_clean_commit_before_result_delivery_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)

    def interrupt_committed_result(_result: TransitionResult) -> TransitionResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        storage_module,
        "_deliver_committed_result",
        interrupt_committed_result,
    )
    ledger = SQLiteLedger.open(directory)
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "COMMIT_OUTCOME_UNKNOWN"
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()

    with SQLiteLedger.open(directory) as reopened:
        assert reopened.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        ).replayed is True


def test_history_post_commit_probe_interrupt_leaves_clean_connection(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, "commit_probe_base")
    try:
        with pytest.raises(KeyboardInterrupt):
            ledger.read_history(tenant_id=TENANT_A, note_id=NOTE_A)
        assert proxy.connection.in_transaction is False
        assert ledger.status().schema_version == 1
    finally:
        ledger.close()


def test_post_rollback_probe_interrupt_poisons_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(
        storage_module,
        "_transition_in_transaction",
        lambda *_arguments: (_ for _ in ()).throw(
            LedgerStorageError("INJECTED_FAILURE", "safe failure")
        ),
    )
    ledger = SQLiteLedger.open(directory)
    proxy = install_proxy(ledger, "rollback_probe_base")
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "ROLLBACK_FAILED"
        assert proxy.connection.in_transaction is False
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()


def test_rollback_wrapper_defensively_poisons_if_helper_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(
        storage_module,
        "_transition_in_transaction",
        lambda *_arguments: (_ for _ in ()).throw(
            LedgerStorageError("INJECTED_FAILURE", "safe failure")
        ),
    )
    monkeypatch.setattr(
        storage_module,
        "_rollback_to_clean",
        lambda *_arguments: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    ledger = SQLiteLedger.open(directory)
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "ROLLBACK_FAILED"
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()


def test_nested_failure_during_settlement_cannot_escape_unpoisoned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(
        storage_module,
        "_transition_in_transaction",
        lambda *_arguments: (_ for _ in ()).throw(
            LedgerStorageError("INJECTED_FAILURE", "safe failure")
        ),
    )
    monkeypatch.setattr(
        SQLiteLedger,
        "_settle_transaction_failure",
        lambda *_arguments, **_keywords: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    ledger = SQLiteLedger.open(directory)
    connection = ledger._connection
    assert connection is not None
    try:
        with pytest.raises(KeyboardInterrupt):
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert connection.in_transaction is True
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()

    with SQLiteLedger.open(directory) as reopened:
        assert table_snapshot(reopened) == ()


@pytest.mark.parametrize("mode", ["rollback_raise", "rollback_lingers"])
def test_rollback_uncertainty_poisons_but_close_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    original_insert = storage_module._insert_head

    def fail_after_head(connection: sqlite3.Connection, event: LedgerEvent) -> None:
        original_insert(connection, event)
        raise LedgerStorageError("INJECTED_FAILURE", "synthetic safe failure")

    monkeypatch.setattr(storage_module, "_insert_head", fail_after_head)
    ledger = SQLiteLedger.open(directory)
    install_proxy(ledger, mode)
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "ROLLBACK_FAILED"
        for operation in (
            ledger.status,
            lambda: ledger.get_note(tenant_id=TENANT_A, note_id=NOTE_A),
            lambda: ledger.create_note(
                tenant_id=TENANT_A,
                command_id=OTHER,
                content=CONTENT_V1,
            ),
        ):
            with pytest.raises(LedgerStorageError) as poisoned:
                operation()
            assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()

    monkeypatch.setattr(storage_module, "_insert_head", original_insert)
    with SQLiteLedger.open(directory) as reopened:
        assert reopened.get_head(tenant_id=TENANT_A, note_id=NOTE_A) is None
        assert table_snapshot(reopened) == ()


@pytest.mark.parametrize("failure_point", ["event", "head", "cas", "precommit", "verify"])
def test_injected_write_failures_rollback_without_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    with SQLiteLedger.open(directory) as ledger:
        if failure_point == "cas":
            ledger.create_note(tenant_id=TENANT_A, command_id=CREATE, content=CONTENT_V1)
        before = table_snapshot(ledger)

        if failure_point == "event":
            original = storage_module._insert_event

            def fail_after_event(connection: sqlite3.Connection, event: LedgerEvent) -> None:
                original(connection, event)
                raise LedgerStorageError("INJECTED_FAILURE", "safe failure")

            monkeypatch.setattr(storage_module, "_insert_event", fail_after_event)
        elif failure_point == "head":
            original_head = storage_module._insert_head

            def fail_after_head(connection: sqlite3.Connection, event: LedgerEvent) -> None:
                original_head(connection, event)
                raise LedgerStorageError("INJECTED_FAILURE", "safe failure")

            monkeypatch.setattr(storage_module, "_insert_head", fail_after_head)
        elif failure_point == "cas":
            original_cas = storage_module._cas_head

            def fail_after_cas(
                connection: sqlite3.Connection,
                *,
                previous: LedgerEvent,
                event: LedgerEvent,
            ) -> None:
                original_cas(connection, previous=previous, event=event)
                raise LedgerStorageError("INJECTED_FAILURE", "safe failure")

            monkeypatch.setattr(storage_module, "_cas_head", fail_after_cas)
        elif failure_point == "precommit":
            original_check = SQLiteLedger._assert_schema_cookie
            calls = 0

            def fail_third_check(
                self: SQLiteLedger,
                connection: sqlite3.Connection,
            ) -> None:
                nonlocal calls
                calls += 1
                original_check(self, connection)
                if calls == 3:
                    raise LedgerStorageError("MIGRATION_DRIFT", "safe drift")

            monkeypatch.setattr(SQLiteLedger, "_assert_schema_cookie", fail_third_check)
        else:
            monkeypatch.setattr(storage_module, "_load_event", lambda *_arguments: None)

        with pytest.raises(LedgerStorageError):
            if failure_point == "cas":
                ledger.revise_note(
                    tenant_id=TENANT_A,
                    note_id=NOTE_A,
                    command_id=REVISE,
                    expected_revision=1,
                    content=CONTENT_V2,
                )
            else:
                ledger.create_note(
                    tenant_id=TENANT_A,
                    command_id=CREATE,
                    content=CONTENT_V1,
                )
        assert table_snapshot(ledger) == before
        assert ledger.status().schema_version == 1


def test_sqlite_transition_error_is_mapped_after_verified_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    monkeypatch.setattr(
        storage_module,
        "_transition_in_transaction",
        lambda *_arguments: (_ for _ in ()).throw(sqlite_failure(sqlite3.SQLITE_CONSTRAINT)),
    )
    with SQLiteLedger.open(directory) as ledger:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert captured.value.code == "DATABASE_INTEGRITY"
        assert table_snapshot(ledger) == ()
        assert ledger.status().schema_version == 1


def test_read_error_paths_rollback_snapshot_and_map_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        monkeypatch.setattr(
            storage_module,
            "_load_head",
            lambda *_arguments: (_ for _ in ()).throw(sqlite_failure(sqlite3.SQLITE_IOERR)),
        )
        operations: tuple[Callable[[], object], ...] = (
            lambda: ledger.get_note(tenant_id=TENANT_A, note_id=NOTE_A),
            lambda: ledger.get_head(tenant_id=TENANT_A, note_id=NOTE_A),
        )
        for operation in operations:
            with pytest.raises(LedgerStorageError) as captured:
                operation()
            assert captured.value.code == "DATABASE_OPERATION_FAILED"

        monkeypatch.undo()
        monkeypatch.setattr(
            storage_module,
            "_history_in_transaction",
            lambda *_arguments, **_keywords: (_ for _ in ()).throw(
                sqlite_failure(sqlite3.SQLITE_IOERR)
            ),
        )
        with pytest.raises(LedgerStorageError) as sqlite_error:
            ledger.read_history(tenant_id=TENANT_A, note_id=NOTE_A)
        assert sqlite_error.value.code == "DATABASE_OPERATION_FAILED"
        assert ledger.status().schema_version == 1

        monkeypatch.setattr(
            storage_module,
            "_history_in_transaction",
            lambda *_arguments, **_keywords: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            ledger.read_history(tenant_id=TENANT_A, note_id=NOTE_A)
        assert ledger.status().schema_version == 1


def test_history_commit_with_unclean_state_poisons_connection(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    ledger = SQLiteLedger.open(directory)
    install_proxy(ledger, "commit_lingers")
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.read_history(tenant_id=TENANT_A, note_id=NOTE_A)
        assert captured.value.code == "TRANSACTION_STATE_UNCERTAIN"
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("primary", "code"),
    [
        (sqlite3.SQLITE_LOCKED, "LEDGER_BUSY"),
        (sqlite3.SQLITE_FULL, "DATABASE_FULL"),
        (sqlite3.SQLITE_READONLY, "DATABASE_READ_ONLY"),
        (sqlite3.SQLITE_CORRUPT, "DATABASE_INTEGRITY"),
        (sqlite3.SQLITE_NOTADB, "DATABASE_INTEGRITY"),
        (sqlite3.SQLITE_IOERR, "DATABASE_OPERATION_FAILED"),
    ],
)
def test_sqlite_primary_codes_have_stable_safe_mapping(primary: int, code: str) -> None:
    mapped = storage_module._mapped_database_error(sqlite_failure(primary))
    assert mapped.code == code
    assert_safe_error(mapped, "private sqlite diagnostic")


def test_default_clock_and_identifier_wrappers_are_canonical() -> None:
    timestamp = storage_module._utc_now_us()
    note_id = storage_module._new_note_id()
    assert type(timestamp) is int
    assert 0 <= timestamp <= MAX_RECORDED_AT_US
    assert note_id.startswith("nt_")
    assert len(note_id) == 35


def test_replay_projection_requires_a_current_terminal_safe_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = canonical_created_event()
    tombstone = created.tombstone(
        command_id=DELETE,
        recorded_at_us=11,
        reason=TombstoneReason.USER_REQUEST,
    )
    alternate_head = created.revise(
        command_id=REVISE,
        recorded_at_us=11,
        content=CONTENT_V2,
    )
    connection = cast(sqlite3.Connection, object())

    monkeypatch.setattr(storage_module, "_load_head", lambda *_arguments: None)
    with pytest.raises(LedgerStorageError) as absent:
        storage_module._verify_replayed_projection(connection, created)
    assert absent.value.code == "DATABASE_INTEGRITY"

    monkeypatch.setattr(storage_module, "_load_head", lambda *_arguments: alternate_head)
    with pytest.raises(LedgerStorageError) as stale_terminal:
        storage_module._verify_replayed_projection(connection, tombstone)
    assert stale_terminal.value.code == "DATABASE_INTEGRITY"


def test_internal_probe_and_cas_failures_map_to_integrity() -> None:
    created = canonical_created_event()
    revised = created.revise(
        command_id=REVISE,
        recorded_at_us=11,
        content=CONTENT_V2,
    )

    class Result:
        rowcount = 0

        def fetchone(self) -> None:
            return None

    class FakeConnection:
        def execute(self, _statement: str, _parameters: object = ()) -> Result:
            return Result()

    connection = cast(sqlite3.Connection, FakeConnection())
    with pytest.raises(LedgerStorageError) as cas:
        storage_module._cas_head(connection, previous=created, event=revised)
    assert cas.value.code == "DATABASE_INTEGRITY"
    with pytest.raises(LedgerStorageError) as inventory:
        storage_module._note_storage_exists(connection, TENANT_A, NOTE_A)
    assert inventory.value.code == "DATABASE_INTEGRITY"
    with pytest.raises(LedgerStorageError) as probe:
        storage_module._load_head(connection, TENANT_A, NOTE_A)
    assert probe.value.code == "DATABASE_INTEGRITY"


def test_row_decoders_reject_blob_column_and_head_drift() -> None:
    event = canonical_created_event()
    base: dict[str, object] = {
        "tenant_id": event.tenant_id,
        "note_id": event.note_id,
        "revision": event.revision,
        "command_id": event.command_id,
        "recorded_at_us": event.recorded_at_us,
        "kind": event.kind.value,
        "previous_revision": None,
        "previous_event_hash": event.previous_event_hash,
        "event_hash": event.event_hash,
        "schema_version": event.schema_version,
        "event_bytes": event.to_bytes(),
    }

    for replacement in (
        {"event_bytes": "not bytes"},
        {"event_bytes": b"{}"},
        {"tenant_id": TENANT_B},
        {"note_id": NOTE_B},
        {"revision": 2},
        {"command_id": OTHER},
        {"recorded_at_us": 11},
        {"kind": EventKind.REVISED.value},
        {"previous_revision": 1},
        {
            "previous_event_hash": (
                "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
            )
        },
        {"event_hash": ("sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")},
        {"schema_version": 2},
    ):
        row = dict(base)
        row.update(replacement)
        with pytest.raises(LedgerStorageError) as captured:
            storage_module._decode_event_row(cast(sqlite3.Row, row))
        assert captured.value.code == "DATABASE_INTEGRITY"

    head_base = dict(base)
    head_base.update(
        {
            "head_tenant_id": event.tenant_id,
            "head_note_id": event.note_id,
            "head_revision": event.revision,
            "head_event_hash": event.event_hash,
            "head_updated_at_us": event.recorded_at_us,
            "head_is_tombstoned": 0,
            "latest_revision": event.revision,
            "latest_event_hash": event.event_hash,
        }
    )
    assert storage_module._decode_head_row(cast(sqlite3.Row, head_base)) == event
    for replacement in (
        {"head_tenant_id": TENANT_B},
        {"head_note_id": NOTE_B},
        {"head_revision": 2},
        {
            "head_event_hash": (
                "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
            )
        },
        {"head_updated_at_us": 11},
        {"head_is_tombstoned": 1},
        {"latest_revision": 2},
        {
            "latest_event_hash": (
                "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
            )
        },
    ):
        head_row = dict(head_base)
        head_row.update(replacement)
        with pytest.raises(LedgerStorageError) as head:
            storage_module._decode_head_row(cast(sqlite3.Row, head_row))
        assert head.value.code == "DATABASE_INTEGRITY"


@pytest.mark.parametrize("corruption", ["orphan", "blob", "column", "head"])
def test_live_reads_detect_involved_storage_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    monkeypatch.setattr(storage_module, "_utc_now_us", lambda: 10)
    with SQLiteLedger.open(directory) as ledger:
        event = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=CREATE,
            content=CONTENT_V1,
        ).event
        connection = ledger._connection
        assert connection is not None
        if corruption == "orphan":
            connection.execute(
                "DELETE FROM note_heads WHERE tenant_id = ? AND note_id = ?",
                (TENANT_A, NOTE_A),
            )
        elif corruption == "blob":
            connection.execute(
                """
                UPDATE ledger_events
                SET event_bytes = ?
                WHERE tenant_id = ? AND note_id = ? AND revision = 1
                """,
                (b"{}", TENANT_A, NOTE_A),
            )
        elif corruption == "column":
            connection.execute(
                """
                UPDATE ledger_events
                SET recorded_at_us = ?
                WHERE tenant_id = ? AND note_id = ? AND revision = 1
                """,
                (event.recorded_at_us + 1, TENANT_A, NOTE_A),
            )
        else:
            connection.execute(
                """
                UPDATE note_heads
                SET updated_at_us = ?
                WHERE tenant_id = ? AND note_id = ?
                """,
                (event.recorded_at_us + 1, TENANT_A, NOTE_A),
            )
        with pytest.raises(LedgerStorageError) as captured:
            ledger.get_head(tenant_id=TENANT_A, note_id=NOTE_A)
        assert captured.value.code == "DATABASE_INTEGRITY"
        with pytest.raises(LedgerStorageError) as replay:
            ledger.create_note(
                tenant_id=TENANT_A,
                command_id=CREATE,
                content=CONTENT_V1,
            )
        assert replay.value.code == "DATABASE_INTEGRITY"


def test_history_detects_missing_anchor_final_head_and_chain_break(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = canonical_created_event()

    class EmptyResult:
        def fetchall(self) -> list[sqlite3.Row]:
            return []

    class EmptyConnection:
        def execute(self, _statement: str, _parameters: object = ()) -> EmptyResult:
            return EmptyResult()

    monkeypatch.setattr(storage_module, "_load_head", lambda *_arguments: created)
    with pytest.raises(LedgerStorageError) as anchor:
        storage_module._history_in_transaction(
            cast(sqlite3.Connection, EmptyConnection()),
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            after_revision=0,
            limit=10,
        )
    assert anchor.value.code == "DATABASE_INTEGRITY"

    directory = secure_directory(tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(storage_module, "_new_note_id", lambda: NOTE_A)
    ticks = Sequence(iter((10, 11)))
    monkeypatch.setattr(storage_module, "_utc_now_us", ticks)
    with SQLiteLedger.open(directory) as ledger:
        ledger.create_note(tenant_id=TENANT_A, command_id=CREATE, content=CONTENT_V1)
        ledger.revise_note(
            tenant_id=TENANT_A,
            note_id=NOTE_A,
            command_id=REVISE,
            expected_revision=1,
            content=CONTENT_V2,
        )
        connection = ledger._connection
        assert connection is not None
        monkeypatch.setattr(storage_module, "_load_head", lambda *_arguments: created)
        with pytest.raises(LedgerStorageError) as final:
            storage_module._history_in_transaction(
                connection,
                tenant_id=TENANT_A,
                note_id=NOTE_A,
                after_revision=0,
                limit=10,
            )
        assert final.value.code == "DATABASE_INTEGRITY"

    with pytest.raises(LedgerStorageError) as chain:
        storage_module._verify_event_sequence((created, created))
    assert chain.value.code == "DATABASE_INTEGRITY"
    clean_connection = sqlite3.connect(":memory:")
    try:
        assert storage_module._rollback_to_clean(clean_connection) is True
    finally:
        clean_connection.close()


def test_history_sequence_rejects_time_hash_revision_and_terminal_breaks() -> None:
    created = canonical_created_event()
    wrong_hash = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    tombstone = created.tombstone(
        command_id=DELETE,
        recorded_at_us=11,
        reason=TombstoneReason.USER_REQUEST,
    )
    cases = (
        crafted_revision(
            created,
            revision=2,
            recorded_at_us=9,
            previous_event_hash=created.event_hash,
        ),
        crafted_revision(
            created,
            revision=3,
            recorded_at_us=11,
            previous_event_hash=created.event_hash,
        ),
        crafted_revision(
            created,
            revision=2,
            recorded_at_us=11,
            previous_event_hash=wrong_hash,
        ),
        crafted_revision(
            tombstone,
            revision=3,
            recorded_at_us=12,
            previous_event_hash=tombstone.event_hash,
        ),
    )
    parents = (created, created, created, tombstone)
    for parent, successor in zip(parents, cases, strict=True):
        with pytest.raises(LedgerStorageError) as captured:
            storage_module._verify_event_sequence((parent, successor))
        assert captured.value.code == "DATABASE_INTEGRITY"


def test_history_query_plans_use_bounded_indexes(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        connection = ledger._connection
        assert connection is not None
        command_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + storage_module._LOAD_COMMAND_EVENT_SQL,
                (TENANT_A, CREATE),
            )
        )
        history_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + storage_module._LOAD_HISTORY_SQL,
                (TENANT_A, NOTE_A, 1, 10),
            )
        )
        assert "sqlite_autoindex_ledger_events_2" in command_plan
        assert "USING PRIMARY KEY" in history_plan
        assert "USE TEMP B-TREE" not in command_plan + history_plan
