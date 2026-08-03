from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

import recall_ledger._ledger_operations as operations_module
import recall_ledger.storage as storage_module
from recall_ledger import (
    CommandId,
    ContractViolation,
    LedgerEvent,
    LedgerStorageError,
    NoteContent,
    NoteId,
    RetrievalContractError,
    SearchCitation,
    SearchHit,
    SearchResults,
    SQLiteLedger,
    TenantId,
    TombstoneReason,
    compile_lexical_query,
)
from recall_ledger.storage import DATABASE_FILENAME, MAX_SEARCH_LIMIT

TENANT_A = TenantId("tn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
TENANT_B = TenantId("tn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
TENANT_C = TenantId("tn_cccccccccccccccccccccccccccccccc")


def secure_directory(tmp_path: Path, name: str = "data") -> Path:
    directory = tmp_path / name
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


def content_bytes(content: NoteContent) -> int:
    return (
        len(content.title.encode())
        + len(content.body.encode())
        + sum(len(tag.encode()) for tag in content.tags)
    )


def create_notes(
    ledger: SQLiteLedger,
    *,
    tenant_id: TenantId,
    contents: tuple[NoteContent, ...],
    command_offset: int = 1,
) -> tuple[LedgerEvent, ...]:
    return tuple(
        ledger.create_note(
            tenant_id=tenant_id,
            command_id=command_id(command_offset + index),
            content=content,
        ).event
        for index, content in enumerate(contents)
    )


def test_reference_search_ranks_full_corpus_and_returns_current_citations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = (
        NoteContent("alpha beta", "title phrase"),
        NoteContent("tag match", "", ("alpha beta",)),
        NoteContent("body match", "alpha beta"),
        NoteContent("split body", "alpha gap beta"),
        NoteContent("off query", "gamma"),
    )
    install_storage_values(
        monkeypatch,
        note_ids=tuple(note_id(index) for index in (5, 4, 3, 2, 1)),
        timestamps=(10, 30, 40, 50, 60),
    )
    directory = secure_directory(tmp_path)

    with SQLiteLedger.open(directory) as ledger:
        events = create_notes(ledger, tenant_id=TENANT_A, contents=contents)
        results = ledger.search_notes(tenant_id=TENANT_A, query="alpha beta", limit=2)

        assert results == SearchResults(
            tenant_id=TENANT_A,
            query=compile_lexical_query("alpha beta"),
            limit=2,
            total_matches=4,
            scanned_heads=5,
            scanned_live_notes=5,
            scanned_content_bytes=sum(content_bytes(content) for content in contents),
            hits=results.hits,
        )
        assert tuple(hit.content for hit in results.hits) == contents[:2]
        assert tuple(hit.score.total for hit in results.hits) == (48, 32)
        assert results.hits == tuple(
            SearchHit(
                citation=SearchCitation(
                    tenant_id=event.tenant_id,
                    note_id=event.note_id,
                    revision=event.revision,
                    event_hash=event.event_hash,
                ),
                recorded_at_us=event.recorded_at_us,
                content=cast(NoteContent, event.content),
                score=results.hits[index].score,
            )
            for index, event in enumerate(events[:2])
        )
        for hit in results.hits:
            head = ledger.get_head(
                tenant_id=hit.citation.tenant_id,
                note_id=hit.citation.note_id,
            )
            assert head is not None
            assert (hit.citation.revision, hit.citation.event_hash, hit.content) == (
                head.revision,
                head.event_hash,
                head.content,
            )


def test_search_ties_use_recency_then_note_id_and_survive_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = (note_id(3), note_id(2), note_id(1))
    install_storage_values(monkeypatch, note_ids=ids, timestamps=(20, 10, 20))
    directory = secure_directory(tmp_path)

    with SQLiteLedger.open(directory) as ledger:
        create_notes(
            ledger,
            tenant_id=TENANT_A,
            contents=(NoteContent("match", ""),) * 3,
        )
        before = ledger.search_notes(tenant_id=TENANT_A, query="match")
        assert tuple(hit.citation.note_id for hit in before.hits) == (
            note_id(1),
            note_id(3),
            note_id(2),
        )

    with SQLiteLedger.open(directory) as reopened:
        assert reopened.search_notes(tenant_id=TENANT_A, query="match") == before


def test_search_inventory_heads_and_scoring_share_one_read_snapshot(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1),),
        timestamps=(10, 20),
    )
    directory = secure_directory(tmp_path)
    reader = SQLiteLedger.open(directory)
    created = create_notes(
        reader,
        tenant_id=TENANT_A,
        contents=(NoteContent("oldterm", "snapshot"),),
    )[0]

    start_writer = threading.Event()
    writer_cas_done = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []
    writer_events: list[LedgerEvent] = []
    original_load_head = operations_module._load_head
    original_cas_head = operations_module._cas_head
    reader_thread = threading.get_ident()
    reader_intercepted = False

    def load_head_after_writer_cas(
        connection: sqlite3.Connection,
        tenant_id: TenantId,
        current_note_id: NoteId,
    ) -> LedgerEvent | None:
        nonlocal reader_intercepted
        if threading.get_ident() == reader_thread and not reader_intercepted:
            reader_intercepted = True
            start_writer.set()
            assert writer_cas_done.wait(timeout=5)
        return original_load_head(connection, tenant_id, current_note_id)

    def signal_after_cas(
        connection: sqlite3.Connection,
        *,
        previous: LedgerEvent,
        event: LedgerEvent,
    ) -> None:
        original_cas_head(connection, previous=previous, event=event)
        writer_cas_done.set()

    monkeypatch.setattr(operations_module, "_load_head", load_head_after_writer_cas)
    monkeypatch.setattr(operations_module, "_cas_head", signal_after_cas)

    def revise_in_writer() -> None:
        try:
            assert start_writer.wait(timeout=5)
            with SQLiteLedger.open(directory) as writer:
                writer_events.append(
                    writer.revise_note(
                        tenant_id=TENANT_A,
                        note_id=created.note_id,
                        command_id=command_id(2),
                        expected_revision=1,
                        content=NoteContent("currentterm", "snapshot"),
                    ).event
                )
        except BaseException as error:
            writer_errors.append(error)
        finally:
            writer_finished.set()

    writer_thread = threading.Thread(target=revise_in_writer)
    writer_thread.start()
    try:
        old_snapshot = reader.search_notes(tenant_id=TENANT_A, query="oldterm")
        assert old_snapshot.total_matches == 1
        assert old_snapshot.hits[0].citation.revision == 1
        assert old_snapshot.hits[0].content.title == "oldterm"
        assert writer_finished.wait(timeout=5)
        writer_thread.join(timeout=5)
        assert not writer_thread.is_alive()
        assert writer_errors == []
        assert writer_events[0].revision == 2

        assert reader.search_notes(tenant_id=TENANT_A, query="oldterm").hits == ()
        current = reader.search_notes(tenant_id=TENANT_A, query="currentterm")
        assert current.hits[0].citation.revision == 2
        assert current.hits[0].citation.event_hash == writer_events[0].event_hash
    finally:
        start_writer.set()
        writer_thread.join(timeout=5)
        reader.close()


