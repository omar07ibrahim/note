from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import lexical_eval_contract as contract  # noqa: E402
from tools import render_lexical_eval_visuals as renderer  # noqa: E402

SVG = "{http://www.w3.org/2000/svg}"
OUTPUT_DIRECTORY = ROOT / "docs" / "visuals"
EXPECTED_DIMENSIONS = {
    "lexical-search-eval-summary.svg": ("2000", "900"),
    "lexical-search-query-matrix.svg": ("2400", "1320"),
    "lexical-search-ranking-breakdown.svg": ("1800", "850"),
}
EXPECTED_HASHES = {
    "data-corpus-sha256": "a41bbb16bf618f0dbfaf9f62f7c85696db6d0e7ff6113491c4bcfe71cb7658e7",
    "data-queries-sha256": "d0fbf57ea8396eaa97a9093a9ca24a473564f3f3c3d8aa87dce70f6002aba7dd",
    "data-expected-sha256": "ae5a3c4815ef9988e72de76e51408790dd9ac980bacc3a8ce00db58102a288c9",
}


def _root(payload: bytes) -> ElementTree.Element:
    return ElementTree.fromstring(payload)  # noqa: S314 - trusted renderer output


def _visible_text(root: ElementTree.Element) -> str:
    return " ".join(text.strip() for text in root.itertext() if text.strip())


def _elements_with_class(root: ElementTree.Element, value: str) -> list[ElementTree.Element]:
    return [element for element in root.iter() if element.get("class") == value]


def _cell(
    root: ElementTree.Element,
    *,
    query_id: str,
    document_id: str,
) -> ElementTree.Element:
    matches = [
        element
        for element in _elements_with_class(root, "matrix-cell")
        if element.get("data-query-id") == query_id
        and element.get("data-document-id") == document_id
    ]
    assert len(matches) == 1
    return matches[0]


def test_committed_eval_visuals_are_exactly_current() -> None:
    rendered = renderer.render_all(renderer.load_snapshot())

    assert tuple(rendered) == renderer.OUTPUT_NAMES
    for filename in renderer.OUTPUT_NAMES:
        assert (OUTPUT_DIRECTORY / filename).read_bytes() == rendered[filename]


def test_every_svg_is_accessible_source_bound_and_self_contained() -> None:
    rendered = renderer.render_all(renderer.load_snapshot())

    for filename, payload in rendered.items():
        root = _root(payload)
        assert root.tag == f"{SVG}svg"
        assert (root.get("width"), root.get("height")) == EXPECTED_DIMENSIONS[filename]
        assert root.get("viewBox") == f"0 0 {' '.join(EXPECTED_DIMENSIONS[filename])}"
        assert root.get("role") == "img"
        assert root.get("data-suite-id") == contract.SUITE_ID
        assert root.get("data-unicode-profile-policy") == "runtime-bound"
        assert {key: root.get(key) for key in EXPECTED_HASHES} == EXPECTED_HASHES

        labelled = root.get("aria-labelledby")
        assert labelled is not None
        label_ids = labelled.split()
        assert len(label_ids) == 2
        ids = {element.get("id") for element in root.iter()}
        assert set(label_ids) <= ids
        assert root.find(f"{SVG}title") is not None
        assert root.find(f"{SVG}desc") is not None

        visible = _visible_text(root)
        assert "corpus.v1.json" in visible
        assert "queries.v1.json" in visible
        assert "expected.v1.json" in visible
        assert "tools/render_lexical_eval_visuals.py" in visible
        assert "lexical contract v1 · runtime-bound Unicode profile" in visible
        assert all(digest in visible for digest in EXPECTED_HASHES.values())

        serialized = payload.decode("utf-8")
        assert serialized.endswith("\n")
        assert "<script" not in serialized
        assert "<image" not in serialized
        assert "<use" not in serialized
        assert "url(" not in serialized
        assert "href=" not in serialized
        assert "linearGradient" not in serialized
        assert "radialGradient" not in serialized
        assert re.findall(r"https?://[^\"']+", serialized) == ["http://www.w3.org/2000/svg"]


