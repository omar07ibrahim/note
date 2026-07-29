#!/usr/bin/env python3
"""Render bounded, source-bound RecallLedger diagrams as deterministic SVG."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from math import ceil, hypot
from pathlib import Path
from typing import BinaryIO, Final, NoReturn, cast
from unicodedata import category, east_asian_width
from xml.sax.saxutils import escape, quoteattr

ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIRECTORY: Final = ROOT / "docs" / "visuals" / "sources"
DEFAULT_OUTPUT_DIRECTORY: Final = ROOT / "docs" / "visuals"
COLOCATED_SVG_NAMES: Final = frozenset(
    {
        "installed-wheel-history-tombstone.svg",
        "installed-wheel-write-replay.svg",
    }
)

MAX_SOURCE_BYTES: Final = 65_536
MAX_SOURCE_FILES: Final = 12
MAX_DIRECTORY_ENTRIES: Final = 256
MAX_TOTAL_SOURCE_BYTES: Final = 524_288
MAX_RENDERED_SVG_BYTES: Final = 524_288
MAX_TOTAL_RENDERED_BYTES: Final = 2_097_152
READ_CHUNK_BYTES: Final = 8_192
MAX_BINDINGS: Final = 64
MAX_LANES: Final = 8
MAX_NODES: Final = 32
MAX_EDGES: Final = 64
MAX_NOTES: Final = 12
MAX_DETAIL_LINES: Final = 5
MAX_POINTS: Final = 8
MAX_TEXT_LENGTH: Final = 900
MAX_JSON_INTEGER_CHARACTERS: Final = 8
MIN_CANVAS_SIZE: Final = 640
MAX_CANVAS_SIZE: Final = 2_400
TITLE_FONT_SIZE: Final = 34
SUBTITLE_FONT_SIZE: Final = 17
LANE_FONT_SIZE: Final = 13
LANE_LETTER_SPACING: Final = 1.4
NODE_TITLE_FONT_SIZE: Final = 17
DETAIL_FONT_SIZE: Final = 13
EDGE_LABEL_FONT_SIZE: Final = 12
CAPTION_FONT_SIZE: Final = 12
MIN_TEXT_CONTRAST: Final = 4.75
MIN_NON_TEXT_CONTRAST: Final = 3.0
TEXT_WIDTH_SAFETY_FACTOR: Final = 1.08
MAX_EDGE_LABEL_ROUTE_DISTANCE: Final = 40.0
DIAGRAM_CONTENT_TOP: Final = 118
DIAGRAM_FOOTER_HEIGHT: Final = 50
SRGB_LINEAR_THRESHOLD: Final = 0.04045
SRGB_LINEAR_DIVISOR: Final = 12.92
SRGB_OFFSET: Final = 0.055
SRGB_SCALE: Final = 1.055
SRGB_GAMMA: Final = 2.4
LANE_BACKGROUND: Final = "#f7f9fc"
CANVAS_BACKGROUND: Final = "#fbfcfe"
LANE_BORDER: Final = "#66768a"
SECONDARY_TEXT: Final = "#405166"
MUTED_TEXT: Final = "#526276"
CONTROL_CODEPOINT_LIMIT: Final = 32
C1_CONTROL_MIN: Final = 0x007F
C1_CONTROL_MAX: Final = 0x009F
SURROGATE_MIN: Final = 0xD800
SURROGATE_MAX: Final = 0xDFFF
NONCHARACTER_MIN: Final = 0xFDD0
NONCHARACTER_MAX: Final = 0xFDEF
_BIDI_CONTROL_CODEPOINTS: Final = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x2028,
        0x2029,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)

_ID_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_SOURCE_NAME_PATTERN: Final = re.compile(r"([a-z][a-z0-9-]{0,63})\.v1\.json\Z")
_BINDING_KINDS: Final = frozenset({"contract", "python", "toml"})
_BINDING_MATCHES: Final = frozenset({"contains", "defined", "equals"})
_DIAGRAM_KINDS: Final = frozenset({"architecture", "state_machine"})
_NODE_VARIANTS: Final = frozenset(
    {"accent", "neutral", "output", "primary", "storage", "strong", "warning"}
)
_EDGE_STYLES: Final = frozenset({"dashed", "solid", "warning"})
_DIRECTORY_OPEN_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_REGULAR_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_REGULAR_WRITE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)

JsonObject = dict[str, object]


class VisualSourceError(ValueError):
    """A bounded visual source violated its closed contract."""


class _MissingVisualPathError(VisualSourceError):
    """A safely walked input or output path does not exist."""

    def __init__(self) -> None:
        super().__init__("visual filesystem path does not exist")


@dataclass(frozen=True, slots=True)
class Canvas:
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Rectangle:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Binding:
    identifier: str
    kind: str
    path: str
    target: str
    match: str
    expected: str
    claim: str


@dataclass(frozen=True, slots=True)
class Lane:
    identifier: str
    binding: str
    label: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Node:
    identifier: str
    binding: str
    lane: str
    label: str
    detail: tuple[str, ...]
    x: int
    y: int
    width: int
    height: int
    variant: str


@dataclass(frozen=True, slots=True)
class Edge:
    identifier: str
    binding: str
    source: str
    target: str
    label: str
    label_x: int
    label_y: int
    style: str
    points: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class Note:
    identifier: str
    binding: str
    label: str
    text: tuple[str, ...]
    x: int
    y: int
    width: int
    variant: str


@dataclass(frozen=True, slots=True)
class Diagram:
    source_name: str
    schema_version: int
    diagram_kind: str
    slug: str
    entry: str
    title: str
    subtitle: str
    description: str
    caption: str
    canvas: Canvas
    bindings: tuple[Binding, ...]
    lanes: tuple[Lane, ...]
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    notes: tuple[Note, ...]


@dataclass(frozen=True, slots=True)
class _SourceEntry:
    name: str
    device: int
    inode: int


def _fail(message: str) -> NoReturn:
    raise VisualSourceError(message)


def _reject_number(_value: str) -> NoReturn:
    _fail("visual sources do not permit non-integer numbers")


def _parse_integer(value: str) -> int:
    digits = value.removeprefix("-")
    if not digits or len(digits) > MAX_JSON_INTEGER_CHARACTERS:
        _fail("visual source integer is outside the lexical bound")
    return int(value)


def _unique_object(pairs: list[tuple[str, object]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            _fail("visual sources do not permit duplicate object keys")
        result[key] = value
    return result


def _object(value: object, keys: frozenset[str], context: str) -> JsonObject:
    if type(value) is not dict:
        _fail(f"{context} must be an object")
    result = cast(JsonObject, value)
    if frozenset(result) != keys:
        _fail(f"{context} has unknown or missing keys")
    return result


def _array(value: object, *, context: str, maximum: int, minimum: int = 0) -> list[object]:
    if type(value) is not list:
        _fail(f"{context} must be an array")
    result = cast(list[object], value)
    if not minimum <= len(result) <= maximum:
        _fail(f"{context} has an unsupported item count")
    return result


def _text(value: object, *, context: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail(f"{context} must be bounded non-empty text")
    if any(
        (codepoint := ord(character)) < CONTROL_CODEPOINT_LIMIT
        or C1_CONTROL_MIN <= codepoint <= C1_CONTROL_MAX
        or SURROGATE_MIN <= codepoint <= SURROGATE_MAX
        or NONCHARACTER_MIN <= codepoint <= NONCHARACTER_MAX
        or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
        or codepoint in _BIDI_CONTROL_CODEPOINTS
        or category(character) == "Cf"
        for character in value
    ):
        _fail(f"{context} contains an unsafe control or Unicode scalar")
    return value


def _identifier(value: object, *, context: str) -> str:
    result = _text(value, context=context, maximum=64)
    if _ID_PATTERN.fullmatch(result) is None:
        _fail(f"{context} is not a canonical identifier")
    return result


def _integer(
    value: object,
    *,
    context: str,
    minimum: int = 0,
    maximum: int = MAX_CANVAS_SIZE,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{context} is outside its integer bounds")
    return value


def _relative_path(value: object, *, context: str) -> str:
    result = _text(value, context=context, maximum=180)
    path = Path(result)
    if path.is_absolute() or ".." in path.parts or result.startswith("."):
        _fail(f"{context} must stay relative to the repository")
    if path.suffix not in {".md", ".py", ".toml"}:
        _fail(f"{context} uses an unsupported source type")
    return result


def _text_tuple(
    value: object,
    *,
    context: str,
    maximum: int,
    minimum: int = 1,
) -> tuple[str, ...]:
    items = _array(value, context=context, maximum=maximum, minimum=minimum)
    return tuple(_text(item, context=f"{context} item", maximum=120) for item in items)


def _parse_binding(value: object, index: int) -> Binding:
    context = f"bindings[{index}]"
    obj = _object(
        value,
        frozenset({"claim", "expected", "id", "kind", "match", "path", "target"}),
        context,
    )
    kind = _text(obj["kind"], context=f"{context}.kind", maximum=16)
    match = _text(obj["match"], context=f"{context}.match", maximum=16)
    if kind not in _BINDING_KINDS or match not in _BINDING_MATCHES:
        _fail(f"{context} uses an unsupported binding contract")
    return Binding(
        identifier=_identifier(obj["id"], context=f"{context}.id"),
        kind=kind,
        path=_relative_path(obj["path"], context=f"{context}.path"),
        target=_text(obj["target"], context=f"{context}.target", maximum=120),
        match=match,
        expected=_text(obj["expected"], context=f"{context}.expected", maximum=260),
        claim=_text(obj["claim"], context=f"{context}.claim", maximum=260),
    )


def _parse_lane(value: object, index: int, canvas: Canvas) -> Lane:
    context = f"lanes[{index}]"
    obj = _object(
        value,
        frozenset({"binding", "height", "id", "label", "width", "x", "y"}),
        context,
    )
    lane = Lane(
        identifier=_identifier(obj["id"], context=f"{context}.id"),
        binding=_identifier(obj["binding"], context=f"{context}.binding"),
        label=_text(obj["label"], context=f"{context}.label", maximum=48),
        x=_integer(obj["x"], context=f"{context}.x"),
        y=_integer(obj["y"], context=f"{context}.y"),
        width=_integer(obj["width"], context=f"{context}.width", minimum=160),
        height=_integer(obj["height"], context=f"{context}.height", minimum=160),
    )
    if lane.x + lane.width > canvas.width or lane.y + lane.height > canvas.height:
        _fail(f"{context} exceeds the canvas")
    return lane


def _parse_node(value: object, index: int, canvas: Canvas) -> Node:
    context = f"nodes[{index}]"
    obj = _object(
        value,
        frozenset(
            {
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
        ),
        context,
    )
    variant = _text(obj["variant"], context=f"{context}.variant", maximum=16)
    if variant not in _NODE_VARIANTS:
        _fail(f"{context} uses an unsupported visual variant")
    node = Node(
        identifier=_identifier(obj["id"], context=f"{context}.id"),
        binding=_identifier(obj["binding"], context=f"{context}.binding"),
        lane=_identifier(obj["lane"], context=f"{context}.lane"),
        label=_text(obj["label"], context=f"{context}.label", maximum=48),
        detail=_text_tuple(
            obj["detail"],
            context=f"{context}.detail",
            maximum=MAX_DETAIL_LINES,
        ),
        x=_integer(obj["x"], context=f"{context}.x"),
        y=_integer(obj["y"], context=f"{context}.y"),
        width=_integer(obj["width"], context=f"{context}.width", minimum=150),
        height=_integer(obj["height"], context=f"{context}.height", minimum=88),
        variant=variant,
    )
    if node.x + node.width > canvas.width or node.y + node.height > canvas.height:
        _fail(f"{context} exceeds the canvas")
    return node


def _parse_edge(value: object, index: int, canvas: Canvas) -> Edge:
    context = f"edges[{index}]"
    obj = _object(
        value,
        frozenset(
            {
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
        ),
        context,
    )
    style = _text(obj["style"], context=f"{context}.style", maximum=16)
    if style not in _EDGE_STYLES:
        _fail(f"{context} uses an unsupported edge style")
    points_raw = _array(
        obj["points"],
        context=f"{context}.points",
        maximum=MAX_POINTS,
        minimum=2,
    )
    points: list[tuple[int, int]] = []
    for point_index, point_raw in enumerate(points_raw):
        point = _array(
            point_raw,
            context=f"{context}.points[{point_index}]",
            maximum=2,
            minimum=2,
        )
        points.append(
            (
                _integer(point[0], context=f"{context}.points[{point_index}][0]"),
                _integer(point[1], context=f"{context}.points[{point_index}][1]"),
            )
        )
    if any(x > canvas.width or y > canvas.height for x, y in points):
        _fail(f"{context} has a point outside the canvas")
    edge = Edge(
        identifier=_identifier(obj["id"], context=f"{context}.id"),
        binding=_identifier(obj["binding"], context=f"{context}.binding"),
        source=_identifier(obj["from"], context=f"{context}.from"),
        target=_identifier(obj["to"], context=f"{context}.to"),
        label=_text(obj["label"], context=f"{context}.label", maximum=58),
        label_x=_integer(obj["label_x"], context=f"{context}.label_x"),
        label_y=_integer(obj["label_y"], context=f"{context}.label_y"),
        style=style,
        points=tuple(points),
    )
    label_width = _edge_label_width(edge.label)
    if (
        edge.label_x - label_width // 2 < 0
        or edge.label_x + (label_width - label_width // 2) > canvas.width
        or edge.label_y - 17 < 0
        or edge.label_y + 9 > canvas.height
    ):
        _fail(f"{context} label exceeds the canvas")
    return edge


def _parse_note(value: object, index: int, canvas: Canvas) -> Note:
    context = f"notes[{index}]"
    obj = _object(
        value,
        frozenset({"binding", "id", "label", "text", "variant", "width", "x", "y"}),
        context,
    )
    variant = _text(obj["variant"], context=f"{context}.variant", maximum=16)
    if variant not in _NODE_VARIANTS:
        _fail(f"{context} uses an unsupported visual variant")
    note = Note(
        identifier=_identifier(obj["id"], context=f"{context}.id"),
        binding=_identifier(obj["binding"], context=f"{context}.binding"),
        label=_text(obj["label"], context=f"{context}.label", maximum=48),
        text=_text_tuple(obj["text"], context=f"{context}.text", maximum=4),
        x=_integer(obj["x"], context=f"{context}.x"),
        y=_integer(obj["y"], context=f"{context}.y"),
        width=_integer(obj["width"], context=f"{context}.width", minimum=180),
        variant=variant,
    )
    note_height = 55 + len(note.text) * 20
    if note.x + note.width > canvas.width or note.y + note_height > canvas.height:
        _fail(f"{context} exceeds the canvas")
    return note


def _unique_identifiers(values: Sequence[str], context: str) -> frozenset[str]:
    result = frozenset(values)
    if len(result) != len(values):
        _fail(f"{context} contains duplicate identifiers")
    return result


def _conservative_text_width(
    value: str,
    font_size: int,
    *,
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
    return em_width * font_size * TEXT_WIDTH_SAFETY_FACTOR + spacing


def _require_text_fit(
    value: str,
    available_width: int,
    font_size: int,
    context: str,
    *,
    letter_spacing: float = 0,
) -> None:
    if (
        _conservative_text_width(
            value,
            font_size,
            letter_spacing=letter_spacing,
        )
        > available_width
    ):
        _fail(f"{context} exceeds its conservative text width")


def _edge_label_width(label: str) -> int:
    measured = _conservative_text_width(label, EDGE_LABEL_FONT_SIZE)
    return max(116, ceil(measured) + 24)


def _node_rectangle(node: Node) -> Rectangle:
    return Rectangle(node.x, node.y, node.width, node.height)


def _note_rectangle(note: Note) -> Rectangle:
    return Rectangle(note.x, note.y, note.width, 55 + len(note.text) * 20)


def _edge_label_rectangle(edge: Edge) -> Rectangle:
    width = _edge_label_width(edge.label)
    return Rectangle(edge.label_x - width // 2, edge.label_y - 17, width, 26)


def _lane_label_rectangle(lane: Lane) -> Rectangle:
    width = ceil(
        _conservative_text_width(
            lane.label,
            LANE_FONT_SIZE,
            letter_spacing=LANE_LETTER_SPACING,
        )
    )
    return Rectangle(lane.x + 20, lane.y + 14, width + 8, 27)


def _rectangles_overlap(first: Rectangle, second: Rectangle) -> bool:
    return (
        first.x < second.x + second.width
        and second.x < first.x + first.width
        and first.y < second.y + second.height
        and second.y < first.y + first.height
    )


def _segment_crosses_rectangle(
    start: tuple[int, int],
    end: tuple[int, int],
    rectangle: Rectangle,
) -> bool:
    start_x, start_y = start
    end_x, end_y = end
    if start_x == end_x:
        return rectangle.x < start_x < rectangle.x + rectangle.width and max(
            min(start_y, end_y), rectangle.y
        ) < min(max(start_y, end_y), rectangle.y + rectangle.height)
    if start_y == end_y:
        return rectangle.y < start_y < rectangle.y + rectangle.height and max(
            min(start_x, end_x), rectangle.x
        ) < min(max(start_x, end_x), rectangle.x + rectangle.width)
    _fail("edge paths must use orthogonal segments")


def _segment_touches_rectangle(
    start: tuple[int, int],
    end: tuple[int, int],
    rectangle: Rectangle,
) -> bool:
    start_x, start_y = start
    end_x, end_y = end
    if start_x == end_x:
        return rectangle.x <= start_x <= rectangle.x + rectangle.width and max(
            min(start_y, end_y), rectangle.y
        ) <= min(max(start_y, end_y), rectangle.y + rectangle.height)
    if start_y == end_y:
        return rectangle.y <= start_y <= rectangle.y + rectangle.height and max(
            min(start_x, end_x), rectangle.x
        ) <= min(max(start_x, end_x), rectangle.x + rectangle.width)
    _fail("edge paths must use orthogonal segments")


def _linear_rgb_component(component: int) -> float:
    value = component / 255
    if value <= SRGB_LINEAR_THRESHOLD:
        return value / SRGB_LINEAR_DIVISOR
    return float(((value + SRGB_OFFSET) / SRGB_SCALE) ** SRGB_GAMMA)


def _relative_luminance(color: str) -> float:
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color) is None:
        _fail("renderer palette contains an invalid color")
    red, green, blue = (int(color[index : index + 2], 16) for index in (1, 3, 5))
    return (
        0.2126 * _linear_rgb_component(red)
        + 0.7152 * _linear_rgb_component(green)
        + 0.0722 * _linear_rgb_component(blue)
    )


def contrast_ratio(first: str, second: str) -> float:
    """Return the WCAG contrast ratio for two six-digit sRGB colors."""

    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _validate_text_layout(diagram: Diagram) -> None:
    header_width = diagram.canvas.width - 96
    _require_text_fit(diagram.title, header_width, TITLE_FONT_SIZE, "title")
    _require_text_fit(diagram.subtitle, header_width, SUBTITLE_FONT_SIZE, "subtitle")
    footer = f"{diagram.caption} · Source: {diagram.source_name}"
    _require_text_fit(footer, header_width, CAPTION_FONT_SIZE, "caption")

    for lane in diagram.lanes:
        _require_text_fit(
            lane.label,
            lane.width - 48,
            LANE_FONT_SIZE,
            f"lane {lane.identifier} label",
            letter_spacing=LANE_LETTER_SPACING,
        )
    for node in diagram.nodes:
        _require_text_fit(
            node.label,
            node.width - 44,
            NODE_TITLE_FONT_SIZE,
            f"node {node.identifier} title",
        )
        for index, detail in enumerate(node.detail):
            _require_text_fit(
                detail,
                node.width - 44,
                DETAIL_FONT_SIZE,
                f"node {node.identifier} detail {index}",
            )
        required_height = 62 + (len(node.detail) - 1) * 21 + DETAIL_FONT_SIZE + 5
        if required_height > node.height:
            _fail(f"node {node.identifier} text exceeds its height")
    for note in diagram.notes:
        _require_text_fit(
            note.label,
            note.width - 40,
            NODE_TITLE_FONT_SIZE,
            f"note {note.identifier} title",
        )
        for index, detail in enumerate(note.text):
            _require_text_fit(
                detail,
                note.width - 40,
                DETAIL_FONT_SIZE,
                f"note {note.identifier} detail {index}",
            )


def _validate_node_containment(diagram: Diagram, lanes: dict[str, Lane]) -> None:
    for node in diagram.nodes:
        lane = lanes[node.lane]
        rectangle = _node_rectangle(node)
        if (
            rectangle.x < lane.x
            or rectangle.y < lane.y + 54
            or rectangle.x + rectangle.width > lane.x + lane.width
            or rectangle.y + rectangle.height > lane.y + lane.height
        ):
            _fail(f"node {node.identifier} exceeds its assigned lane")


def _validate_lane_content_bounds(diagram: Diagram) -> None:
    content_bottom = diagram.canvas.height - DIAGRAM_FOOTER_HEIGHT
    for lane in diagram.lanes:
        if lane.y < DIAGRAM_CONTENT_TOP or lane.y + lane.height > content_bottom:
            _fail(f"lane {lane.identifier} overlaps the diagram header or footer")


def _validate_box_collisions(boxes: list[tuple[str, Rectangle]]) -> None:
    for index, (identifier, rectangle) in enumerate(boxes):
        for other_identifier, other_rectangle in boxes[index + 1 :]:
            if _rectangles_overlap(rectangle, other_rectangle):
                _fail(f"{identifier} overlaps {other_identifier}")


def _validate_edge_label_collisions(
    diagram: Diagram,
    boxes: list[tuple[str, Rectangle]],
) -> None:
    label_boxes = [(edge.identifier, _edge_label_rectangle(edge)) for edge in diagram.edges]
    for edge_identifier, label_rectangle in label_boxes:
        for box_identifier, rectangle in boxes:
            if _rectangles_overlap(label_rectangle, rectangle):
                _fail(f"edge label {edge_identifier} overlaps {box_identifier}")
    for index, (edge_identifier, label_rectangle) in enumerate(label_boxes):
        for other_identifier, other_rectangle in label_boxes[index + 1 :]:
            if _rectangles_overlap(label_rectangle, other_rectangle):
                _fail(f"edge labels {edge_identifier} and {other_identifier} overlap")


def _validate_edge_routes(
    diagram: Diagram,
    boxes: list[tuple[str, Rectangle]],
) -> None:
    node_rectangles = {node.identifier: _node_rectangle(node) for node in diagram.nodes}
    edge_label_rectangles = {edge.identifier: _edge_label_rectangle(edge) for edge in diagram.edges}
    for edge in diagram.edges:
        _validate_edge_endpoint_geometry(edge, node_rectangles)
        segments = tuple(zip(edge.points, edge.points[1:], strict=False))
        for start, end in segments:
            _segment_axis(start, end)
        label_point = (edge.label_x, edge.label_y)
        distance = min(
            _point_to_orthogonal_segment_distance(label_point, start, end)
            for start, end in segments
        )
        if distance > MAX_EDGE_LABEL_ROUTE_DISTANCE:
            _fail(f"edge label {edge.identifier} is detached from its route")
        for start, end in segments:
            for box_identifier, rectangle in boxes:
                intersects = (
                    _segment_crosses_rectangle(start, end, rectangle)
                    if box_identifier in {edge.source, edge.target}
                    else _segment_touches_rectangle(start, end, rectangle)
                )
                if intersects:
                    _fail(f"edge {edge.identifier} crosses {box_identifier}")
            for other_identifier, rectangle in edge_label_rectangles.items():
                if other_identifier != edge.identifier and _segment_touches_rectangle(
                    start, end, rectangle
                ):
                    _fail(f"edge {edge.identifier} crosses edge label {other_identifier}")
        _validate_route_does_not_cross_itself(edge, segments)
    _validate_distinct_edge_routes(diagram.edges)


def _segment_axis(start: tuple[int, int], end: tuple[int, int]) -> str:
    if start == end:
        _fail("edge paths do not permit zero-length segments")
    if start[0] == end[0]:
        return "vertical"
    if start[1] == end[1]:
        return "horizontal"
    _fail("edge paths must use orthogonal segments")


def _point_on_rectangle_boundary(point: tuple[int, int], rectangle: Rectangle) -> bool:
    point_x, point_y = point
    horizontal_side = rectangle.x <= point_x <= rectangle.x + rectangle.width and point_y in {
        rectangle.y,
        rectangle.y + rectangle.height,
    }
    vertical_side = rectangle.y <= point_y <= rectangle.y + rectangle.height and point_x in {
        rectangle.x,
        rectangle.x + rectangle.width,
    }
    return horizontal_side or vertical_side


def _departs_outward(
    boundary: tuple[int, int],
    adjacent: tuple[int, int],
    rectangle: Rectangle,
) -> bool:
    boundary_x, boundary_y = boundary
    adjacent_x, adjacent_y = adjacent
    return (
        (boundary_x == rectangle.x and adjacent_x < boundary_x)
        or (boundary_x == rectangle.x + rectangle.width and adjacent_x > boundary_x)
        or (boundary_y == rectangle.y and adjacent_y < boundary_y)
        or (boundary_y == rectangle.y + rectangle.height and adjacent_y > boundary_y)
    )


def _arrives_inward(
    adjacent: tuple[int, int],
    boundary: tuple[int, int],
    rectangle: Rectangle,
) -> bool:
    adjacent_x, adjacent_y = adjacent
    boundary_x, boundary_y = boundary
    return (
        (boundary_x == rectangle.x and adjacent_x < boundary_x)
        or (boundary_x == rectangle.x + rectangle.width and adjacent_x > boundary_x)
        or (boundary_y == rectangle.y and adjacent_y < boundary_y)
        or (boundary_y == rectangle.y + rectangle.height and adjacent_y > boundary_y)
    )


def _validate_edge_endpoint_geometry(
    edge: Edge,
    node_rectangles: dict[str, Rectangle],
) -> None:
    source_rectangle = node_rectangles[edge.source]
    target_rectangle = node_rectangles[edge.target]
    start = edge.points[0]
    end = edge.points[-1]
    if not _point_on_rectangle_boundary(start, source_rectangle):
        _fail(f"edge {edge.identifier} does not start on its source boundary")
    if not _point_on_rectangle_boundary(end, target_rectangle):
        _fail(f"edge {edge.identifier} does not end on its target boundary")
    if not _departs_outward(start, edge.points[1], source_rectangle):
        _fail(f"edge {edge.identifier} does not depart outward from its source")
    if not _arrives_inward(edge.points[-2], end, target_rectangle):
        _fail(f"edge {edge.identifier} does not point into its target")


def _segment_intersection(  # noqa: PLR0911 - orthogonal cases stay explicit
    first_start: tuple[int, int],
    first_end: tuple[int, int],
    second_start: tuple[int, int],
    second_end: tuple[int, int],
) -> tuple[str, tuple[int, int] | None] | None:
    first_axis = _segment_axis(first_start, first_end)
    second_axis = _segment_axis(second_start, second_end)
    if first_axis == second_axis == "vertical":
        if first_start[0] != second_start[0]:
            return None
        low = max(min(first_start[1], first_end[1]), min(second_start[1], second_end[1]))
        high = min(max(first_start[1], first_end[1]), max(second_start[1], second_end[1]))
        if low > high:
            return None
        if low == high:
            return ("point", (first_start[0], low))
        return ("overlap", None)
    if first_axis == second_axis == "horizontal":
        if first_start[1] != second_start[1]:
            return None
        low = max(min(first_start[0], first_end[0]), min(second_start[0], second_end[0]))
        high = min(max(first_start[0], first_end[0]), max(second_start[0], second_end[0]))
        if low > high:
            return None
        if low == high:
            return ("point", (low, first_start[1]))
        return ("overlap", None)

    if first_axis == "vertical":
        vertical_start, vertical_end = first_start, first_end
        horizontal_start, horizontal_end = second_start, second_end
    else:
        vertical_start, vertical_end = second_start, second_end
        horizontal_start, horizontal_end = first_start, first_end
    point = (vertical_start[0], horizontal_start[1])
    if min(vertical_start[1], vertical_end[1]) <= point[1] <= max(
        vertical_start[1], vertical_end[1]
    ) and min(horizontal_start[0], horizontal_end[0]) <= point[0] <= max(
        horizontal_start[0], horizontal_end[0]
    ):
        return ("point", point)
    return None


def _route_endpoint_semantics(edge: Edge, point: tuple[int, int]) -> frozenset[str]:
    semantics: set[str] = set()
    if point == edge.points[0]:
        semantics.add(edge.source)
    if point == edge.points[-1]:
        semantics.add(edge.target)
    return frozenset(semantics)


def _shared_semantic_endpoint(first: Edge, second: Edge, point: tuple[int, int]) -> bool:
    return bool(_route_endpoint_semantics(first, point) & _route_endpoint_semantics(second, point))


def _validate_route_does_not_cross_itself(
    edge: Edge,
    segments: tuple[tuple[tuple[int, int], tuple[int, int]], ...],
) -> None:
    for index, (start, end) in enumerate(segments):
        for other_index, (other_start, other_end) in enumerate(
            segments[index + 1 :],
            start=index + 1,
        ):
            intersection = _segment_intersection(start, end, other_start, other_end)
            if other_index == index + 1 and intersection == ("point", end):
                continue
            if intersection is not None:
                _fail(f"edge {edge.identifier} crosses or overlaps itself")


def _validate_distinct_edge_routes(edges: tuple[Edge, ...]) -> None:
    for index, first in enumerate(edges):
        first_segments = tuple(zip(first.points, first.points[1:], strict=False))
        for second in edges[index + 1 :]:
            second_segments = tuple(zip(second.points, second.points[1:], strict=False))
            for first_start, first_end in first_segments:
                for second_start, second_end in second_segments:
                    intersection = _segment_intersection(
                        first_start,
                        first_end,
                        second_start,
                        second_end,
                    )
                    if intersection is None:
                        continue
                    kind, point = intersection
                    if (
                        kind == "point"
                        and point is not None
                        and _shared_semantic_endpoint(first, second, point)
                    ):
                        continue
                    _fail(
                        f"edge routes {first.identifier} and {second.identifier} "
                        "cross, touch, or overlap"
                    )


def _point_to_orthogonal_segment_distance(
    point: tuple[int, int],
    start: tuple[int, int],
    end: tuple[int, int],
) -> float:
    point_x, point_y = point
    start_x, start_y = start
    end_x, end_y = end
    if start_x == end_x:
        nearest_y = min(max(point_y, min(start_y, end_y)), max(start_y, end_y))
        return hypot(point_x - start_x, point_y - nearest_y)
    if start_y == end_y:
        nearest_x = min(max(point_x, min(start_x, end_x)), max(start_x, end_x))
        return hypot(point_x - nearest_x, point_y - start_y)
    _fail("edge paths must use orthogonal segments")


def _validate_reachability(diagram: Diagram) -> None:
    adjacency: dict[str, set[str]] = {node.identifier: set() for node in diagram.nodes}
    for edge in diagram.edges:
        adjacency[edge.source].add(edge.target)
    reachable = {diagram.entry}
    pending = [diagram.entry]
    while pending:
        source = pending.pop()
        for target in adjacency[source] - reachable:
            reachable.add(target)
            pending.append(target)
    unreachable = sorted(adjacency.keys() - reachable)
    if unreachable:
        _fail(f"diagram has unreachable nodes: {', '.join(unreachable)}")


def _validate_svg_identifiers(diagram: Diagram) -> None:
    rendered_ids = [
        *(node.identifier for node in diagram.nodes),
        *(note.identifier for note in diagram.notes),
        *(edge.identifier for edge in diagram.edges),
    ]
    _unique_identifiers(rendered_ids, "rendered SVG elements")
    reserved_ids = {
        "arrow",
        "arrow-warning",
        f"{diagram.slug}-title",
        f"{diagram.slug}-description",
    }
    collisions = sorted(reserved_ids.intersection(rendered_ids))
    if collisions:
        _fail(f"rendered SVG identifier is reserved: {', '.join(collisions)}")


def _validate_palette() -> None:
    if contrast_ratio(SECONDARY_TEXT, LANE_BACKGROUND) < MIN_TEXT_CONTRAST:
        _fail("lane label contrast is below the renderer minimum")
    if contrast_ratio(SECONDARY_TEXT, CANVAS_BACKGROUND) < MIN_TEXT_CONTRAST:
        _fail("caption contrast is below the renderer minimum")
    if contrast_ratio(MUTED_TEXT, CANVAS_BACKGROUND) < MIN_TEXT_CONTRAST:
        _fail("subtitle contrast is below the renderer minimum")
    if contrast_ratio(LANE_BORDER, LANE_BACKGROUND) < MIN_NON_TEXT_CONTRAST:
        _fail("lane boundary contrast is below the renderer minimum")
    if contrast_ratio(LANE_BORDER, CANVAS_BACKGROUND) < MIN_NON_TEXT_CONTRAST:
        _fail("lane boundary contrast against the canvas is below the renderer minimum")


def _validate_geometry(diagram: Diagram) -> None:
    lanes = {lane.identifier: lane for lane in diagram.lanes}
    lane_boxes = [
        (f"lane {lane.identifier}", Rectangle(lane.x, lane.y, lane.width, lane.height))
        for lane in diagram.lanes
    ]
    boxes: list[tuple[str, Rectangle]] = [
        ("diagram header", Rectangle(0, 0, diagram.canvas.width, DIAGRAM_CONTENT_TOP)),
        (
            "diagram footer",
            Rectangle(
                0,
                diagram.canvas.height - DIAGRAM_FOOTER_HEIGHT,
                diagram.canvas.width,
                DIAGRAM_FOOTER_HEIGHT,
            ),
        ),
        *((f"lane label {lane.identifier}", _lane_label_rectangle(lane)) for lane in diagram.lanes),
        *((node.identifier, _node_rectangle(node)) for node in diagram.nodes),
        *((note.identifier, _note_rectangle(note)) for note in diagram.notes),
    ]
    _validate_lane_content_bounds(diagram)
    _validate_box_collisions(lane_boxes)
    _validate_node_containment(diagram, lanes)
    _validate_box_collisions(boxes)
    _validate_edge_label_collisions(diagram, boxes)
    _validate_edge_routes(diagram, boxes)


def _validate_diagram(diagram: Diagram) -> None:
    _validate_palette()
    _validate_svg_identifiers(diagram)
    _validate_text_layout(diagram)
    _validate_geometry(diagram)
    _validate_reachability(diagram)


def _absolute_path_parts(path: Path, *, context: str) -> tuple[str, ...]:
    if os.name != "posix":
        _fail(f"{context} requires POSIX no-follow filesystem semantics")
    # resolve() follows links, so retain lexical normalization for the no-follow walk.
    absolute = Path(os.path.abspath(os.fspath(path)))  # noqa: PTH100
    if absolute.anchor != "/":
        _fail(f"{context} is not a canonical POSIX path")
    return absolute.parts[1:]


def _open_directory_fd(path: Path, *, create: bool, context: str) -> int:
    parts = _absolute_path_parts(path, context=context)
    current_fd = -1
    try:
        current_fd = os.open("/", _DIRECTORY_OPEN_FLAGS)
        for component in parts:
            try:
                next_fd = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o755, dir_fd=current_fd)
                next_fd = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=current_fd,
                )
            os.close(current_fd)
            current_fd = next_fd
    except FileNotFoundError:
        if current_fd >= 0:
            os.close(current_fd)
        raise _MissingVisualPathError from None
    except OSError:
        if current_fd >= 0:
            os.close(current_fd)
        _fail(f"{context} is unavailable or unsafe")
    return current_fd


def _open_regular_file_fd(
    path: Path,
    *,
    context: str,
    require_single_link: bool = False,
) -> int:
    parts = _absolute_path_parts(path, context=context)
    if not parts:
        _fail(f"{context} must name a regular file")
    parent = Path("/") / Path(*parts[:-1])
    parent_fd = _open_directory_fd(parent, create=False, context=f"{context} parent")
    file_fd = -1
    try:
        file_fd = os.open(parts[-1], _REGULAR_READ_FLAGS, dir_fd=parent_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"{context} must be a regular file")
        if require_single_link and metadata.st_nlink != 1:
            _fail(f"{context} must have exactly one filesystem link")
    except OSError:
        if file_fd >= 0:
            os.close(file_fd)
        _fail(f"{context} is unavailable or unsafe")
    except VisualSourceError:
        if file_fd >= 0:
            os.close(file_fd)
        raise
    finally:
        os.close(parent_fd)
    return file_fd


def _read_bounded_stream(stream: BinaryIO, *, maximum: int, context: str) -> bytes:
    payload = bytearray()
    while len(payload) <= maximum:
        remaining = maximum + 1 - len(payload)
        chunk = stream.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        payload.extend(chunk)
    if not payload or len(payload) > maximum:
        _fail(f"{context} byte size is outside the supported range")
    return bytes(payload)


def _read_bounded_file(path: Path, *, maximum: int, context: str) -> bytes:
    file_fd = _open_regular_file_fd(path, context=context)
    with os.fdopen(file_fd, "rb", buffering=0, closefd=True) as raw_stream:
        stream = cast(BinaryIO, raw_stream)
        return _read_bounded_stream(stream, maximum=maximum, context=context)


def _validate_binding_paths(bindings: tuple[Binding, ...]) -> None:
    for relative_path in sorted({binding.path for binding in bindings}):
        file_fd = _open_regular_file_fd(
            ROOT / relative_path,
            context=f"binding path {relative_path}",
        )
        os.close(file_fd)


def load_diagram(path: Path) -> Diagram:
    """Load and validate one closed, bounded visual source."""

    try:
        diagram, _source_size = _load_diagram_with_size(path)
    except MemoryError:
        _fail("visual source exceeded the renderer memory budget")
    return diagram


def _load_diagram_with_size(path: Path) -> tuple[Diagram, int]:
    raw = _read_bounded_file(
        path,
        maximum=MAX_SOURCE_BYTES,
        context="visual source",
    )
    return _decode_diagram(path, raw), len(raw)


def _decode_diagram(path: Path, raw: bytes) -> Diagram:
    try:
        text = raw.decode("utf-8", errors="strict")
        parsed = cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_int=_parse_integer,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            ),
        )
    except VisualSourceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("visual source is not strict bounded UTF-8 JSON")
    obj = _object(
        parsed,
        frozenset(
            {
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
        ),
        "source",
    )
    schema_version = _integer(
        obj["schema_version"],
        context="schema_version",
        minimum=1,
        maximum=1,
    )
    diagram_kind = _text(obj["diagram_kind"], context="diagram_kind", maximum=24)
    if diagram_kind not in _DIAGRAM_KINDS:
        _fail("diagram_kind is unsupported")
    canvas_obj = _object(
        obj["canvas"],
        frozenset({"height", "width"}),
        "canvas",
    )
    canvas = Canvas(
        width=_integer(
            canvas_obj["width"],
            context="canvas.width",
            minimum=MIN_CANVAS_SIZE,
        ),
        height=_integer(
            canvas_obj["height"],
            context="canvas.height",
            minimum=MIN_CANVAS_SIZE,
        ),
    )
    bindings = tuple(
        _parse_binding(value, index)
        for index, value in enumerate(
            _array(
                obj["bindings"],
                context="bindings",
                maximum=MAX_BINDINGS,
                minimum=1,
            )
        )
    )
    _validate_binding_paths(bindings)
    lanes = tuple(
        _parse_lane(value, index, canvas)
        for index, value in enumerate(
            _array(obj["lanes"], context="lanes", maximum=MAX_LANES, minimum=1)
        )
    )
    nodes = tuple(
        _parse_node(value, index, canvas)
        for index, value in enumerate(
            _array(obj["nodes"], context="nodes", maximum=MAX_NODES, minimum=2)
        )
    )
    edges = tuple(
        _parse_edge(value, index, canvas)
        for index, value in enumerate(
            _array(obj["edges"], context="edges", maximum=MAX_EDGES, minimum=1)
        )
    )
    notes = tuple(
        _parse_note(value, index, canvas)
        for index, value in enumerate(_array(obj["notes"], context="notes", maximum=MAX_NOTES))
    )

    binding_ids = _unique_identifiers(
        [binding.identifier for binding in bindings],
        "bindings",
    )
    lane_ids = _unique_identifiers([lane.identifier for lane in lanes], "lanes")
    node_ids = _unique_identifiers([node.identifier for node in nodes], "nodes")
    _unique_identifiers([edge.identifier for edge in edges], "edges")
    _unique_identifiers([note.identifier for note in notes], "notes")
    references = {
        *(lane.binding for lane in lanes),
        *(node.binding for node in nodes),
        *(edge.binding for edge in edges),
        *(note.binding for note in notes),
    }
    if references != binding_ids:
        _fail("every binding must be referenced and every reference must resolve")
    if any(node.lane not in lane_ids for node in nodes):
        _fail("every node lane must resolve")
    if any(edge.source not in node_ids or edge.target not in node_ids for edge in edges):
        _fail("every edge endpoint must resolve")
    entry = _identifier(obj["entry"], context="entry")
    if entry not in node_ids:
        _fail("diagram entry must resolve to a node")

    slug = _identifier(obj["slug"], context="slug")
    source_name_match = _SOURCE_NAME_PATTERN.fullmatch(path.name)
    if source_name_match is None or source_name_match.group(1) != slug:
        _fail("visual source filename must be canonical and match its slug")
    diagram = Diagram(
        source_name=path.name,
        schema_version=schema_version,
        diagram_kind=diagram_kind,
        slug=slug,
        entry=entry,
        title=_text(obj["title"], context="title", maximum=100),
        subtitle=_text(obj["subtitle"], context="subtitle", maximum=180),
        description=_text(obj["description"], context="description"),
        caption=_text(obj["caption"], context="caption", maximum=180),
        canvas=canvas,
        bindings=bindings,
        lanes=lanes,
        nodes=nodes,
        edges=edges,
        notes=notes,
    )
    _validate_diagram(diagram)
    return diagram


def _attribute(value: str) -> str:
    return quoteattr(value)


def _polyline_path(points: tuple[tuple[int, int], ...]) -> str:
    first, *remaining = points
    segments = [f"M {first[0]} {first[1]}"]
    segments.extend(f"L {x} {y}" for x, y in remaining)
    return " ".join(segments)


def _render_lane(lane: Lane) -> list[str]:
    return [
        (
            f'  <g class="lane" data-binding={_attribute(lane.binding)}>'
            f'<rect x="{lane.x}" y="{lane.y}" width="{lane.width}" '
            f'height="{lane.height}" rx="20"/>'
        ),
        (
            f'    <text class="lane-label" x="{lane.x + 24}" '
            f'y="{lane.y + 34}">{escape(lane.label)}</text>'
        ),
        "  </g>",
    ]


def _render_node(node: Node) -> list[str]:
    lines = [
        (
            f'  <g class="node node-{node.variant}" id={_attribute(node.identifier)} '
            f"data-binding={_attribute(node.binding)}>"
        ),
        (
            f'    <rect x="{node.x}" y="{node.y}" width="{node.width}" '
            f'height="{node.height}" rx="16"/>'
        ),
        (
            f'    <text class="node-title" x="{node.x + 22}" '
            f'y="{node.y + 34}">{escape(node.label)}</text>'
        ),
    ]
    detail_y = node.y + 62
    for index, detail in enumerate(node.detail):
        lines.append(
            f'    <text class="node-detail" x="{node.x + 22}" '
            f'y="{detail_y + index * 21}">{escape(detail)}</text>'
        )
    lines.append("  </g>")
    return lines


def _render_edge_path(edge: Edge) -> str:
    marker = "arrow-warning" if edge.style == "warning" else "arrow"
    return (
        f'  <path class="edge edge-{edge.style}" id={_attribute(edge.identifier)} '
        f"data-binding={_attribute(edge.binding)} d={_attribute(_polyline_path(edge.points))} "
        f'marker-end="url(#{marker})"/>'
    )


def _render_edge_label(edge: Edge) -> list[str]:
    label_width = _edge_label_width(edge.label)
    return [
        (
            f'  <g class="edge-label" data-edge={_attribute(edge.identifier)} '
            f"data-binding={_attribute(edge.binding)}>"
        ),
        (
            f'    <rect x="{edge.label_x - label_width // 2}" y="{edge.label_y - 17}" '
            f'width="{label_width}" height="26" rx="8"/>'
        ),
        (
            f'    <text x="{edge.label_x}" y="{edge.label_y}" text-anchor="middle">'
            f"{escape(edge.label)}</text>"
        ),
        "  </g>",
    ]


def _render_note(note: Note) -> list[str]:
    height = 55 + len(note.text) * 20
    lines = [
        (
            f'  <g class="note node-{note.variant}" id={_attribute(note.identifier)} '
            f"data-binding={_attribute(note.binding)}>"
        ),
        (f'    <rect x="{note.x}" y="{note.y}" width="{note.width}" height="{height}" rx="16"/>'),
        (
            f'    <text class="note-title" x="{note.x + 20}" '
            f'y="{note.y + 30}">{escape(note.label)}</text>'
        ),
    ]
    for index, line in enumerate(note.text):
        lines.append(
            f'    <text class="note-detail" x="{note.x + 20}" '
            f'y="{note.y + 56 + index * 20}">{escape(line)}</text>'
        )
    lines.append("  </g>")
    return lines


def _render_diagram_payload(diagram: Diagram) -> bytes:
    title_id = f"{diagram.slug}-title"
    description_id = f"{diagram.slug}-description"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {diagram.canvas.width} {diagram.canvas.height}" '
            f"data-entry={_attribute(diagram.entry)} "
            f'role="img" aria-labelledby="{title_id} {description_id}">'
        ),
        f'  <title id="{title_id}">{escape(diagram.title)}</title>',
        f'  <desc id="{description_id}">{escape(diagram.description)}</desc>',
        "  <defs>",
        (
            '    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        ),
        '      <path d="M 0 0 L 10 5 L 0 10 z" fill="#35516f"/>',
        "    </marker>",
        (
            '    <marker id="arrow-warning" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        ),
        '      <path d="M 0 0 L 10 5 L 0 10 z" fill="#b95d1a"/>',
        "    </marker>",
        "    <style>",
        (
            "      text { font-family: Inter, ui-sans-serif, system-ui, -apple-system, "
            'BlinkMacSystemFont, "Segoe UI", sans-serif; fill: #182536; }'
        ),
        "      .canvas { fill: #fbfcfe; }",
        "      .header-rule { stroke: #d8e0ea; stroke-width: 2; }",
        "      .diagram-title { font-size: 34px; font-weight: 750; letter-spacing: -0.6px; }",
        f"      .diagram-subtitle {{ font-size: 17px; fill: {MUTED_TEXT}; }}",
        (
            f"      .lane rect {{ fill: {LANE_BACKGROUND}; stroke: {LANE_BORDER}; "
            "stroke-width: 1.5; }"
        ),
        (
            "      .lane-label { font-size: 13px; font-weight: 750; letter-spacing: "
            f"1.4px; fill: {SECONDARY_TEXT}; }}"
        ),
        "      .node rect, .note rect { stroke-width: 2; }",
        "      .node-primary rect { fill: #eaf2ff; stroke: #2f66b3; }",
        "      .node-strong rect { fill: #dbe9ff; stroke: #214f8c; }",
        "      .node-accent rect { fill: #fff4d8; stroke: #ae7617; }",
        "      .node-neutral rect { fill: #f5f7fa; stroke: #66768a; }",
        "      .node-storage rect { fill: #eef1f5; stroke: #455468; }",
        "      .node-output rect { fill: #f3f8ff; stroke: #5b7fb3; }",
        "      .node-warning rect { fill: #fff0e4; stroke: #b95d1a; }",
        "      .node-title, .note-title { font-size: 17px; font-weight: 750; }",
        "      .node-detail, .note-detail { font-size: 13px; fill: #405166; }",
        (
            "      .edge { fill: none; stroke: #35516f; stroke-width: 2.2; "
            "stroke-linejoin: round; stroke-linecap: round; }"
        ),
        "      .edge-dashed { stroke-dasharray: 8 7; }",
        "      .edge-warning { stroke: #b95d1a; stroke-dasharray: 5 5; }",
        "      .edge-label rect { fill: #fbfcfe; stroke: #dce3ec; stroke-width: 1; }",
        "      .edge-label text { font-size: 12px; font-weight: 650; fill: #405166; }",
        f"      .caption {{ font-size: 12px; fill: {SECONDARY_TEXT}; }}",
        "    </style>",
        "  </defs>",
        (
            f'  <rect class="canvas" x="0" y="0" width="{diagram.canvas.width}" '
            f'height="{diagram.canvas.height}"/>'
        ),
        f'  <text class="diagram-title" x="48" y="58">{escape(diagram.title)}</text>',
        f'  <text class="diagram-subtitle" x="48" y="91">{escape(diagram.subtitle)}</text>',
        (
            f'  <line class="header-rule" x1="48" y1="116" '
            f'x2="{diagram.canvas.width - 48}" y2="116"/>'
        ),
    ]
    for lane in diagram.lanes:
        lines.extend(_render_lane(lane))
    lines.extend(_render_edge_path(edge) for edge in diagram.edges)
    for node in diagram.nodes:
        lines.extend(_render_node(node))
    for edge in diagram.edges:
        lines.extend(_render_edge_label(edge))
    for note in diagram.notes:
        lines.extend(_render_note(note))
    footer = f"{diagram.caption} · Source: {diagram.source_name}"
    lines.append(
        f'  <text class="caption" x="48" y="{diagram.canvas.height - 25}">{escape(footer)}</text>'
    )
    lines.append("</svg>")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    if len(payload) > MAX_RENDERED_SVG_BYTES:
        _fail("rendered SVG exceeds the per-artifact byte budget")
    return payload


def render_diagram(diagram: Diagram) -> bytes:
    """Render one validated diagram into stable standalone SVG bytes."""

    try:
        return _render_diagram_payload(diagram)
    except MemoryError:
        _fail("SVG rendering exceeded the memory budget")


def _bounded_source_entries(directory_fd: int) -> tuple[_SourceEntry, ...]:
    source_entries: list[_SourceEntry] = []
    with os.scandir(directory_fd) as iterator:
        for entry_count, directory_entry in enumerate(iterator, start=1):
            if entry_count > MAX_DIRECTORY_ENTRIES:
                _fail("visual source directory entry count exceeds the supported maximum")
            name = directory_entry.name
            if not name.lower().endswith(".json"):
                continue
            if _SOURCE_NAME_PATTERN.fullmatch(name) is None:
                _fail("visual source directory contains noncanonical JSON")
            try:
                metadata = directory_entry.stat(follow_symlinks=False)
            except OSError:
                _fail("visual source is unavailable or unsafe")
            if not stat.S_ISREG(metadata.st_mode):
                _fail("visual source must be a regular file")
            source_entries.append(
                _SourceEntry(
                    name=name,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                )
            )
            if len(source_entries) > MAX_SOURCE_FILES:
                _fail("visual source count exceeds the supported maximum")
    if not source_entries:
        _fail("no versioned visual sources were found")
    return tuple(sorted(source_entries, key=lambda entry: entry.name))


def _read_source_entry(
    directory_fd: int,
    entry: _SourceEntry,
) -> bytes:
    file_fd = -1
    try:
        file_fd = os.open(entry.name, _REGULAR_READ_FLAGS, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            entry.device,
            entry.inode,
        ):
            _fail("visual source changed after discovery")
        with os.fdopen(file_fd, "rb", buffering=0, closefd=True) as raw_stream:
            file_fd = -1
            payload = _read_bounded_stream(
                cast(BinaryIO, raw_stream),
                maximum=MAX_SOURCE_BYTES,
                context="visual source",
            )
            after = os.fstat(raw_stream.fileno())
        current = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or any(getattr(before, field) != getattr(after, field) for field in stable_fields)
            or any(getattr(after, field) != getattr(current, field) for field in stable_fields)
        ):
            _fail("visual source changed during bounded read")
    except OSError:
        _fail("visual source is unavailable or unsafe")
    finally:
        if file_fd >= 0:
            os.close(file_fd)
    return payload


def render_sources(source_directory: Path = DEFAULT_SOURCE_DIRECTORY) -> dict[str, bytes]:
    """Render every versioned source in deterministic filename order."""

    directory_fd = -1
    try:
        directory_fd = _open_directory_fd(
            source_directory,
            create=False,
            context="visual source directory",
        )
        source_entries = _bounded_source_entries(directory_fd)
        outputs: dict[str, bytes] = {}
        total_source_bytes = 0
        total_rendered_bytes = 0
        for entry in source_entries:
            raw = _read_source_entry(directory_fd, entry)
            total_source_bytes += len(raw)
            if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
                _fail("visual sources exceed the aggregate byte budget")
            path = source_directory / entry.name
            diagram = _decode_diagram(path, raw)
            if diagram.slug in outputs:
                _fail("source slugs must be unique")
            payload = render_diagram(diagram)
            total_rendered_bytes += len(payload)
            if total_rendered_bytes > MAX_TOTAL_RENDERED_BYTES:
                _fail("rendered SVGs exceed the aggregate byte budget")
            outputs[diagram.slug] = payload
    except MemoryError:
        _fail("visual rendering exceeded the memory budget")
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
    return outputs


def _validate_output_budget(outputs: dict[str, bytes]) -> None:
    total = 0
    for slug, payload in outputs.items():
        if _ID_PATTERN.fullmatch(slug) is None:
            _fail("output slug is not canonical")
        if len(payload) > MAX_RENDERED_SVG_BYTES:
            _fail("rendered SVG exceeds the per-artifact byte budget")
        total += len(payload)
        if total > MAX_TOTAL_RENDERED_BYTES:
            _fail("rendered SVGs exceed the aggregate byte budget")


def _replaceable_target_identity(directory_fd: int, name: str) -> tuple[int, int] | None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _fail("rendered output target is unavailable or unsafe")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("rendered output target must be a regular file")
    if metadata.st_nlink != 1:
        _fail("rendered output target must have exactly one filesystem link")
    return (metadata.st_dev, metadata.st_ino)


def _write_all(file_fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(file_fd, payload[offset:])
        if written <= 0:
            _fail("rendered output write made no progress")
        offset += written


def _atomic_write_output(
    directory_fd: int,
    *,
    name: str,
    payload: bytes,
) -> None:
    original_identity = _replaceable_target_identity(directory_fd, name)
    temporary_name = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    temporary_fd = -1
    temporary_exists = False
    try:
        temporary_fd = os.open(
            temporary_name,
            _REGULAR_WRITE_FLAGS,
            0o644,
            dir_fd=directory_fd,
        )
        temporary_exists = True
        metadata = os.fstat(temporary_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            _fail("rendered output temporary must be a single-link regular file")
        os.fchmod(temporary_fd, 0o644)
        _write_all(temporary_fd, payload)
        os.fsync(temporary_fd)
        written_metadata = os.fstat(temporary_fd)
        if not stat.S_ISREG(written_metadata.st_mode) or written_metadata.st_nlink != 1:
            _fail("rendered output temporary changed during write")
        temporary_identity = (written_metadata.st_dev, written_metadata.st_ino)
        os.close(temporary_fd)
        temporary_fd = -1

        current_identity = _replaceable_target_identity(directory_fd, name)
        if current_identity != original_identity:
            _fail("rendered output target changed during atomic write")
        named_temporary = os.stat(
            temporary_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(named_temporary.st_mode)
            or named_temporary.st_nlink != 1
            or (named_temporary.st_dev, named_temporary.st_ino) != temporary_identity
        ):
            _fail("rendered output temporary changed before atomic replacement")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_exists = False
        installed_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(installed_metadata.st_mode)
            or installed_metadata.st_nlink != 1
            or (installed_metadata.st_dev, installed_metadata.st_ino) != temporary_identity
        ):
            _fail("rendered output target changed during atomic replacement")
        os.fsync(directory_fd)
    except OSError:
        _fail("rendered output could not be written safely")
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)


def _write_outputs(outputs: dict[str, bytes], output_directory: Path) -> None:
    _validate_output_budget(outputs)
    directory_fd = _open_directory_fd(
        output_directory,
        create=True,
        context="rendered output directory",
    )
    try:
        for slug, payload in sorted(outputs.items()):
            _atomic_write_output(
                directory_fd,
                name=f"{slug}.svg",
                payload=payload,
            )
    finally:
        os.close(directory_fd)


def _bounded_stream_matches(stream: BinaryIO, expected: bytes) -> bool:
    offset = 0
    while offset < len(expected):
        requested = min(READ_CHUNK_BYTES, len(expected) - offset)
        chunk = stream.read(requested)
        if not chunk or chunk != expected[offset : offset + len(chunk)]:
            return False
        offset += len(chunk)
    return stream.read(1) == b""


def _bounded_file_matches(path: Path, expected: bytes) -> bool:
    try:
        file_fd = _open_regular_file_fd(
            path,
            context="rendered output",
            require_single_link=True,
        )
    except VisualSourceError:
        return False
    with os.fdopen(file_fd, "rb", buffering=0, closefd=True) as raw_stream:
        return _bounded_stream_matches(cast(BinaryIO, raw_stream), expected)


def _bounded_output_matches(
    directory_fd: int,
    *,
    name: str,
    expected: bytes,
) -> bool:
    file_fd = -1
    try:
        file_fd = os.open(name, _REGULAR_READ_FLAGS, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return False
        with os.fdopen(file_fd, "rb", buffering=0, closefd=True) as raw_stream:
            file_fd = -1
            matches = _bounded_stream_matches(cast(BinaryIO, raw_stream), expected)
            after = os.fstat(raw_stream.fileno())
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        return (
            matches
            and stat.S_ISREG(after.st_mode)
            and after.st_nlink == 1
            and stat.S_ISREG(current.st_mode)
            and current.st_nlink == 1
            and all(getattr(before, field) == getattr(after, field) for field in stable_fields)
            and all(getattr(after, field) == getattr(current, field) for field in stable_fields)
        )
    except OSError:
        return False
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def _bounded_svg_names(directory_fd: int, *, maximum: int) -> frozenset[str] | None:
    names: set[str] = set()
    with os.scandir(directory_fd) as iterator:
        for entry_count, directory_entry in enumerate(iterator, start=1):
            if entry_count > MAX_DIRECTORY_ENTRIES:
                return None
            name = directory_entry.name
            if not name.lower().endswith(".svg"):
                continue
            names.add(name)
            if len(names) > maximum:
                return None
    return frozenset(names)


def _check_outputs(outputs: dict[str, bytes], output_directory: Path) -> bool:
    _validate_output_budget(outputs)
    try:
        directory_fd = _open_directory_fd(
            output_directory,
            create=False,
            context="rendered output directory",
        )
    except _MissingVisualPathError:
        return False
    try:
        expected_names = frozenset(f"{slug}.svg" for slug in outputs)
        allowed_names = (
            expected_names | COLOCATED_SVG_NAMES
            if output_directory == DEFAULT_OUTPUT_DIRECTORY
            else expected_names
        )
        actual_names = _bounded_svg_names(directory_fd, maximum=len(allowed_names))
        if actual_names != allowed_names:
            return False
        matches = all(
            _bounded_output_matches(
                directory_fd,
                name=f"{slug}.svg",
                expected=payload,
            )
            for slug, payload in outputs.items()
        )
        final_names = _bounded_svg_names(directory_fd, maximum=len(allowed_names))
        return matches and final_names == allowed_names
    finally:
        os.close(directory_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render deterministic source-bound RecallLedger SVG diagrams."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="write every rendered SVG")
    action.add_argument("--check", action="store_true", help="check committed SVG freshness")
    action.add_argument("--stdout", metavar="SLUG", help="write one rendered SVG to stdout")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIRECTORY,
        help="versioned JSON source directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help="SVG output directory",
    )
    return parser


def _run(namespace: argparse.Namespace) -> int:
    outputs = render_sources(cast(Path, namespace.source_dir))
    if cast(bool, namespace.write):
        _write_outputs(outputs, cast(Path, namespace.output_dir))
        return 0
    if cast(bool, namespace.check):
        return 0 if _check_outputs(outputs, cast(Path, namespace.output_dir)) else 1
    slug = cast(str, namespace.stdout)
    payload = outputs.get(slug)
    if payload is None:
        return 2
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the repository-local visual renderer."""

    try:
        namespace = _parser().parse_args(argv)
        return _run(namespace)
    except (MemoryError, OSError, VisualSourceError):
        sys.stderr.write("visual rendering failed\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