def test_empty_tenant_and_other_tenant_stuffing_do_not_change_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1), note_id(2)),
        timestamps=(10, 20),
    )
    with SQLiteLedger.open(directory) as ledger:
        create_notes(
            ledger,
            tenant_id=TENANT_A,
            contents=(NoteContent("alpha", "one"), NoteContent("alpha", "two")),
        )
        baseline = ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        empty = ledger.search_notes(tenant_id=TENANT_C, query="alpha")
        assert empty.total_matches == empty.scanned_heads == empty.scanned_live_notes == 0
        assert empty.scanned_content_bytes == 0
        assert empty.hits == ()

        install_storage_values(
            monkeypatch,
            note_ids=(note_id(1), note_id(2), note_id(3)),
            timestamps=(100, 101, 102),
        )
        create_notes(
            ledger,
            tenant_id=TENANT_B,
            contents=(NoteContent("alpha alpha alpha", "alpha"),) * 3,
            command_offset=1,
        )
        assert ledger.search_notes(tenant_id=TENANT_A, query="alpha") == baseline


def test_revision_removes_stale_terms_and_tombstone_hides_retained_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1),),
        timestamps=(10, 20, 30),
    )
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        created = create_notes(
            ledger,
            tenant_id=TENANT_A,
            contents=(NoteContent("obsolete", "alpha"),),
        )[0]
        revised = ledger.revise_note(
            tenant_id=TENANT_A,
            note_id=created.note_id,
            command_id=command_id(2),
            expected_revision=1,
            content=NoteContent("current", "alpha"),
        ).event
        assert ledger.search_notes(tenant_id=TENANT_A, query="obsolete").hits == ()
        current = ledger.search_notes(tenant_id=TENANT_A, query="current")
        assert current.total_matches == 1
        assert current.hits[0].citation.revision == 2
        assert current.hits[0].citation.event_hash == revised.event_hash

        history = ledger.read_history(tenant_id=TENANT_A, note_id=created.note_id)
        assert history is not None
        assert history.events[0].content == NoteContent("obsolete", "alpha")
        ledger.tombstone_note(
            tenant_id=TENANT_A,
            note_id=created.note_id,
            command_id=command_id(3),
            expected_revision=2,
            reason=TombstoneReason.USER_REQUEST,
        )
        hidden = ledger.search_notes(tenant_id=TENANT_A, query="current")
        assert hidden.total_matches == 0
        assert hidden.scanned_heads == 1
        assert hidden.scanned_live_notes == hidden.scanned_content_bytes == 0
        retained = ledger.read_history(tenant_id=TENANT_A, note_id=created.note_id)
        assert retained is not None
        assert tuple(event.revision for event in retained.events) == (1, 2, 3)