def test_palette_meets_text_and_non_text_contrast_contracts() -> None:
    renderer._validate_palette()

    text_pairs = (
        (renderer.TEXT, renderer.BACKGROUND),
        (renderer.TEXT, renderer.CARD),
        (renderer.SECONDARY, renderer.BACKGROUND),
        (renderer.MUTED, renderer.BACKGROUND),
        (renderer.MISS, renderer.MISS_BACKGROUND),
        ("#ffffff", renderer.GOLD),
        *((renderer.TEXT, fill) for fill, _stroke in renderer.RANK_COLORS.values()),
        *(("#ffffff", color) for color in renderer.BREAKDOWN_COLORS.values()),
    )
    assert all(
        renderer.contrast_ratio(foreground, background) >= renderer.MIN_TEXT_CONTRAST
        for foreground, background in text_pairs
    )

    non_text_pairs = (
        (renderer.BORDER, renderer.CARD),
        (renderer.BORDER, renderer.BACKGROUND),
        (renderer.MISS, renderer.MISS_BACKGROUND),
        *((stroke, fill) for fill, stroke in renderer.RANK_COLORS.values()),
        *((renderer.GOLD, fill) for fill, _stroke in renderer.RANK_COLORS.values()),
    )
    assert all(
        renderer.contrast_ratio(foreground, background) >= renderer.MIN_NON_TEXT_CONTRAST
        for foreground, background in non_text_pairs
    )
    assert renderer.contrast_ratio(renderer.BORDER, renderer.CARD) >= 3.0


def test_summary_uses_equal_cards_exact_denominators_and_honest_scope() -> None:
    root = _root(renderer.render_summary(renderer.load_snapshot()))
    cards = _elements_with_class(root, "kpi")
    rectangles = _elements_with_class(root, "kpi-card")

    assert len(cards) == 7
    assert len(rectangles) == 7
    assert {(rectangle.get("width"), rectangle.get("height")) for rectangle in rectangles} == {
        ("450", "175")
    }
    exact = {card.get("data-metric"): card.attrib for card in cards}
    assert {
        "data-numerator": "12",
        "data-denominator": "12",
        "data-ppm": "1000000",
    }.items() <= exact["exact_outcome_rate"].items()
    assert {
        "data-numerator": "11",
        "data-denominator": "12",
        "data-ppm": "916667",
    }.items() <= exact["success_at_1"].items()
    assert {
        "data-numerator": "11",
        "data-denominator": "12",
        "data-ppm": "916667",
    }.items() <= exact["macro_recall_at_5"].items()
    assert {
        "data-numerator": "16",
        "data-denominator": "17",
        "data-ppm": "941176",
    }.items() <= exact["micro_recall_at_5"].items()
    assert {
        "data-numerator": "11",
        "data-denominator": "12",
        "data-ppm": "916667",
    }.items() <= exact["mrr_at_5"].items()
    assert {
        "data-numerator": "11",
        "data-denominator": "12",
        "data-ppm": "916667",
    }.items() <= exact["macro_ndcg_at_5"].items()
    assert exact["unexpected_hit_count"]["data-count"] == "0"

    visible = _visible_text(root)
    assert "12/12" in visible and "100.0%" in visible
    assert "16/17" in visible and "94.1%" in visible
    assert "“remove note” returned 0 lexical hits" in visible
    assert "tombstone-boundary is judged g3 relevant" in visible
    assert "makes no semantic-retrieval claim" in visible
    assert "are not independent evidence" in visible
    assert "no cross-metric bars" in visible


def test_matrix_contains_every_judgment_and_uses_only_within_query_rank_fill() -> None:
    root = _root(renderer.render_matrix(renderer.load_snapshot()))
    cells = _elements_with_class(root, "matrix-cell")

    assert len(cells) == 96
    assert len({(cell.get("data-query-id"), cell.get("data-document-id")) for cell in cells}) == 96
    ranks = [int(cell.attrib["data-rank"]) for cell in cells]
    assert {rank: ranks.count(rank) for rank in range(1, 5)} == {1: 11, 2: 3, 3: 1, 4: 1}
    assert ranks.count(0) == 80

    for cell in cells:
        rank = int(cell.attrib["data-rank"])
        if rank:
            rectangle = cell.find(f"{SVG}rect")
            assert rectangle is not None
            assert rectangle.get("fill") == renderer.RANK_COLORS[rank][0]

    miss = _cell(
        root,
        query_id="q11-known-synonym-miss",
        document_id="tombstone-boundary",
    )
    assert {"data-grade": "3", "data-rank": "0", "data-score": "0"}.items() <= (miss.attrib.items())
    assert "\N{MULTIPLICATION SIGN} miss" in _visible_text(miss)
    assert "g3" in _visible_text(miss)
    miss_rectangle = miss.find(f"{SVG}rect")
    assert miss_rectangle is not None
    assert miss_rectangle.get("fill") == renderer.MISS_BACKGROUND
    assert miss_rectangle.get("stroke") == renderer.MISS

    operator = _cell(
        root,
        query_id="q12-literal-operators",
        document_id="literal-operators",
    )
    assert {"data-grade": "3", "data-rank": "1", "data-score": "9"}.items() <= (
        operator.attrib.items()
    )
    visible = _visible_text(root)
    assert "OR NEAR wildcard* → terms [or, near, wildcard]" in visible
    assert "[u6f72, u6e656172, u77696c6463617264]" in visible
    assert "“u6f72” AND “u6e656172” AND “u77696c6463617264”" in visible
    assert "raw scores are not comparable across queries" in visible


