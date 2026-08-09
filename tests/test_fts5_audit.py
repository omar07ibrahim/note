from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

import recall_ledger._ledger_operations as operations_module
import recall_ledger.storage as storage_module
from recall_ledger import (
    CommandId,
    Fts5CandidateAudit,
    LedgerStorageError,
    NoteContent,
    NoteId,
    SQLiteLedger,
    TenantId,
    TombstoneReason,
    compile_lexical_query,
)
from recall_ledger.storage import (
    FTS5_CANDIDATE_SCHEMA_VERSION,
    FTS5_CANDIDATE_TOKENIZER,
)

TENANT_A = TenantId("tn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
TENANT_B = TenantId("tn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")


def secure_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def note_id(index: int) -> NoteId:
    return NoteId(f"nt_{index:032x}")


def command_id(index: int) -> CommandId:
    return CommandId(f"cmd_{index:032x}")


def install_storage_values(
    monkeypatch: pytest.MonkeyPatch,
    *,
    note_ids: tuple[NoteId, ...],
    timestamps: tuple[int, ...],
) -> None:
    ids = iter(note_ids)
    clock = iter(timestamps)
    monkeypatch.setattr(operations_module, "_new_note_id", lambda: next(ids))
    monkeypatch.setattr(operations_module, "_utc_now_us", lambda: next(clock))


def test_fts5_audit_matches_reference_and_is_tenant_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(3), note_id(1), note_id(2)),
        timestamps=(30, 10, 20),
    )
    directory = secure_directory(tmp_path)

    with SQLiteLedger.open(directory) as ledger:
        first = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=command_id(1),
            content=NoteContent("Alpha OR", "beta"),
        ).event
        second = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=command_id(2),
            content=NoteContent("alpha", "beta"),
        ).event
        ledger.create_note(
            tenant_id=TENANT_B,
            command_id=command_id(1),
            content=NoteContent("Alpha OR", "other tenant"),
        )
        main_cookie = cast(sqlite3.Connection, ledger._connection).execute(
            "PRAGMA schema_version"
        ).fetchone()[0]

        audit = ledger.audit_fts5_candidates(tenant_id=TENANT_A, query="ALPHA or")
        assert isinstance(audit, Fts5CandidateAudit)
        assert audit.tenant_id == TENANT_A
        assert audit.query == compile_lexical_query("ALPHA or")
        assert audit.schema_version == FTS5_CANDIDATE_SCHEMA_VERSION == 1
        assert audit.tokenizer == FTS5_CANDIDATE_TOKENIZER == "ascii"
        assert audit.sqlite_version == sqlite3.sqlite_version
        assert 1 <= len(audit.sqlite_source_id) <= 256
        assert audit.scanned_heads == audit.indexed_live_notes == 2
        assert audit.scanned_content_bytes > 0
        assert audit.oracle_match_note_ids == (first.note_id,)
        assert audit.candidate_note_ids == audit.oracle_match_note_ids
        assert second.note_id not in audit.candidate_note_ids

        connection = cast(sqlite3.Connection, ledger._connection)
        assert (
            connection.execute(
                "SELECT count(*) FROM temp.sqlite_schema "
                "WHERE name = 'recall_ledger_fts5_candidates'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA schema_version").fetchone()[0] == main_cookie


def test_fts5_audit_handles_empty_tenant(
    tmp_path: Path,
) -> None:
    with SQLiteLedger.open(secure_directory(tmp_path)) as ledger:
        audit = ledger.audit_fts5_candidates(tenant_id=TENANT_A, query="alpha")
    assert audit.scanned_heads == audit.indexed_live_notes == 0
    assert audit.scanned_content_bytes == 0
    assert audit.oracle_match_note_ids == audit.candidate_note_ids == ()


def test_fts5_audit_uses_current_heads_and_excludes_tombstones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1), note_id(2)),
        timestamps=(10, 20, 30, 40),
    )
    with SQLiteLedger.open(secure_directory(tmp_path)) as ledger:
        first = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=command_id(1),
            content=NoteContent("obsolete", "alpha"),
        ).event
        second = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=command_id(2),
            content=NoteContent("remove", "alpha"),
        ).event
        revised = ledger.revise_note(
            tenant_id=TENANT_A,
            note_id=first.note_id,
            command_id=command_id(3),
            expected_revision=1,
            content=NoteContent("current", "alpha"),
        ).event
        ledger.tombstone_note(
            tenant_id=TENANT_A,
            note_id=second.note_id,
            command_id=command_id(4),
            expected_revision=1,
            reason=TombstoneReason.USER_REQUEST,
        )

        current = ledger.audit_fts5_candidates(tenant_id=TENANT_A, query="current")
        assert current.candidate_note_ids == (revised.note_id,)
        assert ledger.audit_fts5_candidates(
            tenant_id=TENANT_A, query="obsolete"
        ).candidate_note_ids == ()
        alpha = ledger.audit_fts5_candidates(tenant_id=TENANT_A, query="alpha")
        assert alpha.scanned_heads == 2
        assert alpha.indexed_live_notes == 1
        assert alpha.candidate_note_ids == (revised.note_id,)