@pytest.mark.parametrize("limit", (False, 0, -1, MAX_SEARCH_LIMIT + 1, 1.0, "1"))
def test_search_rejects_nonexact_or_out_of_range_limits(
    tmp_path: Path,
    limit: object,
) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha", limit=limit)  # type: ignore[arg-type]
        assert captured.value.code == "INVALID_SEARCH_LIMIT"


def test_search_validates_tenant_and_raw_query_before_begin(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        with pytest.raises(ContractViolation) as tenant:
            ledger.search_notes(tenant_id=TenantId("bad"), query="alpha")
        assert tenant.value.code == "INVALID_IDENTIFIER"

        for value, code in (
            (object(), "INVALID_QUERY_TYPE"),
            ("\ud800", "INVALID_QUERY_UNICODE"),
            ("<> _ -", "EMPTY_QUERY"),
        ):
            with pytest.raises(RetrievalContractError) as query:
                ledger.search_notes(tenant_id=TENANT_A, query=value)  # type: ignore[arg-type]
            assert query.value.code == code

        connection = ledger._connection
        assert connection is not None
        assert connection.in_transaction is False


def test_search_bounds_include_tombstones_and_count_live_utf8_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1), note_id(2)),
        timestamps=(10, 20, 30),
    )
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        events = create_notes(
            ledger,
            tenant_id=TENANT_A,
            contents=(NoteContent("café", "alpha"), NoteContent("beta", "")),
        )
        ledger.tombstone_note(
            tenant_id=TENANT_A,
            note_id=events[1].note_id,
            command_id=command_id(3),
            expected_revision=1,
            reason=TombstoneReason.RETENTION_POLICY,
        )
        exact_bytes = content_bytes(cast(NoteContent, events[0].content))
        monkeypatch.setattr(operations_module, "MAX_SEARCH_LIVE_CONTENT_BYTES", exact_bytes)
        exact = ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert exact.scanned_heads == 2
        assert exact.scanned_live_notes == 1
        assert exact.scanned_content_bytes == exact_bytes

        monkeypatch.setattr(operations_module, "MAX_SEARCH_LIVE_CONTENT_BYTES", exact_bytes - 1)
        with pytest.raises(LedgerStorageError) as corpus:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert corpus.value.code == "SEARCH_CORPUS_TOO_LARGE"
        assert ledger.status().schema_version == 1

        monkeypatch.setattr(operations_module, "MAX_SEARCH_LIVE_CONTENT_BYTES", exact_bytes)
        monkeypatch.setattr(operations_module, "MAX_SEARCH_HEADS", 1)
        with pytest.raises(LedgerStorageError) as inventory:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert inventory.value.code == "SEARCH_INVENTORY_TOO_LARGE"