def test_dense_matrix_exposes_a_complete_aria_table_equivalent() -> None:
    root = _root(renderer.render_matrix(renderer.load_snapshot()))
    tables = _elements_with_class(root, "matrix-table")
    rows = _elements_with_class(root, "matrix-row")
    cells = _elements_with_class(root, "matrix-cell")

    assert len(tables) == 1
    table = tables[0]
    assert table.get("role") == "table"
    assert table.get("aria-rowcount") == "12"
    assert table.get("aria-colcount") == "8"
    assert table.get("aria-label") == "Frozen lexical query by document result table"
    assert len(rows) == 12
    assert [row.get("aria-rowindex") for row in rows] == [str(index) for index in range(1, 13)]
    assert all(row.get("role") == "row" and row.get("aria-label") for row in rows)
    assert len(cells) == 96
    for cell in cells:
        label = cell.get("aria-label")
        assert cell.get("role") == "cell"
        assert cell.get("aria-colindex") in {str(index) for index in range(1, 9)}
        assert label is not None
        assert cell.attrib["data-query-id"] in label
        assert cell.attrib["data-document-id"] in label
        assert f"relevance grade {cell.attrib['data-grade']}" in label
        if cell.attrib["data-rank"] == "0":
            assert "not retrieved" in label
        else:
            assert f"rank {cell.attrib['data-rank']}, score {cell.attrib['data-score']}" in label
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert description.text is not None
    assert "ARIA cell labels and data attributes" in description.text


def test_q01_breakdown_starts_at_zero_and_exposes_exact_score_components() -> None:
    root = _root(renderer.render_breakdown(renderer.load_snapshot()))
    rows = _elements_with_class(root, "score-row")

    observed = {
        row.attrib["data-document-id"]: (
            int(row.attrib["data-rank"]),
            int(row.attrib["data-grade"]),
            int(row.attrib["data-total"]),
            int(row.attrib["data-title"]),
            int(row.attrib["data-tag"]),
            int(row.attrib["data-body"]),
        )
        for row in rows
    }
    assert observed == {
        "retrieval-safety": (1, 3, 20, 1, 1, 0),
        "lexical-ranking": (2, 3, 12, 1, 0, 0),
        "unicode-profile": (3, 2, 8, 0, 1, 0),
        "tombstone-boundary": (4, 1, 3, 0, 0, 1),
    }
    segments = _elements_with_class(root, "score-segment")
    assert [(segment.get("data-component"), segment.get("data-value")) for segment in segments] == [
        ("title", "12"),
        ("tag", "8"),
        ("title", "12"),
        ("tag", "8"),
        ("body", "3"),
    ]
    assert {int(text) for text in _visible_text(root).split() if text.isdecimal()} >= {
        0,
        4,
        8,
        12,
        16,
        20,
    }
    visible = _visible_text(root)
    assert "one shared zero baseline" in visible
    assert "phrase bonuses are 0 for all four hits" in visible
    assert "12 x title term frequency + 8 x tag term frequency + 3 x body term frequency" in visible


