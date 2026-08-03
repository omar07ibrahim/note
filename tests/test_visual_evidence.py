from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TextIO, cast
from unicodedata import east_asian_width
from xml.etree import ElementTree

import pytest

from recall_ledger import cli as cli_module
from recall_ledger import storage as storage_module
from recall_ledger.cli import (
    EXIT_BUSY,
    EXIT_INTERRUPTED,
    EXIT_OUTPUT,
    EXIT_STATE,
    EXIT_SUCCESS,
    EXIT_UNCERTAIN,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import (  # noqa: E402 - repository-local review tools
    render_cli_evidence,
    render_lexical_eval_visuals,
    render_visuals,
)

SOURCE_DIRECTORY = ROOT / "docs" / "visuals" / "sources"
OUTPUT_DIRECTORY = ROOT / "docs" / "visuals"
EXPECTED_SOURCES = {
    "architecture-workflow.v1.json",
    "reference-search-workflow.v1.json",
    "transaction-output-retry.v1.json",
}
EXPECTED_OUTPUTS = {
    "architecture-workflow",
    "reference-search-workflow",
    "transaction-output-retry",
}
EXPECTED_VISUAL_TREE = {
    Path("README.md"),
    Path("architecture-workflow.svg"),
    Path("evidence/installed-wheel-cli.v1.json"),
    Path("fixtures/cli-content-stale.json"),
    Path("fixtures/cli-content-v1.json"),
    Path("fixtures/cli-content-v2.json"),
    Path("installed-wheel-history-tombstone.svg"),
    Path("installed-wheel-write-replay.svg"),
    Path("lexical-search-eval-summary.svg"),
    Path("lexical-search-query-matrix.svg"),
    Path("lexical-search-ranking-breakdown.svg"),
    Path("reference-search-workflow.svg"),
    Path("sources/architecture-workflow.v1.json"),
    Path("sources/reference-search-workflow.v1.json"),
    Path("sources/transaction-output-retry.v1.json"),
    Path("transaction-output-retry.svg"),
}
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
UNREACHABLE_FLUSH_MESSAGE = "flush is unreachable"

TOP_LEVEL_KEYS = {
    "bindings",
    "canvas",
    "caption",
    "description",
    "diagram_kind",
    "edges",
    "entry",
    "lanes",
    "nodes",
    "notes",
    "schema_version",
    "slug",
    "subtitle",
    "title",
}
BINDING_KEYS = {"claim", "expected", "id", "kind", "match", "path", "target"}
LANE_KEYS = {"binding", "height", "id", "label", "width", "x", "y"}
NODE_KEYS = {
    "binding",
    "detail",
    "height",
    "id",
    "label",
    "lane",
    "variant",
    "width",
    "x",
    "y",
}
EDGE_KEYS = {
    "binding",
    "from",
    "id",
    "label",
    "label_x",
    "label_y",
    "points",
    "style",
    "to",
}
NOTE_KEYS = {"binding", "id", "label", "text", "variant", "width", "x", "y"}
Rectangle = tuple[float, float, float, float]


class CapturedStandardOutput:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.text = io.StringIO()

    def write(self, value: str) -> int:
        return self.text.write(value)

    def flush(self) -> None:
        self.text.flush()


def source_paths() -> tuple[Path, ...]:
    return tuple(
        OUTPUT_DIRECTORY / path
        for path in sorted(EXPECTED_VISUAL_TREE)
        if path.parts[0] == "sources"
    )


def source_object(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    assert type(value) is dict
    return cast(dict[str, object], value)


def exact_object(value: object) -> dict[str, object]:
    assert type(value) is dict
    return cast(dict[str, object], value)


def object_array(value: object) -> list[dict[str, object]]:
    assert type(value) is list
    return [exact_object(item) for item in cast(list[object], value)]


def conservative_text_width(
    value: str,
    *,
    font_size: float,
    letter_spacing: float = 0,
) -> float:
    em_width = 0.0
    for character in value:
        if east_asian_width(character) in {"F", "W"} or not character.isascii():
            em_width += 1.0
        elif character in "MW@#%&":
            em_width += 1.15
        elif character == "m":
            em_width += 1.1
        elif character == "w":
            em_width += 0.95
        elif character.isupper():
            em_width += 0.78
        elif character.islower():
            em_width += 0.65
        elif character.isdecimal():
            em_width += 0.7
        elif character.isspace():
            em_width += 0.36
        else:
            em_width += 0.55
    spacing = max(0, len(value) - 1) * letter_spacing
    return em_width * font_size * render_visuals.TEXT_WIDTH_SAFETY_FACTOR + spacing


def rectangles_overlap(left: Rectangle, right: Rectangle, *, gap: float = 0) -> bool:
    return (
        left[0] < right[2] + gap
        and left[2] > right[0] - gap
        and left[1] < right[3] + gap
        and left[3] > right[1] - gap
    )


def segment_intersects_rectangle(
    start: tuple[int, int],
    end: tuple[int, int],
    rectangle: Rectangle,
    *,
    gap: float = 0,
) -> bool:
    x_min, y_min, x_max, y_max = rectangle
    x_min -= gap
    y_min -= gap
    x_max += gap
    y_max += gap
    if start[0] == end[0]:
        segment_min, segment_max = sorted((start[1], end[1]))
        return x_min < start[0] < x_max and segment_min < y_max and segment_max > y_min
    assert start[1] == end[1], "visual edge segments must be orthogonal"
    segment_min, segment_max = sorted((start[0], end[0]))
    return y_min < start[1] < y_max and segment_min < x_max and segment_max > x_min


def relative_luminance(hex_color: str) -> float:
    channels = tuple(int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5))
    linear = tuple(
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    )
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def python_definitions(tree: ast.Module) -> dict[str, tuple[str, ast.AST]]:
    definitions: dict[str, tuple[str, ast.AST]] = {}

    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            definitions[statement.name] = ("function", statement)
        elif isinstance(statement, ast.ClassDef):
            definitions[statement.name] = ("class", statement)
            for member in statement.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    definitions[f"{statement.name}.{member.name}"] = ("method", member)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = ("constant", statement)
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            definitions[statement.target.id] = ("constant", statement)
    return definitions


def literal_assignments(tree: ast.Module) -> dict[str, object]:
    values: dict[str, object] = {}
    for statement in tree.body:
        target: ast.Name | None = None
        value: ast.expr | None = None
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            target = statement.target
            value = statement.value
        if target is not None and value is not None:
            try:
                values[target.id] = cast(object, ast.literal_eval(value))
            except (ValueError, TypeError):
                continue
    return values


def function_definition(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef) and statement.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def ordered_call_lines(function: ast.FunctionDef) -> dict[str, list[int]]:
    calls: dict[str, list[int]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name: str | None = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name is not None:
            calls.setdefault(name, []).append(node.lineno)
    for lines in calls.values():
        lines.sort()
    return calls


def write_source(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )


def resolve_python_binding(binding: render_visuals.Binding) -> None:
    source = (ROOT / binding.path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = python_definitions(tree)
    if binding.target == "__doc__":
        assert binding.match == "contains"
        target_source = ast.get_docstring(tree, clean=False)
        assert target_source is not None
        assert binding.expected in target_source
        return

    assert binding.target in definitions
    definition_kind, definition = definitions[binding.target]
    if binding.match == "defined":
        assert definition_kind == binding.expected
    elif binding.match == "equals":
        assert str(literal_assignments(tree)[binding.target]) == binding.expected
    else:
        target_source = ast.get_source_segment(source, definition)
        assert target_source is not None
        assert binding.expected in target_source


def resolve_toml_binding(binding: render_visuals.Binding) -> None:
    value: object = tomllib.loads((ROOT / binding.path).read_text(encoding="utf-8"))
    for component in binding.target.split("."):
        assert type(value) is dict
        value = cast(dict[str, object], value)[component]
    assert binding.match == "equals"
    assert value == binding.expected


def markdown_section(source: str, target: str) -> str:
    lines = source.splitlines()
    start: int | None = None
    level: int | None = None
    for index, line in enumerate(lines):
        match = re.fullmatch(r"(#{1,6})\s+(.+)", line)
        if match is None:
            continue
        heading_target = re.sub(
            r"[^a-z0-9]+",
            "-",
            match.group(2).casefold(),
        ).strip("-")
        if start is None:
            if heading_target == target:
                start = index + 1
                level = len(match.group(1))
            continue
        assert level is not None
        if len(match.group(1)) <= level:
            return "\n".join(lines[start:index])
    assert start is not None
    return "\n".join(lines[start:])


def resolve_binding(binding: render_visuals.Binding) -> None:
    if binding.kind == "python":
        resolve_python_binding(binding)
    elif binding.kind == "toml":
        resolve_toml_binding(binding)
    else:
        assert binding.kind == "contract"
        assert binding.match == "contains"
        source = (ROOT / binding.path).read_text(encoding="utf-8")
        assert binding.expected in markdown_section(source, binding.target)


def test_visual_sources_use_closed_versioned_bounded_schema(tmp_path: Path) -> None:
    colocated = frozenset(render_cli_evidence.OUTPUT_NAMES) | frozenset(
        render_lexical_eval_visuals.OUTPUT_NAMES
    )
    assert colocated == render_visuals.COLOCATED_SVG_NAMES
    visual_tree = {
        path.relative_to(OUTPUT_DIRECTORY) for path in OUTPUT_DIRECTORY.rglob("*") if path.is_file()
    }
    assert visual_tree == EXPECTED_VISUAL_TREE
    paths = source_paths()
    assert {path.name for path in paths} == EXPECTED_SOURCES
    for path in paths:
        assert path.stat().st_size <= render_visuals.MAX_SOURCE_BYTES
        source = source_object(path)
        assert set(source) == TOP_LEVEL_KEYS
        assert source["schema_version"] == 1
        assert all(set(value) == BINDING_KEYS for value in object_array(source["bindings"]))
        assert all(set(value) == LANE_KEYS for value in object_array(source["lanes"]))
        assert all(set(value) == NODE_KEYS for value in object_array(source["nodes"]))
        assert all(set(value) == EDGE_KEYS for value in object_array(source["edges"]))
        assert all(set(value) == NOTE_KEYS for value in object_array(source["notes"]))
        render_visuals.load_diagram(path)

    invalid = source_object(paths[0])
    invalid["unknown"] = "rejected"
    invalid_path = tmp_path / "invalid.v1.json"
    write_source(invalid_path, invalid)
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals.load_diagram(invalid_path)


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "unsafe\u0085label",
        "unsafe\u2028label",
        "unsafe\u202elabel",
        "unsafe\ud800label",
        "unsafe\ufdd0label",
        "unsafe\ufffelabel",
        "unsafe\U0001fffelabel",
    ],
)
def test_visual_sources_reject_controls_surrogates_and_noncharacters(
    tmp_path: Path,
    unsafe_text: str,
) -> None:
    source = source_object(source_paths()[0])
    source["title"] = unsafe_text
    path = tmp_path / "unsafe.v1.json"
    write_source(path, source)
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals.load_diagram(path)


def test_visual_sources_reject_oversized_integer_lexemes(tmp_path: Path) -> None:
    source = source_paths()[0].read_text(encoding="utf-8")
    oversized = source.replace(
        '"schema_version": 1',
        f'"schema_version": {"9" * 5_000}',
        1,
    )
    path = tmp_path / "oversized-integer.v1.json"
    path.write_text(oversized, encoding="utf-8")
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals.load_diagram(path)


@pytest.mark.parametrize("geometry", ["edge-label", "note"])
def test_visual_sources_reject_derived_geometry_outside_canvas(
    tmp_path: Path,
    geometry: str,
) -> None:
    source = source_object(source_paths()[0])
    canvas = exact_object(source["canvas"])
    if geometry == "edge-label":
        edge = object_array(source["edges"])[0]
        edge["label_x"] = 0
    else:
        note = object_array(source["notes"])[0]
        note["y"] = cast(int, canvas["height"]) - 1
    path = tmp_path / "outside.v1.json"
    write_source(path, source)
    with pytest.raises(render_visuals.VisualSourceError):
        render_visuals.load_diagram(path)


def test_visual_copy_fits_every_rendered_container_conservatively() -> None:
    for path in source_paths():
        diagram = render_visuals.load_diagram(path)
        assert conservative_text_width(diagram.title, font_size=34) <= (diagram.canvas.width - 96)
        assert conservative_text_width(diagram.subtitle, font_size=17) <= (
            diagram.canvas.width - 96
        )
        for lane in diagram.lanes:
            assert conservative_text_width(
                lane.label,
                font_size=13,
                letter_spacing=1.4,
            ) <= (lane.width - 48)
        for node in diagram.nodes:
            available = node.width - 44
            assert conservative_text_width(node.label, font_size=17) <= available
            assert all(
                conservative_text_width(line, font_size=13) <= available for line in node.detail
            )
        for edge in diagram.edges:
            available = render_visuals._edge_label_width(edge.label) - 24
            assert (
                conservative_text_width(
                    edge.label,
                    font_size=render_visuals.EDGE_LABEL_FONT_SIZE,
                )
                <= available
            )
        for note in diagram.notes:
            available = note.width - 40
            assert conservative_text_width(note.label, font_size=17) <= available
            assert all(
                conservative_text_width(line, font_size=13) <= available for line in note.text
            )


def visual_rectangle_groups(
    diagram: render_visuals.Diagram,
) -> tuple[tuple[tuple[str, Rectangle], ...], tuple[tuple[str, Rectangle], ...]]:
    node_rectangles: dict[str, Rectangle] = {
        node.identifier: (
            node.x,
            node.y,
            node.x + node.width,
            node.y + node.height,
        )
        for node in diagram.nodes
    }
    note_rectangles: dict[str, Rectangle] = {
        note.identifier: (
            note.x,
            note.y,
            note.x + note.width,
            note.y + 55 + len(note.text) * 20,
        )
        for note in diagram.notes
    }
    label_rectangles: dict[str, Rectangle] = {}
    for edge in diagram.edges:
        width = render_visuals._edge_label_width(edge.label)
        label_rectangles[edge.identifier] = (
            edge.label_x - width // 2,
            edge.label_y - 17,
            edge.label_x + (width - width // 2),
            edge.label_y + 9,
        )
    return tuple({**node_rectangles, **note_rectangles}.items()), tuple(label_rectangles.items())


def assert_visual_rectangles_clear(
    path: Path,
    content_items: tuple[tuple[str, Rectangle], ...],
    label_items: tuple[tuple[str, Rectangle], ...],
) -> None:
    for index, (left_id, left) in enumerate(content_items):
        for right_id, right in content_items[index + 1 :]:
            assert not rectangles_overlap(left, right, gap=4), (
                path.name,
                left_id,
                right_id,
            )
    for index, (left_id, left) in enumerate(label_items):
        for right_id, right in label_items[index + 1 :]:
            assert not rectangles_overlap(left, right, gap=4), (
                path.name,
                left_id,
                right_id,
            )
        for content_id, content in content_items:
            assert not rectangles_overlap(left, content, gap=4), (
                path.name,
                left_id,
                content_id,
            )


def assert_visual_routes_clear(
    path: Path,
    diagram: render_visuals.Diagram,
    content_items: tuple[tuple[str, Rectangle], ...],
    label_items: tuple[tuple[str, Rectangle], ...],
) -> None:
    edge_by_id = {edge.identifier: edge for edge in diagram.edges}
    for label_id, label in label_items:
        for edge_id, edge in edge_by_id.items():
            if edge_id == label_id:
                continue
            for start, end in zip(edge.points, edge.points[1:], strict=False):
                assert not segment_intersects_rectangle(start, end, label, gap=2), (
                    path.name,
                    label_id,
                    edge_id,
                )
    for edge in diagram.edges:
        excluded = {edge.source, edge.target}
        for content_id, content in content_items:
            if content_id in excluded:
                continue
            for start, end in zip(edge.points, edge.points[1:], strict=False):
                assert not segment_intersects_rectangle(start, end, content), (
                    path.name,
                    edge.identifier,
                    content_id,
                )


def test_visual_labels_and_routes_do_not_collide_with_other_content() -> None:
    for path in source_paths():
        diagram = render_visuals.load_diagram(path)
        content_items, label_items = visual_rectangle_groups(diagram)
        assert_visual_rectangles_clear(path, content_items, label_items)
        assert_visual_routes_clear(path, diagram, content_items, label_items)


def test_lane_label_contrast_has_accessibility_margin() -> None:
    rendered = next(iter(render_visuals.render_sources().values())).decode("utf-8")
    lane_fill = re.search(r"\.lane rect \{ fill: (#[0-9a-f]{6});", rendered)
    lane_label = re.search(r"\.lane-label \{[^}]+fill: (#[0-9a-f]{6});", rendered)
    assert lane_fill is not None
    assert lane_label is not None
    assert contrast_ratio(lane_label.group(1), lane_fill.group(1)) >= 4.8


def test_every_visual_claim_resolves_to_code_packaging_or_contract() -> None:
    diagrams = tuple(render_visuals.load_diagram(path) for path in source_paths())
    for diagram in diagrams:
        for binding in diagram.bindings:
            binding_path = ROOT / binding.path
            assert binding_path.resolve(strict=True).is_relative_to(ROOT.resolve(strict=True))
            current = ROOT
            for component in Path(binding.path).parts:
                current /= component
                assert not current.is_symlink()
            resolve_binding(binding)

    architecture = next(diagram for diagram in diagrams if diagram.diagram_kind == "architecture")
    architecture_bindings = {node.identifier: node.binding for node in architecture.nodes}
    assert architecture_bindings["bounded-cli-input"] == "bounded-input"
    assert architecture_bindings["write-transaction"] == "write-guard"
    assert architecture_bindings["canonical-event"] == "event-contract"
    assert architecture_bindings["event-log"] == "event-schema"
    assert architecture_bindings["head-projection"] == "head-schema"
    assert architecture_bindings["machine-json-output"] == "machine-output"
    architecture_edges = {edge.identifier: edge for edge in architecture.edges}
    assert architecture_edges["transaction-to-event"].label == "new command: derive"
    architecture_notes = {note.identifier: note for note in architecture.notes}
    replay_note = architecture_notes["replay-bypass-note"]
    assert replay_note.binding == "transition-order"
    assert replay_note.text == (
        "Stored event returns after proof.",
        "No event append or head write.",
    )


def test_binding_targets_are_semantic_not_decorative() -> None:
    bindings = {
        binding.identifier: binding
        for path in source_paths()
        for binding in render_visuals.load_diagram(path).bindings
    }
    with pytest.raises(AssertionError):
        resolve_python_binding(replace(bindings["event-schema"], target="_CREATE_HEADS"))
    with pytest.raises(AssertionError):
        resolve_binding(
            replace(
                bindings["head-write"],
                target="stored-row-reconciliation",
            )
        )
    with pytest.raises(KeyError):
        resolve_toml_binding(replace(bindings["cli-entry"], target="project.scripts.missing"))


def test_architecture_order_claims_are_bound_to_current_call_order() -> None:
    operations_tree = ast.parse(
        (ROOT / "src" / "recall_ledger" / "_ledger_operations.py").read_text(encoding="utf-8")
    )
    transition_calls = ordered_call_lines(
        function_definition(operations_tree, "_transition_in_transaction")
    )
    assert (
        transition_calls["_load_command_event"][0] < transition_calls["_create_event_for_intent"][0]
    )
    assert transition_calls["_load_command_event"][0] < transition_calls["_load_head"][0]
    assert (
        transition_calls["_load_head"][0]
        < transition_calls["_next_recorded_at_us"][0]
        < transition_calls["_insert_event"][-1]
        < transition_calls["_cas_head"][0]
    )

    creation_calls = ordered_call_lines(
        function_definition(operations_tree, "_create_event_for_intent")
    )
    assert (
        creation_calls["_new_note_id"][0]
        < creation_calls["_note_storage_exists"][0]
        < creation_calls["_next_recorded_at_us"][0]
    )

    cli_tree = ast.parse((ROOT / "src" / "recall_ledger" / "cli.py").read_text(encoding="utf-8"))
    execute = function_definition(cli_tree, "_execute")
    execute_calls = ordered_call_lines(execute)
    result_returns = [
        node.lineno
        for node in ast.walk(execute)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_RenderedResult"
    ]
    assert result_returns and max(execute_calls["close"]) < result_returns[0]

    run_calls = ordered_call_lines(function_definition(cli_tree, "run_cli"))
    assert run_calls["_execute"][0] < run_calls["_serialize"][0] < run_calls["_write"][0]


def test_failure_visual_binds_exact_phases_exit_codes_and_retry_edges() -> None:
    state_diagram = next(
        render_visuals.load_diagram(path)
        for path in source_paths()
        if render_visuals.load_diagram(path).diagram_kind == "state_machine"
    )
    assert tuple(phase.value for phase in storage_module._WriteTransactionPhase) == (
        "before_begin",
        "active",
        "commit_call",
        "commit_returned",
        "committed",
    )
    assert {
        EXIT_SUCCESS,
        EXIT_STATE,
        EXIT_BUSY,
        EXIT_UNCERTAIN,
        EXIT_OUTPUT,
        EXIT_INTERRUPTED,
    } == {0, 11, 12, 14, 74, 130}
    assert state_diagram.entry == "mutation-invocation"
    nodes = {node.identifier: node for node in state_diagram.nodes}
    assert nodes["settlement-guard"].binding == "write-phases"
    assert nodes["settlement-guard"].detail == (
        "before_begin → active",
        "active → commit_call",
        "returned → committed",
    )
    assert nodes["settled-operation"].binding == "write-phases"
    labels = {node.label for node in nodes.values()}
    assert {
        "Open / write guard",
        "Settled",
        "Poisoned",
        "Success · 0",
        "State · 11",
        "Busy · 12",
        "Uncertain · 14",
        "Stdout · 74",
        "Error-channel delivery",
    } <= labels
    assert nodes["state-error"].detail == (
        "revision → inspect",
        "idempotency → stop",
        "absent/tomb. → none",
    )
    assert nodes["busy-error"].detail == (
        "open: no ledger",
        "write: no commit",
        "exact / same retry",
    )
    assert nodes["retry-policy"].detail == (
        "state: inspect / stop / none · busy: exact / same",
        "uncertain: reopen · output: settlement-aware",
    )
    edges = {edge.identifier: edge for edge in state_diagram.edges}
    edge_bindings = {identifier: edge.binding for identifier, edge in edges.items()}
    assert edge_bindings["invoke-to-guard"] == "begin-proof"
    assert edge_bindings["guard-to-settled"] == "write-phases"
    assert edge_bindings["guard-to-poisoned"] == "failure-settlement"
    assert edge_bindings["guard-to-state"] == "state-exit"
    assert edge_bindings["guard-to-busy"] == "busy-exit"
    assert edge_bindings["settled-to-serialize"] == "cli-run"
    assert edge_bindings["poisoned-to-uncertain"] == "uncertain-exit"
    assert edge_bindings["serialize-to-stdout-failure"] == "output-exit"
    assert edge_bindings["state-to-policy"] == "retry-guidance"
    assert edge_bindings["busy-to-policy"] == "retry-guidance"
    assert edge_bindings["uncertain-to-policy"] == "retry-guidance"
    assert edge_bindings["stdout-to-policy"] == "command-retry"
    assert edge_bindings["policy-to-error-channel"] == "error-emission"
    assert edges["state-to-policy"].label == "per code"
    note_bindings = {note.identifier: note.binding for note in state_diagram.notes}
    assert note_bindings["exact-replay-note"] == "exact-replay"
    assert note_bindings["rollback-proof-note"] == "verified-rollback"
    assert note_bindings["interruption-note"] == "interrupted-exit"

    assert cli_module._retry_guidance("LEDGER_BUSY", command="create") == "retry_exact_command"
    assert (
        cli_module._retry_guidance("REVISION_CONFLICT", command="revise")
        == "inspect_head_then_new_command"
    )
    assert (
        cli_module._retry_guidance("IDEMPOTENCY_CONFLICT", command="create")
        == "do_not_retry_same_command"
    )
    assert cli_module._retry_guidance("NOTE_NOT_FOUND", command="revise") == "none"
    assert cli_module._retry_guidance("NOTE_TOMBSTONED", command="revise") == "none"
    assert cli_module._retry_guidance("MIGRATION_BUSY", command="create") == "retry_exact_command"
    assert (
        cli_module._retry_guidance("COMMIT_OUTCOME_UNKNOWN", command="create")
        == "reopen_and_retry_exact_command"
    )
    assert cli_module._command_retry("history") == "retry_same_invocation"

    class UnavailableErrorStream:
        def write(self, _value: str) -> int:
            raise OSError

        def flush(self) -> None:
            raise AssertionError(UNREACHABLE_FLUSH_MESSAGE)

    class InterruptedErrorStream:
        def write(self, _value: str) -> int:
            raise KeyboardInterrupt

        def flush(self) -> None:
            raise AssertionError(UNREACHABLE_FLUSH_MESSAGE)

    assert (
        cli_module._emit_error(
            cast(TextIO, UnavailableErrorStream()),
            exit_code=EXIT_UNCERTAIN,
            code="COMMIT_OUTCOME_UNKNOWN",
            message="bounded",
            retry="reopen_and_retry_exact_command",
        )
        == EXIT_OUTPUT
    )
    assert (
        cli_module._emit_error(
            cast(TextIO, InterruptedErrorStream()),
            exit_code=EXIT_STATE,
            code="REVISION_CONFLICT",
            message="bounded",
            retry="inspect_head_then_new_command",
        )
        == EXIT_INTERRUPTED
    )
    error_channel = next(node for node in state_diagram.nodes if node.identifier == "error-channel")
    assert "mapped error attempts stderr JSON" in error_channel.detail
    assert "write failure can hide its exit and retry hint" in error_channel.detail
    interruption_note = next(
        note for note in state_diagram.notes if note.identifier == "interruption-note"
    )
    assert any("stderr emission" in line for line in interruption_note.text)


def test_renderer_cli_write_check_stdout_and_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_directory = tmp_path / "rendered"
    common = [
        "--source-dir",
        str(SOURCE_DIRECTORY),
        "--output-dir",
        str(output_directory),
    ]
    assert render_visuals.main(["--check", *common]) == 1
    assert render_visuals.main(["--write", *common]) == 0
    assert render_visuals.main(["--check", *common]) == 0

    captured = CapturedStandardOutput()
    monkeypatch.setattr(sys, "stdout", captured)
    assert (
        render_visuals.main(
            ["--stdout", "architecture-workflow", "--source-dir", str(SOURCE_DIRECTORY)]
        )
        == 0
    )
    assert captured.buffer.getvalue().startswith(b"<?xml version=")
    assert (
        render_visuals.main(["--stdout", "not-a-diagram", "--source-dir", str(SOURCE_DIRECTORY)])
        == 2
    )

    invalid_directory = tmp_path / "invalid-sources"
    invalid_directory.mkdir()
    invalid = source_object(source_paths()[0])
    invalid["unknown"] = "rejected"
    write_source(invalid_directory / "invalid.v1.json", invalid)
    captured_error = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_error)
    assert (
        render_visuals.main(
            [
                "--check",
                "--source-dir",
                str(invalid_directory),
                "--output-dir",
                str(output_directory),
            ]
        )
        == 2
    )
    assert captured_error.getvalue() == "visual rendering failed\n"


def test_svg_rerender_is_byte_exact_and_accessible() -> None:
    first = render_visuals.render_sources()
    second = render_visuals.render_sources()
    assert first == second
    assert set(first) == EXPECTED_OUTPUTS

    for slug, payload in first.items():
        assert (OUTPUT_DIRECTORY / f"{slug}.svg").read_bytes() == payload
        root = ElementTree.fromstring(  # noqa: S314 - parses our bounded local renderer output
            payload
        )
        assert root.tag == f"{{{SVG_NAMESPACE}}}svg"
        assert root.attrib["role"] == "img"
        labelled = root.attrib["aria-labelledby"].split()
        assert len(labelled) == 2
        title = root.find(f"{{{SVG_NAMESPACE}}}title")
        description = root.find(f"{{{SVG_NAMESPACE}}}desc")
        assert title is not None and title.attrib["id"] == labelled[0] and title.text
        assert (
            description is not None and description.attrib["id"] == labelled[1] and description.text
        )
        binding_groups = [element for element in root.iter() if "data-binding" in element.attrib]
        assert binding_groups


def test_sources_and_svg_have_no_external_assets_host_paths_or_sensitive_data() -> None:
    payload = b"".join(
        (OUTPUT_DIRECTORY / relative_path).read_bytes()
        for relative_path in sorted(EXPECTED_VISUAL_TREE)
    )
    payload += b"".join(render_visuals.render_sources().values())
    text = payload.decode("utf-8")
    without_svg_namespace = text.replace(SVG_NAMESPACE, "")

    assert not re.search(r"(?:https?|file|data)://", without_svg_namespace)
    assert not re.search(r"(?:href|xlink:href)=", text, flags=re.IGNORECASE)
    assert not re.search(r"<(?:script|foreignObject|image)\b", text, flags=re.IGNORECASE)
    assert "/home/" not in text
    assert "/Users/" not in text
    assert "file://" not in text
    assert "-----BEGIN " not in text
    assert not re.search(r"\bAKIA[0-9A-Z]{16}\b", text)
    assert not re.search(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b", text)
    assert not re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text)


def test_sdist_carries_self_contained_visual_renderer(tmp_path: Path) -> None:
    distribution_directory = tmp_path / "dist"
    build = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned arguments
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--skip-dependency-check",
            "--outdir",
            str(distribution_directory),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    archives = tuple(distribution_directory.glob("recall_ledger-*.tar.gz"))
    assert len(archives) == 1

    extraction_directory = tmp_path / "extracted"
    extraction_directory.mkdir()
    with tarfile.open(archives[0], mode="r:gz") as archive:
        members = archive.getmembers()
        member_paths = [Path(member.name) for member in members]
        assert member_paths
        assert all(not path.is_absolute() and ".." not in path.parts for path in member_paths)
        roots = {path.parts[0] for path in member_paths if path.parts}
        assert len(roots) == 1
        root_name = next(iter(roots))
        file_members = {
            Path(*path.parts[1:]).as_posix(): member
            for path, member in zip(member_paths, members, strict=True)
            if len(path.parts) > 1 and member.isfile()
        }
        expected_visual_members = {
            f"docs/visuals/{relative_path.as_posix()}" for relative_path in EXPECTED_VISUAL_TREE
        }
        assert {
            path for path in file_members if path.startswith("docs/visuals/")
        } == expected_visual_members
        required_review_files = {
            "tests/test_cli_evidence.py",
            "tests/test_cli_evidence_hardening.py",
            "tests/test_lexical_eval_visuals.py",
            "tests/test_visual_evidence.py",
            "tests/test_visual_renderer_hardening.py",
            "tools/__init__.py",
            "tools/capture_cli_evidence.py",
            "tools/cli_evidence_contract.py",
            "tools/lexical_eval_contract.py",
            "tools/render_cli_evidence.py",
            "tools/render_lexical_eval_visuals.py",
            "tools/render_visuals.py",
        }
        assert required_review_files <= file_members.keys()
        for path in expected_visual_members | required_review_files:
            assert file_members[path].mode & 0o777 == 0o644
        archive.extractall(extraction_directory, filter="data")

    extracted_root = extraction_directory / root_name
    subprocess_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COV_CORE_") and key != "COVERAGE_PROCESS_START"
    }
    render_check = subprocess.run(  # noqa: S603 - fixed interpreter and repository tool
        [sys.executable, "tools/render_visuals.py", "--check"],
        cwd=extracted_root,
        check=False,
        capture_output=True,
        env=subprocess_environment,
        text=True,
    )
    assert render_check.returncode == 0, render_check.stderr

    terminal_check = subprocess.run(  # noqa: S603 - fixed interpreter and repository tool
        [sys.executable, "tools/render_cli_evidence.py", "--check"],
        cwd=extracted_root,
        check=False,
        capture_output=True,
        env=subprocess_environment,
        text=True,
    )
    assert terminal_check.returncode == 0, terminal_check.stderr

    lexical_check = subprocess.run(  # noqa: S603 - extracted repository-owned renderer
        [sys.executable, "tools/render_lexical_eval_visuals.py", "--check"],
        cwd=extracted_root,
        check=False,
        capture_output=True,
        env=subprocess_environment,
        text=True,
    )
    assert lexical_check.returncode == 0, lexical_check.stderr

    import_check = subprocess.run(  # noqa: S603 - fixed interpreter and constant probe
        [
            sys.executable,
            "-c",
            (
                "from tools import capture_cli_evidence,cli_evidence_contract,"
                "render_cli_evidence,render_lexical_eval_visuals,render_visuals;"
                "assert capture_cli_evidence.EXPECTED_WHEEL_NAME.endswith('.whl');"
                "assert len(cli_evidence_contract.FIXTURE_FILES)==3;"
                "assert len(render_cli_evidence.OUTPUT_NAMES)==2;"
                "assert len(render_lexical_eval_visuals.OUTPUT_NAMES)==3;"
                "assert len(render_visuals.render_sources())==3"
            ),
        ],
        cwd=extracted_root,
        check=False,
        capture_output=True,
        env=subprocess_environment,
        text=True,
    )
    assert import_check.returncode == 0, import_check.stderr