@pytest.mark.parametrize("corruption", ("orphan", "blob", "column", "head", "flag", "stale"))
def test_search_checks_every_involved_head_and_event_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1), note_id(2)),
        timestamps=(10, 20),
    )
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        events = create_notes(
            ledger,
            tenant_id=TENANT_A,
            contents=(NoteContent("alpha", "match"), NoteContent("off query", "gamma")),
        )
        connection = ledger._connection
        assert connection is not None
        target = events[1]
        if corruption == "orphan":
            connection.execute(
                "DELETE FROM note_heads WHERE tenant_id = ? AND note_id = ?",
                (TENANT_A, target.note_id),
            )
        elif corruption == "blob":
            connection.execute(
                "UPDATE ledger_events SET event_bytes = ? WHERE tenant_id = ? AND note_id = ?",
                (b"{}", TENANT_A, target.note_id),
            )
        elif corruption == "column":
            connection.execute(
                "UPDATE ledger_events SET recorded_at_us = recorded_at_us + 1 "
                "WHERE tenant_id = ? AND note_id = ?",
                (TENANT_A, target.note_id),
            )
        elif corruption == "head":
            connection.execute(
                "UPDATE note_heads SET updated_at_us = updated_at_us + 1 "
                "WHERE tenant_id = ? AND note_id = ?",
                (TENANT_A, target.note_id),
            )
        elif corruption == "flag":
            connection.execute(
                "UPDATE note_heads SET is_tombstoned = 1 WHERE tenant_id = ? AND note_id = ?",
                (TENANT_A, target.note_id),
            )
        else:
            successor = target.revise(
                command_id=command_id(9),
                recorded_at_us=target.recorded_at_us + 1,
                content=NoteContent("later", "gamma"),
            )
            operations_module._insert_event(connection, successor)

        with pytest.raises(LedgerStorageError) as captured:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha", limit=1)
        assert captured.value.code == "DATABASE_INTEGRITY"
        assert ledger.status().schema_version == 1


def test_orphan_detection_is_tenant_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_storage_values(
        monkeypatch,
        note_ids=(note_id(1), note_id(1)),
        timestamps=(10, 20),
    )
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        create_notes(ledger, tenant_id=TENANT_A, contents=(NoteContent("alpha", ""),))
        create_notes(ledger, tenant_id=TENANT_B, contents=(NoteContent("alpha", ""),))
        connection = ledger._connection
        assert connection is not None
        connection.execute(
            "DELETE FROM note_heads WHERE tenant_id = ? AND note_id = ?",
            (TENANT_A, note_id(1)),
        )
        with pytest.raises(LedgerStorageError) as tenant_a:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert tenant_a.value.code == "DATABASE_INTEGRITY"
        assert ledger.search_notes(tenant_id=TENANT_B, query="alpha").total_matches == 1


class QueryResult:
    def __init__(
        self,
        *,
        all_rows: list[dict[str, object]] | None = None,
        one_row: dict[str, object] | None = None,
    ) -> None:
        self._all_rows = [] if all_rows is None else all_rows
        self._one_row = one_row

    def fetchall(self) -> list[dict[str, object]]:
        return self._all_rows

    def fetchone(self) -> dict[str, object] | None:
        return self._one_row


class ScriptedConnection:
    def __init__(
        self,
        inventory: list[dict[str, object]],
        event_rows: Iterator[dict[str, object] | None],
    ) -> None:
        self._inventory = inventory
        self._event_rows = event_rows
        self._calls = 0

    def execute(self, _statement: str, _parameters: object = ()) -> QueryResult:
        self._calls += 1
        if self._calls == 1:
            return QueryResult(all_rows=self._inventory)
        return QueryResult(one_row=next(self._event_rows))


@pytest.mark.parametrize(
    ("inventory", "event_rows"),
    (
        ([{"note_id": "bad"}], ()),
        ([{"note_id": note_id(1)}, {"note_id": note_id(1)}], ()),
        ([{"note_id": note_id(1)}], ({"note_id": "bad"},)),
        ([{"note_id": note_id(1)}], ({"note_id": ""},)),
        ([{"note_id": note_id(1)}], ({"note_id": note_id(2)},)),
        (
            [{"note_id": note_id(1)}],
            ({"note_id": note_id(1)}, {"note_id": note_id(1)}),
        ),
    ),
)
def test_search_rejects_invalid_or_nonprogressing_inventory_rows(
    inventory: list[dict[str, object]],
    event_rows: tuple[dict[str, object], ...],
) -> None:
    connection = ScriptedConnection(inventory, iter(event_rows))
    with pytest.raises(LedgerStorageError) as captured:
        operations_module._search_in_transaction(
            cast(sqlite3.Connection, connection),
            tenant_id=TENANT_A,
            query=compile_lexical_query("alpha"),
            limit=1,
        )
    assert captured.value.code == "DATABASE_INTEGRITY"