def test_snapshot_calls_canonical_validator_once_and_all_reads_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_calls = 0
    original_validate = contract.validate_suite
    original_read = os.read
    read_sizes: list[int] = []

    def counted_validate(
        directory: Path = contract.DEFAULT_SUITE_DIRECTORY,
    ) -> contract.EvaluationSummary:
        nonlocal validation_calls
        validation_calls += 1
        return original_validate(directory)

    def guarded_read(descriptor: int, size: int) -> bytes:
        assert 0 <= size <= renderer.READ_CHUNK_BYTES
        read_sizes.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(contract, "validate_suite", counted_validate)
    monkeypatch.setattr(os, "read", guarded_read)

    snapshot = renderer.load_snapshot()

    assert snapshot.summary.query_count == 12
    assert validation_calls == 1
    assert read_sizes
    assert max(read_sizes) <= renderer.READ_CHUNK_BYTES


@pytest.mark.parametrize("link_component", ["ancestor", "final"])
def test_suite_directory_walk_rejects_alias_symlinks(
    tmp_path: Path,
    link_component: str,
) -> None:
    real_parent = tmp_path / "real"
    real_suite = real_parent / "lexical-v1"
    real_parent.mkdir()
    shutil.copytree(renderer.SUITE_DIRECTORY, real_suite)
    if link_component == "ancestor":
        alias_parent = tmp_path / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        candidate = alias_parent / "lexical-v1"
    else:
        candidate = tmp_path / "alias-suite"
        candidate.symlink_to(real_suite, target_is_directory=True)

    with pytest.raises(renderer.LexicalEvalRenderError, match="suite directory"):
        renderer.load_snapshot(candidate)


def test_runtime_bound_unicode_version_does_not_make_svg_bytes_runtime_specific() -> None:
    snapshot = renderer.load_snapshot()
    another_runtime = renderer.EvalSnapshot(
        summary=replace(snapshot.summary, unicode_profile="other-validated-runtime-profile"),
        documents=snapshot.documents,
        queries=snapshot.queries,
        outcomes=snapshot.outcomes,
    )

    assert renderer.render_all(another_runtime) == renderer.render_all(snapshot)


