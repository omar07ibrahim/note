from __future__ import annotations

import io
import json
import math
import os
import re
import stat
import struct
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import render_visuals  # noqa: E402 - repository-local tool is not installed

JsonObject = dict[str, object]


class GuardedBytesIO(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.requests: list[int] = []

    def read(self, size: int | None = -1, /) -> bytes:
        assert type(size) is int
        assert 0 <= size <= render_visuals.READ_CHUNK_BYTES
        self.requests.append(size)
        return super().read(size)


def minimal_source() -> JsonObject:
    return {
        "schema_version": 1,
        "diagram_kind": "architecture",
        "slug": "fixture",
        "entry": "entry",
        "title": "Renderer fixture",
        "subtitle": "A bounded source for hostile renderer tests",
        "description": "A local deterministic renderer validation fixture.",
        "caption": "Test-only source",
        "canvas": {"width": 1000, "height": 700},
        "bindings": [
            {
                "id": "implementation",
                "kind": "python",
                "path": "src/recall_ledger/cli.py",
                "target": "run_cli",
                "match": "defined",
                "expected": "function",
                "claim": "The fixture binds every rendered element.",
            }
        ],
        "lanes": [
            {
                "id": "flow-lane",
                "binding": "implementation",
                "label": "FLOW",
                "x": 40,
                "y": 150,
                "width": 920,
                "height": 400,
            }
        ],
        "nodes": [
            {
                "id": "entry",
                "binding": "implementation",
                "lane": "flow-lane",
                "label": "Entry",
                "detail": ["start"],
                "x": 80,
                "y": 250,
                "width": 180,
                "height": 120,
                "variant": "primary",
            },
            {
                "id": "middle",
                "binding": "implementation",
                "lane": "flow-lane",
                "label": "Middle",
                "detail": ["settle"],
                "x": 410,
                "y": 250,
                "width": 180,
                "height": 120,
                "variant": "neutral",
            },
            {
                "id": "result",
                "binding": "implementation",
                "lane": "flow-lane",
                "label": "Result",
                "detail": ["finish"],
                "x": 740,
                "y": 250,
                "width": 180,
                "height": 120,
                "variant": "output",
            },
        ],
        "edges": [
            {
                "id": "entry-to-middle",
                "binding": "implementation",
                "from": "entry",
                "to": "middle",
                "label": "dispatch",
                "label_x": 335,
                "label_y": 300,
                "style": "solid",
                "points": [[260, 310], [410, 310]],
            },
            {
                "id": "middle-to-result",
                "binding": "implementation",
                "from": "middle",
                "to": "result",
                "label": "return",
                "label_x": 665,
                "label_y": 300,
                "style": "solid",
                "points": [[590, 310], [740, 310]],
            },
        ],
        "notes": [],
    }


def object_array(value: object) -> list[JsonObject]:
    assert type(value) is list
    return cast(list[JsonObject], value)


def write_source(directory: Path, source: JsonObject) -> Path:
    path = directory / "fixture.v1.json"
    path.write_text(
        json.dumps(source, ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )
    return path


def load_fixture(tmp_path: Path) -> render_visuals.Diagram:
    return render_visuals.load_diagram(write_source(tmp_path, minimal_source()))


def make_edge(
    identifier: str,
    *,
    source: str,
    target: str,
    points: tuple[tuple[int, int], ...],
    label: str = "route",
) -> render_visuals.Edge:
    return render_visuals.Edge(
        identifier=identifier,
        binding="implementation",
        source=source,
        target=target,
        label=label,
        label_x=0,
        label_y=0,
        style="solid",
        points=points,
    )


def test_minimal_fixture_is_valid_and_fully_reachable(tmp_path: Path) -> None:
    diagram = load_fixture(tmp_path)
    assert diagram.entry == "entry"
    assert {node.identifier for node in diagram.nodes} == {
        "entry",
        "middle",
        "result",
    }


@pytest.mark.parametrize("entry", ["missing", "middle", "result"])
def test_entry_mutations_fail_closed_when_not_resolved_or_not_a_graph_root(
    tmp_path: Path,
    entry: str,
) -> None:
    source = minimal_source()
    source["entry"] = entry
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_removing_a_reachability_edge_is_rejected(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["edges"]).pop()
    with pytest.raises(render_visuals.VisualSourceError, match="unreachable"):
        render_visuals.load_diagram(write_source(tmp_path, source))


@pytest.mark.parametrize("collision", ["entry", "arrow", "fixture-title", "fixture-description"])
def test_svg_ids_are_global_and_reserved_ids_cannot_be_shadowed(
    tmp_path: Path,
    collision: str,
) -> None:
    source = minimal_source()
    object_array(source["edges"])[0]["id"] = collision
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_note_and_node_ids_share_one_svg_namespace(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["notes"]).append(
        {
            "id": "entry",
            "binding": "implementation",
            "label": "Note",
            "text": ["duplicate ID"],
            "x": 80,
            "y": 580,
            "width": 200,
            "variant": "neutral",
        }
    )
    with pytest.raises(render_visuals.VisualSourceError, match="rendered SVG elements"):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_lane_palette_meets_text_and_non_text_contrast_contract(tmp_path: Path) -> None:
    assert (
        render_visuals.contrast_ratio(
            render_visuals.LANE_BORDER,
            render_visuals.LANE_BACKGROUND,
        )
        >= render_visuals.MIN_NON_TEXT_CONTRAST
    )
    assert (
        render_visuals.contrast_ratio(
            render_visuals.LANE_BORDER,
            render_visuals.CANVAS_BACKGROUND,
        )
        >= render_visuals.MIN_NON_TEXT_CONTRAST
    )
    assert (
        render_visuals.contrast_ratio(
            render_visuals.SECONDARY_TEXT,
            render_visuals.LANE_BACKGROUND,
        )
        >= render_visuals.MIN_TEXT_CONTRAST
    )
    assert (
        render_visuals.contrast_ratio(
            render_visuals.MUTED_TEXT,
            render_visuals.CANVAS_BACKGROUND,
        )
        >= render_visuals.MIN_TEXT_CONTRAST
    )
    svg = render_visuals.render_diagram(load_fixture(tmp_path)).decode("utf-8")
    assert (f"fill: {render_visuals.LANE_BACKGROUND}; stroke: {render_visuals.LANE_BORDER};") in svg


@pytest.mark.parametrize(
    "second_points",
    [
        ((10, 0), (10, 20)),
        ((5, 10), (15, 10)),
        ((10, 10), (10, 30)),
        ((20, 10), (20, 30)),
    ],
)
def test_distinct_routes_reject_crossings_overlaps_and_ambiguous_touches(
    second_points: tuple[tuple[int, int], tuple[int, int]],
) -> None:
    first = make_edge(
        "first",
        source="a",
        target="shared",
        points=((0, 10), (20, 10)),
    )
    second = make_edge(
        "second",
        source="different",
        target="b",
        points=second_points,
    )
    with pytest.raises(render_visuals.VisualSourceError, match="cross, touch, or overlap"):
        render_visuals._validate_distinct_edge_routes((first, second))


def test_distinct_routes_allow_only_a_shared_semantic_endpoint() -> None:
    first = make_edge(
        "first",
        source="a",
        target="shared",
        points=((0, 0), (10, 0)),
    )
    second = make_edge(
        "second",
        source="shared",
        target="b",
        points=((10, 0), (10, 10)),
    )
    render_visuals._validate_distinct_edge_routes((first, second))


def test_one_route_cannot_reverse_over_its_previous_segment() -> None:
    edge = make_edge(
        "self-overlap",
        source="a",
        target="b",
        points=((0, 0), (20, 0), (10, 0)),
    )
    segments = tuple(zip(edge.points, edge.points[1:], strict=False))
    with pytest.raises(render_visuals.VisualSourceError, match="itself"):
        render_visuals._validate_route_does_not_cross_itself(edge, segments)


def test_edge_start_must_be_on_the_source_boundary(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["edges"])[0]["points"] = [[250, 310], [410, 310]]
    with pytest.raises(render_visuals.VisualSourceError, match="source boundary"):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_edge_end_must_be_on_the_target_boundary(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["edges"])[0]["points"] = [[260, 310], [400, 310]]
    with pytest.raises(render_visuals.VisualSourceError, match="target boundary"):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_edge_must_depart_outward_from_the_source(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["edges"])[0]["points"] = [
        [260, 310],
        [200, 310],
        [200, 200],
        [410, 200],
        [410, 250],
    ]
    with pytest.raises(render_visuals.VisualSourceError, match="depart outward"):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_edge_final_segment_must_point_into_the_target(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["edges"])[0]["points"] = [
        [260, 310],
        [500, 310],
        [410, 310],
    ]
    with pytest.raises(render_visuals.VisualSourceError, match="point into"):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_edge_cannot_reenter_its_source_interior(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["edges"])[0]["points"] = [
        [260, 310],
        [300, 310],
        [300, 270],
        [200, 270],
        [200, 200],
        [410, 200],
        [410, 250],
    ]
    with pytest.raises(render_visuals.VisualSourceError, match="crosses entry"):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_route_cannot_cross_lane_label_content(tmp_path: Path) -> None:
    diagram = load_fixture(tmp_path)
    lane = replace(diagram.lanes[0], label="MMMMMMMMMMMMMMMM")
    first = replace(
        diagram.edges[0],
        label_x=335,
        label_y=210,
        points=((170, 250), (170, 180), (500, 180), (500, 250)),
    )
    hostile = replace(
        diagram,
        lanes=(lane,),
        edges=(first, diagram.edges[1]),
    )
    with pytest.raises(render_visuals.VisualSourceError, match="crosses lane label"):
        render_visuals._validate_geometry(hostile)


def test_edge_label_cannot_overlap_lane_label_content(tmp_path: Path) -> None:
    diagram = load_fixture(tmp_path)
    lane = replace(diagram.lanes[0], label="MMMMMMMMMMMMMMMM")
    first = replace(diagram.edges[0], label_x=200, label_y=180)
    hostile = replace(
        diagram,
        lanes=(lane,),
        edges=(first, diagram.edges[1]),
    )
    with pytest.raises(render_visuals.VisualSourceError, match="overlaps lane label"):
        render_visuals._validate_geometry(hostile)


def test_route_cannot_cross_another_edges_label(tmp_path: Path) -> None:
    diagram = load_fixture(tmp_path)
    second = replace(diagram.edges[1], label_x=335, label_y=310)
    hostile = replace(diagram, edges=(diagram.edges[0], second))
    boxes = [
        *(
            (f"lane label {lane.identifier}", render_visuals._lane_label_rectangle(lane))
            for lane in hostile.lanes
        ),
        *((node.identifier, render_visuals._node_rectangle(node)) for node in hostile.nodes),
    ]
    with pytest.raises(render_visuals.VisualSourceError, match="crosses edge label"):
        render_visuals._validate_edge_routes(hostile, boxes)


def test_lane_cannot_overlap_diagram_header_or_footer(tmp_path: Path) -> None:
    source = minimal_source()
    object_array(source["lanes"])[0]["y"] = 0
    with pytest.raises(render_visuals.VisualSourceError, match="header or footer"):
        render_visuals.load_diagram(write_source(tmp_path, source))


def test_lanes_cannot_overlap_each_other(tmp_path: Path) -> None:
    diagram = load_fixture(tmp_path)
    overlap = replace(
        diagram.lanes[0],
        identifier="overlap",
        label="SECOND FLOW",
        x=50,
        width=900,
    )
    hostile = replace(diagram, lanes=(*diagram.lanes, overlap))
    with pytest.raises(
        render_visuals.VisualSourceError, match="lane flow-lane overlaps lane overlap"
    ):
        render_visuals._validate_geometry(hostile)


def test_route_cannot_run_along_unrelated_content_boundary(tmp_path: Path) -> None:
    diagram = load_fixture(tmp_path)
    boundary = render_visuals.Rectangle(x=300, y=310, width=100, height=100)
    with pytest.raises(render_visuals.VisualSourceError, match="crosses unrelated"):
        render_visuals._validate_edge_routes(diagram, [("unrelated", boundary)])


def test_source_read_uses_bounded_prefix_chunks() -> None:
    maximum = render_visuals.READ_CHUNK_BYTES * 2
    stream = GuardedBytesIO(b"x" * (maximum + 1))

    with pytest.raises(render_visuals.VisualSourceError, match="byte size"):
        render_visuals._read_bounded_stream(
            stream,
            maximum=maximum,
            context="test source",
        )
    assert stream.requests
    assert max(stream.requests) <= render_visuals.READ_CHUNK_BYTES
    assert sum(stream.requests) == maximum + 1


def test_output_check_reads_only_expected_prefix_plus_one_byte() -> None:
    expected = b"<svg/>"
    stream = GuardedBytesIO(expected + b"x" * 100_000)

    assert not render_visuals._bounded_stream_matches(stream, expected)
    assert stream.requests == [len(expected), 1]


def test_direct_reads_reject_symlinks_fifos_and_symlink_ancestors(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"bounded")
    symlink = tmp_path / "source.v1.json"
    symlink.symlink_to(payload)
    fifo = tmp_path / "fifo.v1.json"
    os.mkfifo(fifo)

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    (real_directory / "source.v1.json").write_bytes(b"bounded")
    alias_directory = tmp_path / "alias"
    alias_directory.symlink_to(real_directory, target_is_directory=True)

    for path in (symlink, fifo, alias_directory / "source.v1.json"):
        with pytest.raises(render_visuals.VisualSourceError):
            render_visuals._read_bounded_file(path, maximum=64, context="hostile source")


def test_source_directory_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    write_source(real_directory, minimal_source())
    alias_directory = tmp_path / "alias"
    alias_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(render_visuals.VisualSourceError, match="directory"):
        render_visuals.render_sources(alias_directory)


def test_source_and_output_directory_fifos_are_rejected_without_blocking(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "directory"
    os.mkfifo(fifo)
    outputs = {"fixture": b"<svg/>"}
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals.render_sources(fifo)
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals._write_outputs(outputs, fifo)
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals._check_outputs(outputs, fifo)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_source_discovery_rejects_non_regular_canonical_entries(
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / "fixture.v1.json"
    if kind == "symlink":
        payload = tmp_path / "payload"
        payload.write_bytes(b"{}")
        source.symlink_to(payload)
    else:
        os.mkfifo(source)
    with pytest.raises(render_visuals.VisualSourceError, match="regular file"):
        render_visuals.render_sources(tmp_path)


@pytest.mark.parametrize("name", ["unversioned.json", "uppercase.JSON"])
def test_source_discovery_rejects_noncanonical_extra_json(
    tmp_path: Path,
    name: str,
) -> None:
    write_source(tmp_path, minimal_source())
    (tmp_path / name).write_text("{}", encoding="utf-8")
    with pytest.raises(render_visuals.VisualSourceError, match="noncanonical JSON"):
        render_visuals.render_sources(tmp_path)


@pytest.mark.parametrize("link_component", ["ancestor", "final"])
def test_binding_paths_never_follow_links_outside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_component: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if link_component == "ancestor":
        (outside / "recall_ledger").mkdir()
        (outside / "recall_ledger" / "cli.py").write_text("pass\n", encoding="utf-8")
        (repository / "src").symlink_to(outside, target_is_directory=True)
    else:
        (repository / "src" / "recall_ledger").mkdir(parents=True)
        payload = outside / "cli.py"
        payload.write_text("pass\n", encoding="utf-8")
        (repository / "src" / "recall_ledger" / "cli.py").symlink_to(payload)
    source_directory = tmp_path / "sources"
    source_directory.mkdir()
    source_path = write_source(source_directory, minimal_source())
    monkeypatch.setattr(render_visuals, "ROOT", repository)
    with pytest.raises(render_visuals.VisualSourceError, match="binding path"):
        render_visuals.load_diagram(source_path)


def test_source_count_is_capped_before_any_source_is_loaded(tmp_path: Path) -> None:
    for index in range(render_visuals.MAX_SOURCE_FILES + 1):
        (tmp_path / f"source-{index:02}.v1.json").touch()
    with pytest.raises(render_visuals.VisualSourceError, match="source count"):
        render_visuals.render_sources(tmp_path)


def test_source_directory_caps_irrelevant_entries_while_streaming(tmp_path: Path) -> None:
    for index in range(render_visuals.MAX_DIRECTORY_ENTRIES + 1):
        (tmp_path / f"ignored-{index:03}.txt").touch()
    with pytest.raises(render_visuals.VisualSourceError, match="directory entry count"):
        render_visuals.render_sources(tmp_path)


def test_source_read_stays_on_discovered_directory_inode(tmp_path: Path) -> None:
    source_directory = tmp_path / "sources"
    source_directory.mkdir()
    source_path = source_directory / "fixture.v1.json"
    source_path.write_bytes(b"original")
    directory_fd = render_visuals._open_directory_fd(
        source_directory,
        create=False,
        context="test source directory",
    )
    try:
        entries = render_visuals._bounded_source_entries(directory_fd)
        parked_directory = tmp_path / "parked"
        source_directory.rename(parked_directory)
        source_directory.mkdir()
        (source_directory / "fixture.v1.json").write_bytes(b"substitute")
        assert render_visuals._read_source_entry(directory_fd, entries[0]) == b"original"
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_atomic_write_never_mutates_unsafe_existing_targets(
    tmp_path: Path,
    kind: str,
) -> None:
    output_directory = tmp_path / kind
    output_directory.mkdir()
    target = output_directory / "fixture.svg"
    victim = output_directory / "victim"
    victim.write_bytes(b"victim")
    if kind == "symlink":
        target.symlink_to(victim)
    elif kind == "hardlink":
        os.link(victim, target)
    else:
        os.mkfifo(target)

    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals._write_outputs({"fixture": b"<svg/>"}, output_directory)
    assert victim.read_bytes() == b"victim"
    if kind == "symlink":
        assert target.is_symlink()
    elif kind == "hardlink":
        assert target.stat().st_ino == victim.stat().st_ino
    else:
        assert stat.S_ISFIFO(target.lstat().st_mode)


def test_output_directory_link_is_rejected_for_write_and_check(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    alias_directory = tmp_path / "alias"
    alias_directory.symlink_to(real_directory, target_is_directory=True)
    outputs = {"fixture": b"<svg/>"}

    with pytest.raises(render_visuals.VisualSourceError, match="output directory"):
        render_visuals._write_outputs(outputs, alias_directory)
    with pytest.raises(render_visuals.VisualSourceError, match="output directory"):
        render_visuals._check_outputs(outputs, alias_directory)
    assert not list(real_directory.iterdir())


def test_output_write_is_same_directory_atomic_and_handles_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()
    target = output_directory / "fixture.svg"
    target.write_bytes(b"old")
    payload = b"<svg>" + b"x" * 128 + b"</svg>"
    real_replace = os.replace
    real_write = os.write
    observations: list[tuple[int, int]] = []
    write_calls = 0

    def inspected_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd is not None
        assert src_dir_fd == dst_dir_fd
        metadata = os.stat(source, dir_fd=src_dir_fd, follow_symlinks=False)
        observations.append((metadata.st_mode, metadata.st_nlink))
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def short_write(file_fd: int, data: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        return real_write(file_fd, data[:7])

    monkeypatch.setattr(os, "replace", inspected_replace)
    monkeypatch.setattr(os, "write", short_write)
    render_visuals._write_outputs({"fixture": payload}, output_directory)

    assert target.read_bytes() == payload
    assert write_calls > 1
    assert len(observations) == 1
    mode, link_count = observations[0]
    assert stat.S_ISREG(mode)
    assert stat.S_IMODE(mode) == 0o644
    assert link_count == 1
    assert not list(output_directory.glob(".*.tmp-*"))


def test_failed_atomic_replace_cleans_same_directory_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "outputs"
    output_directory.mkdir()

    def fail_replace(
        _source: str,
        _destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd == dst_dir_fd
        raise OSError

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(render_visuals.VisualSourceError, match="written safely"):
        render_visuals._write_outputs({"fixture": b"<svg/>"}, output_directory)
    assert not list(output_directory.iterdir())


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_output_check_rejects_unsafe_expected_artifacts(
    tmp_path: Path,
    kind: str,
) -> None:
    output_directory = tmp_path / kind
    output_directory.mkdir()
    target = output_directory / "fixture.svg"
    victim = output_directory / "payload"
    victim.write_bytes(b"<svg/>")
    if kind == "symlink":
        target.symlink_to(victim)
    elif kind == "hardlink":
        os.link(victim, target)
    else:
        os.mkfifo(target)
    assert not render_visuals._check_outputs({"fixture": b"<svg/>"}, output_directory)


@pytest.mark.parametrize("orphan_name", ["orphan.svg", "orphan.SVG"])
def test_output_check_rejects_unexpected_orphan_svg(
    tmp_path: Path,
    orphan_name: str,
) -> None:
    (tmp_path / "fixture.svg").write_bytes(b"<svg/>")
    (tmp_path / orphan_name).write_bytes(b"<svg/>")
    assert not render_visuals._check_outputs({"fixture": b"<svg/>"}, tmp_path)


def test_output_check_caps_irrelevant_directory_entries(tmp_path: Path) -> None:
    (tmp_path / "fixture.svg").write_bytes(b"<svg/>")
    for index in range(render_visuals.MAX_DIRECTORY_ENTRIES):
        (tmp_path / f"ignored-{index:03}.txt").touch()
    assert not render_visuals._check_outputs({"fixture": b"<svg/>"}, tmp_path)


def test_output_check_detects_same_size_mutation_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "fixture.svg"
    target.write_bytes(b"<svg/>")
    original = target.stat()
    real_stat = os.stat
    mutated = False

    def mutate_before_current_stat(
        path: str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal mutated
        if path == "fixture.svg" and dir_fd is not None and not mutated:
            mutated = True
            target.write_bytes(b"<bad/>")
            os.utime(
                target,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", mutate_before_current_stat)
    assert not render_visuals._check_outputs({"fixture": b"<svg/>"}, tmp_path)
    assert mutated
    assert target.read_bytes() == b"<bad/>"


def test_aggregate_source_bytes_are_capped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_source(tmp_path, minimal_source())
    monkeypatch.setattr(
        render_visuals,
        "MAX_TOTAL_SOURCE_BYTES",
        path.stat().st_size - 1,
    )
    with pytest.raises(render_visuals.VisualSourceError, match="aggregate byte"):
        render_visuals.render_sources(tmp_path)


def test_rendered_output_has_per_artifact_and_aggregate_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagram = load_fixture(tmp_path)
    monkeypatch.setattr(render_visuals, "MAX_RENDERED_SVG_BYTES", 1)
    with pytest.raises(render_visuals.VisualSourceError, match="per-artifact"):
        render_visuals.render_diagram(diagram)

    monkeypatch.setattr(render_visuals, "MAX_RENDERED_SVG_BYTES", 524_288)
    monkeypatch.setattr(render_visuals, "MAX_TOTAL_RENDERED_BYTES", 1)
    with pytest.raises(render_visuals.VisualSourceError, match="aggregate byte"):
        render_visuals.render_sources(tmp_path)


def test_memory_error_is_reported_as_a_stable_cli_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exhaust_memory(_source_directory: Path) -> dict[str, bytes]:
        raise MemoryError

    error = io.StringIO()
    monkeypatch.setattr(render_visuals, "render_sources", exhaust_memory)
    monkeypatch.setattr(sys, "stderr", error)
    assert (
        render_visuals.main(
            [
                "--check",
                "--source-dir",
                str(tmp_path),
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 2
    )
    assert error.getvalue() == "visual rendering failed\n"


def test_direct_source_load_converts_memory_error_to_source_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exhaust_memory(_path: Path) -> tuple[render_visuals.Diagram, int]:
        raise MemoryError

    monkeypatch.setattr(render_visuals, "_load_diagram_with_size", exhaust_memory)
    with pytest.raises(render_visuals.VisualSourceError, match="memory budget"):
        render_visuals.load_diagram(Path("ignored.v1.json"))


def test_direct_render_converts_memory_error_to_source_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagram = load_fixture(tmp_path)

    def exhaust_memory(_diagram: render_visuals.Diagram) -> bytes:
        raise MemoryError

    monkeypatch.setattr(render_visuals, "_render_diagram_payload", exhaust_memory)
    with pytest.raises(render_visuals.VisualSourceError, match="memory budget"):
        render_visuals.render_diagram(diagram)


def _u16(payload: bytes, offset: int) -> int:
    return cast(int, struct.unpack_from(">H", payload, offset)[0])


def _u32(payload: bytes, offset: int) -> int:
    return cast(int, struct.unpack_from(">I", payload, offset)[0])


def _font_tables(payload: bytes) -> dict[str, int]:
    tables: dict[str, int] = {}
    table_count = _u16(payload, 4)
    for index in range(table_count):
        record = 12 + index * 16
        tag = payload[record : record + 4].decode("ascii")
        tables[tag] = _u32(payload, record + 8)
    return tables


def _format_four_glyph(payload: bytes, table: int, codepoint: int) -> int | None:
    segment_count = _u16(payload, table + 6) // 2
    end_codes = table + 14
    start_codes = end_codes + segment_count * 2 + 2
    deltas = start_codes + segment_count * 2
    range_offsets = deltas + segment_count * 2
    for index in range(segment_count):
        end = _u16(payload, end_codes + index * 2)
        start = _u16(payload, start_codes + index * 2)
        if not start <= codepoint <= end:
            continue
        delta = _u16(payload, deltas + index * 2)
        range_offset_location = range_offsets + index * 2
        range_offset = _u16(payload, range_offset_location)
        if range_offset == 0:
            return (codepoint + delta) & 0xFFFF
        glyph_location = range_offset_location + range_offset + (codepoint - start) * 2
        glyph = _u16(payload, glyph_location)
        return 0 if glyph == 0 else (glyph + delta) & 0xFFFF
    return None


def _glyph_id(payload: bytes, tables: dict[str, int], character: str) -> int:
    cmap = tables["cmap"]
    subtable_count = _u16(payload, cmap + 2)
    for index in range(subtable_count):
        record = cmap + 4 + index * 8
        subtable = cmap + _u32(payload, record + 4)
        if _u16(payload, subtable) != 4:
            continue
        glyph = _format_four_glyph(payload, subtable, ord(character))
        if glyph is not None:
            return glyph
    raise AssertionError


def _advance_width_px(payload: bytes, character: str, font_size: int) -> float:
    tables = _font_tables(payload)
    units_per_em = _u16(payload, tables["head"] + 18)
    long_metric_count = _u16(payload, tables["hhea"] + 34)
    glyph = _glyph_id(payload, tables, character)
    metric_index = min(glyph, long_metric_count - 1)
    advance_units = _u16(payload, tables["hmtx"] + metric_index * 4)
    return advance_units * font_size / units_per_em


def test_text_bound_covers_real_dejavu_sans_bold_m_and_edge_label_box() -> None:
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if not font_path.is_file():
        pytest.skip("DejaVu Sans Bold is unavailable on this platform")
    payload = font_path.read_bytes()
    label = "mmmmmmmmmmmmmmmm"
    actual_width = _advance_width_px(
        payload,
        "m",
        render_visuals.EDGE_LABEL_FONT_SIZE,
    ) * len(label)
    conservative_width = render_visuals._conservative_text_width(
        label,
        render_visuals.EDGE_LABEL_FONT_SIZE,
    )
    edge = make_edge(
        "font-probe",
        source="a",
        target="b",
        points=((0, 0), (10, 0)),
        label=label,
    )
    rectangle = next(line for line in render_visuals._render_edge_label(edge) if "<rect " in line)
    rendered_width = int(rectangle.split('width="', 1)[1].split('"', 1)[0])
    assert conservative_width >= actual_width
    assert rendered_width == render_visuals._edge_label_width(label)
    assert rendered_width >= math.ceil(actual_width) + 24


def test_rendered_stylesheet_is_balanced_and_contains_complete_lane_rule(
    tmp_path: Path,
) -> None:
    svg = render_visuals.render_diagram(load_fixture(tmp_path)).decode("utf-8")
    stylesheet = svg.split("<style>", 1)[1].split("</style>", 1)[0]
    depth = 0
    for brace in re.findall(r"[{}]", stylesheet):
        depth += 1 if brace == "{" else -1
        assert depth in {0, 1}
    assert depth == 0
    lane_rule = re.search(r"\.lane rect\s*\{([^{}]+)\}", stylesheet)
    assert lane_rule is not None
    assert "stroke-width: 1.5;" in lane_rule.group(1)