def test_search_rejects_disappearing_or_noncontent_live_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(monkeypatch, note_ids=(note_id(1),), timestamps=(10,))
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        event = create_notes(
            ledger,
            tenant_id=TENANT_A,
            contents=(NoteContent("alpha", ""),),
        )[0]
        monkeypatch.setattr(operations_module, "_load_head", lambda *_arguments: None)
        with pytest.raises(LedgerStorageError) as missing:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert missing.value.code == "DATABASE_INTEGRITY"

        object.__setattr__(event, "content", None)
        monkeypatch.setattr(operations_module, "_load_head", lambda *_arguments: event)
        with pytest.raises(LedgerStorageError) as content:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert content.value.code == "DATABASE_INTEGRITY"


def sqlite_failure(primary_code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("private sqlite diagnostic")
    error.sqlite_errorcode = primary_code
    return error


@pytest.mark.parametrize("failure", (sqlite_failure(sqlite3.SQLITE_IOERR), KeyboardInterrupt()))
def test_search_body_failures_rollback_and_leave_connection_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    directory = secure_directory(tmp_path)
    monkeypatch.setattr(
        operations_module,
        "_search_in_transaction",
        lambda *_arguments, **_keywords: (_ for _ in ()).throw(failure),
    )
    with SQLiteLedger.open(directory) as ledger:
        if isinstance(failure, sqlite3.Error):
            with pytest.raises(LedgerStorageError) as captured:
                ledger.search_notes(tenant_id=TENANT_A, query="alpha")
            assert captured.value.code == "DATABASE_OPERATION_FAILED"
        else:
            with pytest.raises(KeyboardInterrupt):
                ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert ledger.status().schema_version == 1


def test_search_retrieval_failure_is_not_converted_or_partially_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(monkeypatch, note_ids=(note_id(1),), timestamps=(10,))
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        create_notes(ledger, tenant_id=TENANT_A, contents=(NoteContent("alpha", ""),))
        monkeypatch.setattr(operations_module, "MAX_SEARCH_LIVE_CONTENT_BYTES", 1_000)
        monkeypatch.setattr("recall_ledger.retrieval.MAX_CONTENT_TOKEN_STREAM_BYTES", 1)
        with pytest.raises(RetrievalContractError) as captured:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert captured.value.code == "CONTENT_TOKEN_STREAM_TOO_LARGE"
        assert ledger.status().schema_version == 1


def test_search_unclean_commit_poisons_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = secure_directory(tmp_path)
    states = iter((False, True, True))
    monkeypatch.setattr(storage_module, "_transaction_active", lambda _connection: next(states))
    ledger = SQLiteLedger.open(directory)
    try:
        with pytest.raises(LedgerStorageError) as captured:
            ledger.search_notes(tenant_id=TENANT_A, query="alpha")
        assert captured.value.code == "TRANSACTION_STATE_UNCERTAIN"
        with pytest.raises(LedgerStorageError) as poisoned:
            ledger.status()
        assert poisoned.value.code == "CONNECTION_POISONED"
    finally:
        ledger.close()


def test_search_queries_use_bounded_covering_indexes(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        connection = ledger._connection
        assert connection is not None
        head_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + operations_module._LOAD_TENANT_HEAD_IDS_SQL,
                (TENANT_A, 2),
            )
        )
        first_event_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + operations_module._LOAD_FIRST_TENANT_EVENT_NOTE_ID_SQL,
                (TENANT_A,),
            )
        )
        next_event_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + operations_module._LOAD_NEXT_TENANT_EVENT_NOTE_ID_SQL,
                (TENANT_A, ""),
            )
        )

    assert "note_heads_all_page" in head_plan
    assert "ledger_events" in first_event_plan
    assert "tenant_id=?" in first_event_plan
    assert "ledger_events" in next_event_plan
    assert "tenant_id=? AND note_id>?" in next_event_plan
    assert "TEMP B-TREE" not in head_plan + first_event_plan + next_event_plan


def test_inert_operator_query_cannot_broaden_reference_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_storage_values(monkeypatch, note_ids=(note_id(1),), timestamps=(10,))
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        create_notes(ledger, tenant_id=TENANT_A, contents=(NoteContent("alpha", ""),))
        result = ledger.search_notes(tenant_id=TENANT_A, query='" OR * NEAR(alpha)')
        assert result.query.terms == ("or", "near", "alpha")
        assert result.hits == ()


def test_database_file_remains_owner_only_after_search(tmp_path: Path) -> None:
    directory = secure_directory(tmp_path)
    with SQLiteLedger.open(directory) as ledger:
        ledger.search_notes(tenant_id=TENANT_A, query="alpha")
    assert (directory / DATABASE_FILENAME).stat().st_mode & 0o777 == 0o600