def test_write_check_and_stdout_modes_are_deterministic_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    expected = renderer.render_all(renderer.load_snapshot())

    assert renderer.main(["--write"]) == 0
    assert {path.name for path in tmp_path.iterdir()} == set(renderer.OUTPUT_NAMES)
    assert renderer.main(["--check"]) == 0

    (tmp_path / renderer.OUTPUT_NAMES[0]).write_bytes(b"stale\n")
    assert renderer.main(["--check"]) == 1
    captured = capsys.readouterr()
    assert "is not byte-for-byte current" in captured.err

    assert renderer.main(["--stdout", "matrix"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.encode("utf-8") == expected[renderer.STDOUT_CHOICES["matrix"]]


def test_check_rejects_a_final_path_swap_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    assert renderer.main(["--write"]) == 0
    filename = renderer.OUTPUT_NAMES[0]
    target = tmp_path / filename
    original_identity = (target.stat().st_dev, target.stat().st_ino)
    original_payload = target.read_bytes()
    parked = tmp_path / "parked.svg"
    real_read = os.read
    swapped = False

    def swap_after_open(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        chunk = real_read(descriptor, size)
        metadata = os.fstat(descriptor)
        if chunk and not swapped and (metadata.st_dev, metadata.st_ino) == original_identity:
            swapped = True
            target.rename(parked)
            target.write_bytes(b"x" * len(original_payload))
        return chunk

    monkeypatch.setattr(os, "read", swap_after_open)

    assert renderer.main(["--check"]) == 1
    assert swapped
    assert parked.read_bytes() == original_payload
    assert target.read_bytes() != original_payload
    assert "changed while it was read" in capsys.readouterr().err


def test_check_rejects_same_inode_mutation_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    assert renderer.main(["--write"]) == 0
    target = tmp_path / renderer.OUTPUT_NAMES[0]
    original_metadata = target.stat()
    original_payload = target.read_bytes()
    target_identity = (original_metadata.st_dev, original_metadata.st_ino)
    real_read = os.read
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        metadata = os.fstat(descriptor)
        if chunk and not mutated and (metadata.st_dev, metadata.st_ino) == target_identity:
            mutated = True
            target.write_bytes(b"x" * len(original_payload))
            os.utime(
                target,
                ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
            )
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_read)

    assert renderer.main(["--check"]) == 1
    assert mutated
    assert target.stat().st_ino == original_metadata.st_ino
    assert target.stat().st_mtime_ns == original_metadata.st_mtime_ns
    assert target.read_bytes() != original_payload
    assert "changed while it was read" in capsys.readouterr().err


def test_atomic_write_handles_short_writes_and_leaves_no_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = renderer.OUTPUT_NAMES[0]
    target = tmp_path / filename
    target.write_bytes(b"old")
    payload = b"<svg>" + b"x" * 128 + b"</svg>"
    real_write = os.write
    calls = 0
    write_modes: list[int] = []

    def short_write(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        write_modes.append(os.fstat(descriptor).st_mode & 0o777)
        return real_write(descriptor, data[:7])

    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    monkeypatch.setattr(os, "write", short_write)
    renderer._atomic_write(filename, payload)

    assert calls > 1
    assert write_modes and set(write_modes) == {0o600}
    assert target.read_bytes() == payload
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(f".{filename}.tmp-")]


def test_atomic_write_rejects_temporary_name_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = renderer.OUTPUT_NAMES[0]
    target = tmp_path / filename
    target.write_bytes(b"old")
    real_stat = os.stat
    substituted = False

    def substitute_temporary(
        path: str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal substituted
        if (
            isinstance(path, str)
            and path.startswith(f".{filename}.tmp-")
            and dir_fd is not None
            and not substituted
        ):
            substituted = True
            os.unlink(path, dir_fd=dir_fd)
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.write(descriptor, b"bad")
            finally:
                os.close(descriptor)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    monkeypatch.setattr(os, "stat", substitute_temporary)
    with pytest.raises(renderer.LexicalEvalRenderError, match="temporary changed"):
        renderer._atomic_write(filename, b"new")

    assert substituted
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_rejects_destination_swap_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = renderer.OUTPUT_NAMES[0]
    target = tmp_path / filename
    target.write_bytes(b"old")
    real_stat = os.stat
    target_stats = 0

    def swap_destination(
        path: str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal target_stats
        if path == filename and dir_fd is not None:
            target_stats += 1
            if target_stats == 2:
                os.unlink(path, dir_fd=dir_fd)
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=dir_fd,
                )
                try:
                    os.write(descriptor, b"bad")
                finally:
                    os.close(descriptor)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    monkeypatch.setattr(os, "stat", swap_destination)
    with pytest.raises(renderer.LexicalEvalRenderError, match="target changed"):
        renderer._atomic_write(filename, b"new")

    assert target_stats == 2
    assert target.read_bytes() == b"bad"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_verifies_the_final_installed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = renderer.OUTPUT_NAMES[0]
    target = tmp_path / filename
    target.write_bytes(b"old")
    real_replace = os.replace

    def replace_then_swap(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd is not None
        assert src_dir_fd == dst_dir_fd
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        os.unlink(destination, dir_fd=dst_dir_fd)
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(descriptor, b"bad")
        finally:
            os.close(descriptor)

    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    monkeypatch.setattr(os, "replace", replace_then_swap)
    with pytest.raises(renderer.LexicalEvalRenderError, match="atomic replacement"):
        renderer._atomic_write(filename, b"new")

    assert target.read_bytes() == b"bad"
    assert list(tmp_path.iterdir()) == [target]


def test_failed_atomic_replace_cleans_the_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = renderer.OUTPUT_NAMES[0]
    target = tmp_path / filename
    target.write_bytes(b"old")

    def reject_replace(
        _source: str,
        _destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd == dst_dir_fd
        raise OSError

    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)
    monkeypatch.setattr(os, "replace", reject_replace)
    with pytest.raises(renderer.LexicalEvalRenderError, match="atomically"):
        renderer._atomic_write(filename, b"new")

    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_write_refuses_to_replace_a_symlinked_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "outside.svg"
    target.write_bytes(b"preserve\n")
    output = tmp_path / renderer.OUTPUT_NAMES[0]
    output.symlink_to(target)
    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", tmp_path)

    assert renderer.main(["--write"]) == 1
    assert target.read_bytes() == b"preserve\n"
    assert output.is_symlink()
    assert "output target is unavailable or unsafe" in capsys.readouterr().err


def test_write_refuses_a_symlinked_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    alias_directory = tmp_path / "alias"
    alias_directory.symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setattr(renderer, "OUTPUT_DIRECTORY", alias_directory)

    assert renderer.main(["--write"]) == 1
    assert list(real_directory.iterdir()) == []
    assert "output directory is unavailable or unsafe" in capsys.readouterr().err