def test_fts5_audit_fails_closed_on_candidate_drift_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1),),
        timestamps=(10,),
    )
    with SQLiteLedger.open(secure_directory(tmp_path)) as ledger:
        event = ledger.create_note(
            tenant_id=TENANT_A,
            command_id=command_id(1),
            content=NoteContent("alpha", ""),
        ).event
        monkeypatch.setattr(
            operations_module,
            "_fts5_candidate_note_ids",
            lambda _connection, _query, _events: (),
        )
        with pytest.raises(LedgerStorageError) as captured:
            ledger.audit_fts5_candidates(tenant_id=TENANT_A, query="alpha")
        assert captured.value.code == "FTS5_CANDIDATE_DRIFT"
        assert ledger.search_notes(tenant_id=TENANT_A, query="alpha").hits[
            0
        ].citation.note_id == event.note_id


def test_fts5_sqlite_error_is_sanitized_and_connection_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SQLiteLedger.open(secure_directory(tmp_path)) as ledger:

        def fail(
            _connection: sqlite3.Connection,
            _query: object,
            _events: object,
        ) -> tuple[NoteId, ...]:
            raise sqlite3.OperationalError("host detail must not escape")

        monkeypatch.setattr(operations_module, "_fts5_candidate_note_ids", fail)
        with pytest.raises(LedgerStorageError) as captured:
            ledger.audit_fts5_candidates(tenant_id=TENANT_A, query="alpha")
        assert captured.value.code == "DATABASE_OPERATION_FAILED"
        assert "host detail" not in str(captured.value)
        assert ledger.search_notes(tenant_id=TENANT_A, query="alpha").hits == ()


def test_fts5_terminal_transaction_state_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter((False, True, True))
    monkeypatch.setattr(storage_module, "_transaction_active", lambda _connection: next(states))
    ledger = SQLiteLedger.open(secure_directory(tmp_path))
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.audit_fts5_candidates(tenant_id=TENANT_A, query="alpha")
        assert captured.value.code == "TRANSACTION_STATE_UNCERTAIN"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "values",
    [
        (1,),
        ("invalid",),
        (note_id(1), note_id(1)),
        (note_id(2), note_id(1)),
    ],
)
def test_candidate_identity_validation_rejects_hostile_rows(
    values: tuple[object, ...],
) -> None:
    with pytest.raises(LedgerStorageError) as captured:
        operations_module._validated_fts5_candidate_ids(values)
    assert captured.value.code == "DATABASE_INTEGRITY"


def test_candidate_identity_validation_accepts_exact_sorted_ids() -> None:
    values = (note_id(1), note_id(2))
    assert operations_module._validated_fts5_candidate_ids(values) == values
