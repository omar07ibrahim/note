#!/usr/bin/env python3
"""Render deterministic SVG evidence from the frozen lexical evaluation suite."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, cast
from xml.sax.saxutils import escape, quoteattr

ROOT: Final = Path(__file__).resolve().parents[1]
if os.fspath(ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(ROOT))

from tools import lexical_eval_contract as contract  # noqa: E402

LEXICAL_CONTRACT_VERSION: Final[int] = contract.LEXICAL_CONTRACT_VERSION  # type: ignore[attr-defined]

SUITE_DIRECTORY: Final = ROOT / "evals" / "lexical-v1"
OUTPUT_DIRECTORY: Final = ROOT / "docs" / "visuals"
OUTPUT_NAMES: Final = (
    "lexical-search-eval-summary.svg",
    "lexical-search-query-matrix.svg",
    "lexical-search-ranking-breakdown.svg",
)
STDOUT_CHOICES: Final = {
    "summary": OUTPUT_NAMES[0],
    "matrix": OUTPUT_NAMES[1],
    "breakdown": OUTPUT_NAMES[2],
}

MAX_SOURCE_BYTES: Final = contract.MAX_JSON_BYTES
MAX_SVG_BYTES: Final = 524_288
READ_CHUNK_BYTES: Final = 8_192
MIN_TEXT_CONTRAST: Final = 4.75
MIN_NON_TEXT_CONTRAST: Final = 3.0
SRGB_LINEAR_THRESHOLD: Final = 0.04045
SRGB_LINEAR_DIVISOR: Final = 12.92
SRGB_OFFSET: Final = 0.055
SRGB_SCALE: Final = 1.055
SRGB_GAMMA: Final = 2.4
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)

TEXT: Final = "#182536"
SECONDARY: Final = "#405166"
MUTED: Final = "#526276"
BACKGROUND: Final = "#f7f9fc"
CARD: Final = "#ffffff"
BORDER: Final = "#66768a"
GOLD: Final = "#7a5200"
MISS: Final = "#9c4813"
MISS_BACKGROUND: Final = "#fff0e4"
RANK_COLORS: Final = {
    1: ("#dbe9ff", "#214f8c"),
    2: ("#eaf2ff", "#2f66b3"),
    3: ("#f3f8ff", "#5b7fb3"),
    4: ("#fbfcfe", "#66768a"),
}
BREAKDOWN_COLORS: Final = {
    "title": "#245b93",
    "tag": "#6b4ea0",
    "body": "#23734d",
}


class LexicalEvalRenderError(ValueError):
    """The frozen suite or one renderer output violated the closed contract."""


@dataclass(frozen=True, slots=True)
class DocumentView:
    """Document fields required by the visual projection."""

    document_id: str
    title: str


@dataclass(frozen=True, slots=True)
class QueryView:
    """Query text and complete graded judgments in source order."""

    query_id: str
    text: str
    grades: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HitView:
    """One frozen production hit and its score components."""

    document_id: str
    rank: int
    total: int
    title: int
    tag: int
    body: int
    title_phrase: bool
    tag_phrase: bool
    body_phrase: bool


@dataclass(frozen=True, slots=True)
class OutcomeView:
    """One query's frozen compiled terms and ranked hits."""

    query_id: str
    terms: tuple[str, ...]
    hits: tuple[HitView, ...]


@dataclass(frozen=True, slots=True)
class EvalSnapshot:
    """One immutable projection of an exactly validated frozen suite."""

    summary: contract.EvaluationSummary
    documents: tuple[DocumentView, ...]
    queries: tuple[QueryView, ...]
    outcomes: tuple[OutcomeView, ...]


def _fail(message: str) -> NoReturn:
    raise LexicalEvalRenderError(message)


def _object(value: object, context: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail(f"{context} unexpectedly stopped being an object")
    return cast(dict[str, object], value)


def _objects(value: object, context: str) -> list[dict[str, object]]:
    if type(value) is not list or any(type(item) is not dict for item in value):
        _fail(f"{context} unexpectedly stopped being an object array")
    return cast(list[dict[str, object]], value)


def _text(value: object, context: str) -> str:
    if type(value) is not str:
        _fail(f"{context} unexpectedly stopped being text")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        _fail(f"{context} unexpectedly stopped being an integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        _fail(f"{context} unexpectedly stopped being a boolean")
    return value


def _relative_luminance(color: str) -> float:
    channels = tuple(int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))
    linear = tuple(
        channel / SRGB_LINEAR_DIVISOR
        if channel <= SRGB_LINEAR_THRESHOLD
        else ((channel + SRGB_OFFSET) / SRGB_SCALE) ** SRGB_GAMMA
        for channel in channels
    )
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG relative-luminance contrast ratio for two hex colors."""

    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _validate_palette() -> None:
    text_pairs = (
        (TEXT, BACKGROUND),
        (TEXT, CARD),
        (SECONDARY, BACKGROUND),
        (MUTED, BACKGROUND),
        (MISS, MISS_BACKGROUND),
        ("#ffffff", GOLD),
        *((TEXT, fill) for fill, _stroke in RANK_COLORS.values()),
        *(("#ffffff", color) for color in BREAKDOWN_COLORS.values()),
    )
    if any(
        contrast_ratio(foreground, background) < MIN_TEXT_CONTRAST
        for foreground, background in text_pairs
    ):
        _fail("lexical visual text contrast is below the renderer minimum")
    non_text_pairs = (
        (BORDER, CARD),
        (BORDER, BACKGROUND),
        (MISS, MISS_BACKGROUND),
        *((stroke, fill) for fill, stroke in RANK_COLORS.values()),
        *((GOLD, fill) for fill, _stroke in RANK_COLORS.values()),
    )
    if any(
        contrast_ratio(foreground, background) < MIN_NON_TEXT_CONTRAST
        for foreground, background in non_text_pairs
    ):
        _fail("lexical visual boundary contrast is below the renderer minimum")


def _absolute_path_parts(path: Path, *, context: str) -> tuple[str, ...]:
    if os.name != "posix":
        _fail(f"{context} requires POSIX no-follow filesystem semantics")
    absolute = Path(os.path.abspath(os.fspath(path)))  # noqa: PTH100
    if absolute.anchor != "/":
        _fail(f"{context} is not a canonical POSIX path")
    return absolute.parts[1:]


def _open_directory_fd(path: Path, *, context: str) -> int:
    descriptor = -1
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        for component in _absolute_path_parts(path, context=context):
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        _fail(f"{context} is unavailable or unsafe")
    return descriptor


def _same_file_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(left, field) == getattr(right, field) for field in stable_fields)


def _read_regular_at(
    directory_fd: int,
    filename: str,
    *,
    limit: int,
    context: str,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(filename, _READ_FLAGS, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= limit
        ):
            _fail(f"{context} must be one bounded single-link regular file")
        payload = bytearray()
        while len(payload) <= limit:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, limit + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(payload) != before.st_size
            or len(payload) > limit
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or not _same_file_metadata(before, after)
            or not _same_file_metadata(after, current)
        ):
            _fail(f"{context} changed while it was read or exceeded its bound")
        return bytes(payload)
    except LexicalEvalRenderError:
        raise
    except OSError:
        _fail(f"{context} is unavailable or unsafe")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_regular_file(path: Path, *, limit: int, context: str) -> bytes:
    """Read one stable regular file through a component-wise no-follow walk."""

    parts = _absolute_path_parts(path, context=context)
    if not parts:
        _fail(f"{context} must name a regular file")
    directory = Path("/") / Path(*parts[:-1])
    directory_fd = _open_directory_fd(directory, context=f"{context} parent directory")
    try:
        return _read_regular_at(directory_fd, parts[-1], limit=limit, context=context)
    finally:
        os.close(directory_fd)


def _source_object(
    directory_fd: int,
    filename: str,
    expected_digest: str,
) -> dict[str, object]:
    raw = _read_regular_at(
        directory_fd,
        filename,
        limit=MAX_SOURCE_BYTES,
        context=f"lexical evaluation source {filename}",
    )
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        _fail(f"{filename} changed after suite validation")
    return cast(
        dict[str, object],
        contract.decode_canonical_document(raw, context=filename),
    )


def load_snapshot(directory: Path = SUITE_DIRECTORY) -> EvalSnapshot:
    """Validate the suite exactly once, then bind a snapshot to those exact digests."""

    directory_fd = _open_directory_fd(directory, context="lexical evaluation suite directory")
    try:
        validator_directory = Path(f"/proc/self/fd/{directory_fd}")
        summary = contract.validate_suite(validator_directory)
        corpus = _source_object(directory_fd, contract.CORPUS_FILE, summary.corpus_sha256)
        queries_source = _source_object(directory_fd, contract.QUERIES_FILE, summary.queries_sha256)
        expected = _source_object(directory_fd, contract.EXPECTED_FILE, summary.expected_sha256)
    finally:
        os.close(directory_fd)

    documents: list[DocumentView] = []
    for row in _objects(corpus.get("documents"), "corpus documents"):
        content = _object(row.get("content"), "document content")
        documents.append(
            DocumentView(
                document_id=_text(row.get("document_id"), "document identifier"),
                title=_text(content.get("title"), "document title"),
            )
        )

    query_views: list[QueryView] = []
    for row in _objects(queries_source.get("queries"), "query cases"):
        judgments = _objects(row.get("judgments"), "query judgments")
        query_views.append(
            QueryView(
                query_id=_text(row.get("query_id"), "query identifier"),
                text=_text(row.get("text"), "query text"),
                grades=tuple(
                    _integer(judgment.get("grade"), "judgment grade") for judgment in judgments
                ),
            )
        )

    outcomes: list[OutcomeView] = []
    for row in _objects(expected.get("outcomes"), "expected outcomes"):
        hits: list[HitView] = []
        for hit in _objects(row.get("expected_hits"), "expected hits"):
            score = _object(hit.get("score"), "expected score")
            hits.append(
                HitView(
                    document_id=_text(hit.get("document_id"), "hit document identifier"),
                    rank=_integer(hit.get("rank"), "hit rank"),
                    total=_integer(score.get("total"), "score total"),
                    title=_integer(score.get("title_term_frequency"), "title frequency"),
                    tag=_integer(score.get("tag_term_frequency"), "tag frequency"),
                    body=_integer(score.get("body_term_frequency"), "body frequency"),
                    title_phrase=_boolean(score.get("title_phrase"), "title phrase flag"),
                    tag_phrase=_boolean(score.get("tag_phrase"), "tag phrase flag"),
                    body_phrase=_boolean(score.get("body_phrase"), "body phrase flag"),
                )
            )
        terms = row.get("expected_terms")
        if type(terms) is not list or any(type(term) is not str for term in terms):
            _fail("expected terms unexpectedly stopped being a text array")
        outcomes.append(
            OutcomeView(
                query_id=_text(row.get("query_id"), "outcome query identifier"),
                terms=tuple(cast(list[str], terms)),
                hits=tuple(hits),
            )
        )

    snapshot = EvalSnapshot(summary, tuple(documents), tuple(query_views), tuple(outcomes))
    if (
        len(snapshot.documents) != summary.document_count
        or len(snapshot.queries) != summary.query_count
        or tuple(query.query_id for query in snapshot.queries)
        != tuple(outcome.query_id for outcome in snapshot.outcomes)
        or any(len(query.grades) != len(snapshot.documents) for query in snapshot.queries)
    ):
        _fail("validated suite projection has inconsistent dimensions")
    return snapshot


def _root_open(snapshot: EvalSnapshot, *, width: int, height: int, slug: str) -> list[str]:
    _validate_palette()
    summary = snapshot.summary
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" '
            f'aria-labelledby="{slug}-title {slug}-description" '
            f"data-suite-id={quoteattr(contract.SUITE_ID)} "
            f"data-corpus-sha256={quoteattr(summary.corpus_sha256)} "
            f"data-queries-sha256={quoteattr(summary.queries_sha256)} "
            f"data-expected-sha256={quoteattr(summary.expected_sha256)} "
            'data-unicode-profile-policy="runtime-bound">'
        ),
    ]


def _common_style() -> list[str]:
    return [
        "  <style>",
        f"    .title {{ fill: {TEXT}; font: 700 34px system-ui, sans-serif; }}",
        f"    .subtitle {{ fill: {SECONDARY}; font: 500 17px system-ui, sans-serif; }}",
        f"    .body {{ fill: {TEXT}; font: 500 15px system-ui, sans-serif; }}",
        f"    .small {{ fill: {MUTED}; font: 500 13px system-ui, sans-serif; }}",
        f"    .mono {{ fill: {MUTED}; font: 500 12px ui-monospace, monospace; }}",
        "  </style>",
    ]


def _provenance_footer(snapshot: EvalSnapshot, *, y: int) -> list[str]:
    summary = snapshot.summary
    lines = (
        "sources: evals/lexical-v1/{corpus.v1.json, queries.v1.json, expected.v1.json}"
        " · renderer: tools/render_lexical_eval_visuals.py",
        f"corpus sha256  {summary.corpus_sha256}",
        f"queries sha256 {summary.queries_sha256}",
        f"expected sha256 {summary.expected_sha256}",
        f"lexical contract v{LEXICAL_CONTRACT_VERSION} · runtime-bound Unicode profile",
    )
    return [
        f'  <text class="mono" x="60" y="{y + index * 22}">{escape(line)}</text>'
        for index, line in enumerate(lines)
    ]


def _metric(snapshot: EvalSnapshot, name: str) -> dict[str, object]:
    value = snapshot.summary.metrics.get(name)
    if type(value) is not dict:
        _fail(f"validated metric {name} unexpectedly changed type")
    return cast(dict[str, object], value)


def _percent(ppm: int) -> str:
    tenths = (ppm + 500) // 1_000
    return f"{tenths // 10}.{tenths % 10}%"


def render_summary(snapshot: EvalSnapshot) -> bytes:
    """Render equal-footprint KPI cards without incomparable magnitude bars."""

    width, height = 2_000, 900
    slug = "lexical-search-eval-summary"
    body = _root_open(snapshot, width=width, height=height, slug=slug)
    body.extend(
        [
            f'  <title id="{slug}-title">Frozen lexical evaluation scorecard</title>',
            (
                f'  <desc id="{slug}-description">Seven exact KPI cards from the validated '
                "twelve-query synthetic conformance suite, including one disclosed semantic miss."
                "</desc>"
            ),
            *_common_style(),
            f'  <rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
            '  <text class="title" x="60" y="58">Frozen lexical evaluation · v1</text>',
            (
                '  <text class="subtitle" x="60" y="91">12 queries · 8 synthetic documents '
                "· exact fractions and fixed-point percentages · no cross-metric bars</text>"
            ),
            (
                '  <text class="small" x="60" y="121">A conformance scorecard, not a semantic '
                "quality, production traffic, or latency benchmark.</text>"
            ),
        ]
    )

    cards = (
        ("Exact outcomes", "exact_outcome_rate"),
        ("Success @ 1", "success_at_1"),
        ("Macro Recall @ 5", "macro_recall_at_5"),
        ("Micro Recall @ 5", "micro_recall_at_5"),
        ("MRR @ 5", "mrr_at_5"),
        ("Macro nDCG @ 5", "macro_ndcg_at_5"),
    )
    positions = (
        (60, 155),
        (530, 155),
        (1_000, 155),
        (1_470, 155),
        (60, 360),
        (530, 360),
    )
    for (label, name), (x, y) in zip(cards, positions, strict=True):
        metric = _metric(snapshot, name)
        numerator = _integer(metric.get("numerator"), f"{name} numerator")
        denominator = _integer(metric.get("denominator"), f"{name} denominator")
        ppm = _integer(metric.get("ppm"), f"{name} ppm")
        body.extend(
            [
                (
                    f'  <g class="kpi" data-metric={quoteattr(name)} '
                    f'data-numerator="{numerator}" data-denominator="{denominator}" '
                    f'data-ppm="{ppm}">'
                ),
                (
                    f'    <rect class="kpi-card" x="{x}" y="{y}" width="450" height="175" '
                    f'rx="16" fill="{CARD}" stroke="{BORDER}" stroke-width="2"/>'
                ),
                f'    <text class="body" x="{x + 26}" y="{y + 42}">{escape(label)}</text>',
                (
                    f'    <text x="{x + 26}" y="{y + 101}" fill="{TEXT}" '
                    f'font-family="system-ui, sans-serif" font-size="38" font-weight="750">'
                    f"{numerator}/{denominator}</text>"
                ),
                (
                    f'    <text x="{x + 424}" y="{y + 101}" text-anchor="end" fill="{SECONDARY}" '
                    f'font-family="system-ui, sans-serif" font-size="25" font-weight="650">'
                    f"{_percent(ppm)}</text>"
                ),
                (
                    f'    <text class="small" x="{x + 26}" y="{y + 142}">'
                    "exact denominator shown</text>"
                ),
                "  </g>",
            ]
        )

    unexpected = _metric(snapshot, "unexpected_hit_count")
    count = _integer(unexpected.get("count"), "unexpected hit count")
    body.extend(
        [
            f'  <g class="kpi" data-metric="unexpected_hit_count" data-count="{count}">',
            (
                '    <rect class="kpi-card" x="1000" y="360" width="450" height="175" '
                f'rx="16" fill="{CARD}" stroke="{BORDER}" stroke-width="2"/>'
            ),
            '    <text class="body" x="1026" y="402">Unexpected hits</text>',
            (
                f'    <text x="1026" y="461" fill="{TEXT}" font-family="system-ui, sans-serif" '
                f'font-size="38" font-weight="750">{count}</text>'
            ),
            '    <text class="small" x="1026" y="502">count across all frozen outcomes</text>',
            "  </g>",
            (
                f'  <rect x="60" y="575" width="1880" height="132" rx="16" '
                f'fill="{MISS_BACKGROUND}" stroke="{MISS}" stroke-width="2"/>'
            ),
            (
                f'  <text x="88" y="615" fill="{MISS}" font-family="system-ui, sans-serif" '
                'font-size="18" font-weight="750">Known semantic miss · disclosed, not '
                "hidden</text>"
            ),
            (
                '  <text class="body" x="88" y="651">“remove note” returned 0 lexical hits '
                "although tombstone-boundary is judged g3 relevant.</text>"
            ),
            (
                '  <text class="small" x="88" y="682">RecallLedger makes no semantic-retrieval '
                "claim; macro Recall, MRR, and nDCG co-move here and are not independent evidence."
                "</text>"
            ),
            *_provenance_footer(snapshot, y=750),
            "</svg>",
        ]
    )
    return _payload(body, context="lexical evaluation summary")


def _hit_map(outcome: OutcomeView) -> Mapping[str, HitView]:
    return {hit.document_id: hit for hit in outcome.hits}


def _header_lines(document_id: str) -> tuple[str, str]:
    parts = document_id.split("-", maxsplit=1)
    return (parts[0], "" if len(parts) == 1 else parts[1])


def render_matrix(snapshot: EvalSnapshot) -> bytes:
    """Render the complete query-document judgment and frozen-rank matrix."""

    width, height = 2_400, 1_320
    slug = "lexical-search-query-matrix"
    grid_x, grid_y = 530, 190
    cell_width, row_height = 220, 68
    body = _root_open(snapshot, width=width, height=height, slug=slug)
    body.extend(
        [
            f'  <title id="{slug}-title">Frozen lexical query by document matrix</title>',
            (
                f'  <desc id="{slug}-description">All ninety-six judged query-document cells. '
                "Fill encodes rank only within a query, labels show raw score, and gold marks "
                "graded relevance. The table exposes every query, document, grade, rank, and "
                "score through ARIA cell labels and data attributes.</desc>"
            ),
            *_common_style(),
            f'  <rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
            '  <text class="title" x="60" y="55">Lexical search · query x document</text>',
            (
                '  <text class="subtitle" x="60" y="88">12 queries x 8 documents · fill is '
                "within-query rank only · raw scores are not comparable across queries</text>"
            ),
            (
                '  <text class="small" x="60" y="119">Gold outline/badge = human relevance '
                "grade; orange = the one known semantic miss.</text>"
            ),
        ]
    )

    for index, document in enumerate(snapshot.documents):
        x = grid_x + index * cell_width
        first, second = _header_lines(document.document_id)
        body.extend(
            [
                (
                    f'  <text x="{x + cell_width // 2}" y="151" text-anchor="middle" '
                    f'fill="{TEXT}" font-family="ui-monospace, monospace" font-size="13" '
                    f'font-weight="700">{escape(first)}</text>'
                ),
                (
                    f'  <text x="{x + cell_width // 2}" y="170" text-anchor="middle" '
                    f'fill="{SECONDARY}" font-family="ui-monospace, monospace" font-size="12">'
                    f"{escape(second)}</text>"
                ),
            ]
        )

    body.append(
        '  <g class="matrix-table" role="table" aria-label="Frozen lexical query by document '
        'result table" aria-rowcount="12" aria-colcount="8">'
    )
    for row_index, (query, outcome) in enumerate(
        zip(snapshot.queries, snapshot.outcomes, strict=True)
    ):
        y = grid_y + row_index * row_height
        body.extend(
            [
                (
                    f'  <g class="matrix-row" role="row" aria-rowindex="{row_index + 1}" '
                    f"data-query-id={quoteattr(query.query_id)} "
                    f"aria-label={quoteattr(f'{query.query_id}, query {query.text}')}>"
                ),
                (
                    f'  <text x="60" y="{y + 28}" fill="{TEXT}" '
                    'font-family="ui-monospace, monospace" font-size="14" font-weight="700">'
                    f"{escape(query.query_id)}</text>"
                ),
                (
                    f'  <text x="260" y="{y + 28}" fill="{SECONDARY}" '
                    'font-family="system-ui, sans-serif" font-size="14">'
                    f"{escape(query.text)}</text>"
                ),
            ]
        )
        hits = _hit_map(outcome)
        for column_index, (document, grade) in enumerate(
            zip(snapshot.documents, query.grades, strict=True)
        ):
            x = grid_x + column_index * cell_width
            hit = hits.get(document.document_id)
            known_miss = grade > 0 and hit is None
            if known_miss:
                fill, stroke, stroke_width = MISS_BACKGROUND, MISS, 3
            elif hit is not None:
                fill, rank_stroke = RANK_COLORS[hit.rank]
                stroke = GOLD if grade > 0 else rank_stroke
                stroke_width = 3 if grade > 0 else 2
            else:
                fill = CARD
                stroke = GOLD if grade > 0 else BORDER
                stroke_width = 3 if grade > 0 else 1
            result_label = (
                "not retrieved, known relevant miss"
                if known_miss
                else ("not retrieved" if hit is None else f"rank {hit.rank}, score {hit.total}")
            )
            cell_label = (
                f"{query.query_id}, query {query.text}; document {document.document_id}, "
                f"{document.title}; relevance grade {grade}; {result_label}"
            )
            attributes = [
                'class="matrix-cell"',
                'role="cell"',
                f'aria-colindex="{column_index + 1}"',
                f"aria-label={quoteattr(cell_label)}",
                f"data-query-id={quoteattr(query.query_id)}",
                f"data-document-id={quoteattr(document.document_id)}",
                f'data-grade="{grade}"',
                f'data-rank="{0 if hit is None else hit.rank}"',
                f'data-score="{0 if hit is None else hit.total}"',
            ]
            body.append(f"  <g {' '.join(attributes)}>")
            body.append(
                f'    <rect x="{x + 3}" y="{y + 3}" width="214" height="62" rx="9" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
            )
            if known_miss:
                body.append(
                    f'    <text x="{x + 110}" y="{y + 40}" text-anchor="middle" fill="{MISS}" '
                    'font-family="system-ui, sans-serif" font-size="15" font-weight="750">'
                    "&#215; miss</text>"
                )
            elif hit is not None:
                body.append(
                    f'    <text x="{x + 110}" y="{y + 40}" text-anchor="middle" fill="{TEXT}" '
                    'font-family="ui-monospace, monospace" font-size="14" font-weight="700">'
                    f"r{hit.rank} · s{hit.total}</text>"
                )
            if grade > 0:
                body.extend(
                    [
                        (
                            f'    <rect x="{x + 172}" y="{y + 10}" width="36" height="22" '
                            f'rx="7" fill="{GOLD}"/>'
                        ),
                        (
                            f'    <text x="{x + 190}" y="{y + 26}" text-anchor="middle" '
                            'fill="#ffffff" font-family="ui-monospace, monospace" '
                            f'font-size="12" font-weight="750">g{grade}</text>'
                        ),
                    ]
                )
            body.append("  </g>")
        body.append("  </g>")
    body.append("  </g>")

    legend_y = 1_035
    body.append(f'  <text class="body" x="60" y="{legend_y}">Within-query rank:</text>')
    for offset, rank in enumerate((1, 2, 3, 4)):
        fill, stroke = RANK_COLORS[rank]
        x = 245 + offset * 145
        body.extend(
            [
                f'  <rect x="{x}" y="{legend_y - 22}" width="38" height="26" rx="6" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>',
                f'  <text class="small" x="{x + 48}" y="{legend_y}">r{rank}</text>',
            ]
        )
    body.extend(
        [
            (
                f'  <rect x="850" y="{legend_y - 22}" width="38" height="26" rx="6" '
                f'fill="{CARD}" stroke="{GOLD}" stroke-width="3"/>'
            ),
            f'  <text class="small" x="898" y="{legend_y}">judged relevant (g1-g3)</text>',
            (
                f'  <rect x="1210" y="{legend_y - 22}" width="38" height="26" rx="6" '
                f'fill="{MISS_BACKGROUND}" stroke="{MISS}" stroke-width="3"/>'
            ),
            f'  <text class="small" x="1258" y="{legend_y}">relevant, not retrieved</text>',
            (
                '  <rect x="60" y="1070" width="2280" height="82" rx="14" '
                'fill="#eef3f8" stroke="#66768a" stroke-width="2"/>'
            ),
            (
                '  <text class="body" x="86" y="1102">Operator-shaped input stays data: '
                "OR NEAR wildcard* → terms [or, near, wildcard]</text>"
            ),
            (
                '  <text class="mono" x="86" y="1132">encoded '
                "[u6f72, u6e656172, u77696c6463617264] → “u6f72” AND “u6e656172” AND "
                "“u77696c6463617264”</text>"
            ),
            *_provenance_footer(snapshot, y=1190),
            "</svg>",
        ]
    )
    return _payload(body, context="lexical query matrix")


def render_breakdown(snapshot: EvalSnapshot) -> bytes:
    """Render the q01 zero-baseline score-component explanation."""

    width, height = 1_800, 850
    slug = "lexical-search-ranking-breakdown"
    outcome = snapshot.outcomes[0]
    query = snapshot.queries[0]
    if outcome.query_id != "q01-retrieval" or query.text != "retrieval":
        _fail("ranking breakdown is not bound to the frozen q01 retrieval case")
    grade_by_document = dict(
        zip((document.document_id for document in snapshot.documents), query.grades, strict=True)
    )
    chart_x, chart_width = 420, 1_120
    scale = chart_width // 20
    body = _root_open(snapshot, width=width, height=height, slug=slug)
    body.extend(
        [
            f'  <title id="{slug}-title">q01 retrieval ranking score breakdown</title>',
            (
                f'  <desc id="{slug}-description">A zero-baseline stacked score breakdown for '
                "the four q01 retrieval hits: title, tag, and body components only.</desc>"
            ),
            *_common_style(),
            f'  <rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
            '  <text class="title" x="60" y="55">Why q01 ranks in this order</text>',
            (
                '  <text class="subtitle" x="60" y="88">query “retrieval” · one shared '
                "zero baseline · integer production score components</text>"
            ),
            (
                '  <text class="small" x="60" y="118">Only this single query is compared; '
                "phrase bonuses are 0 for all four hits.</text>"
            ),
        ]
    )

    legends = (("T", "title", 60), ("G", "tag", 245), ("B", "body", 410))
    for letter, component, x in legends:
        body.extend(
            [
                f'  <rect x="{x}" y="145" width="30" height="24" rx="5" '
                f'fill="{BREAKDOWN_COLORS[component]}"/>',
                f'  <text x="{x + 15}" y="162" text-anchor="middle" fill="#ffffff" '
                'font-family="ui-monospace, monospace" font-size="12" font-weight="750">'
                f"{letter}</text>",
                f'  <text class="small" x="{x + 40}" y="163">{component} term weight</text>',
            ]
        )

    for tick in range(0, 21, 4):
        x = chart_x + tick * scale
        body.extend(
            [
                f'  <line x1="{x}" y1="205" x2="{x}" y2="650" stroke="#ccd6e2" stroke-width="1"/>',
                f'  <text class="small" x="{x}" y="194" text-anchor="middle">{tick}</text>',
            ]
        )

    document_titles = {document.document_id: document.title for document in snapshot.documents}
    for index, hit in enumerate(outcome.hits):
        y = 230 + index * 105
        grade = grade_by_document[hit.document_id]
        body.extend(
            [
                f'  <g class="score-row" data-document-id={quoteattr(hit.document_id)} '
                f'data-rank="{hit.rank}" data-grade="{grade}" data-total="{hit.total}" '
                f'data-title="{hit.title}" data-tag="{hit.tag}" data-body="{hit.body}">',
                (
                    f'    <text x="60" y="{y + 22}" fill="{TEXT}" '
                    'font-family="system-ui, sans-serif" font-size="16" font-weight="700">'
                    f"{escape(document_titles[hit.document_id])}</text>"
                ),
                (
                    f'    <text class="small" x="60" y="{y + 47}">'
                    f"r{hit.rank} · g{grade} · total {hit.total}</text>"
                ),
                f'    <rect x="{chart_x}" y="{y}" width="{chart_width}" height="58" rx="8" '
                f'fill="{CARD}" stroke="{BORDER}" stroke-width="2"/>',
            ]
        )
        cursor = chart_x
        for letter, component, value in (
            ("T", "title", hit.title * 12),
            ("G", "tag", hit.tag * 8),
            ("B", "body", hit.body * 3),
        ):
            if value == 0:
                continue
            segment_width = value * scale
            body.extend(
                [
                    f'    <rect class="score-segment" data-component={quoteattr(component)} '
                    f'data-value="{value}" x="{cursor}" y="{y}" width="{segment_width}" '
                    f'height="58" fill="{BREAKDOWN_COLORS[component]}"/>',
                    (
                        f'    <text x="{cursor + segment_width // 2}" y="{y + 37}" '
                        'text-anchor="middle" fill="#ffffff" font-family="ui-monospace, monospace" '
                        f'font-size="14" font-weight="750">{letter} {value}</text>'
                    ),
                ]
            )
            cursor += segment_width
        body.extend(
            [
                f'    <text x="{chart_x + hit.total * scale + 14}" y="{y + 37}" fill="{TEXT}" '
                'font-family="ui-monospace, monospace" font-size="14" font-weight="750">'
                f"{hit.total}</text>",
                "  </g>",
            ]
        )

    body.extend(
        [
            (
                '  <text class="small" x="420" y="675">Score = 12 x title term frequency '
                "+ 8 x tag term frequency + 3 x body term frequency; q01 phrase bonuses = 0.</text>"
            ),
            *_provenance_footer(snapshot, y=720),
            "</svg>",
        ]
    )
    return _payload(body, context="lexical ranking breakdown")


def _payload(lines: Sequence[str], *, context: str) -> bytes:
    payload = ("\n".join(lines) + "\n").encode("utf-8", errors="strict")
    if len(payload) > MAX_SVG_BYTES:
        _fail(f"{context} SVG exceeds its byte bound")
    return payload


def render_all(snapshot: EvalSnapshot) -> dict[str, bytes]:
    """Render the three canonical visual projections in stable filename order."""

    return dict(
        zip(
            OUTPUT_NAMES,
            (
                render_summary(snapshot),
                render_matrix(snapshot),
                render_breakdown(snapshot),
            ),
            strict=True,
        )
    )


def _replaceable_target_identity(directory_fd: int, filename: str) -> tuple[int, int] | None:
    try:
        metadata = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _fail("lexical visual output target is unavailable or unsafe")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        _fail("lexical visual output must be one single-link regular file")
    return (metadata.st_dev, metadata.st_ino)


def _open_replaceable_target(
    directory_fd: int,
    filename: str,
) -> tuple[int, tuple[int, int] | None]:
    descriptor = -1
    try:
        descriptor = os.open(filename, _READ_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        return (-1, None)
    except OSError:
        _fail("lexical visual output target is unavailable or unsafe")
    try:
        metadata = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or not _same_file_metadata(metadata, current)
        ):
            _fail("lexical visual output target changed while it was opened")
    except LexicalEvalRenderError:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        _fail("lexical visual output target is unavailable or unsafe")
    except BaseException:
        os.close(descriptor)
        raise
    else:
        return (descriptor, (metadata.st_dev, metadata.st_ino))


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            _fail("lexical visual output write made no progress")
        offset += written


def _atomic_write_at(directory_fd: int, filename: str, payload: bytes) -> None:
    original_fd, original_identity = _open_replaceable_target(directory_fd, filename)
    temporary_name = f".{filename}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    temporary_fd = -1
    temporary_exists = False
    try:
        temporary_fd = os.open(temporary_name, _WRITE_FLAGS, 0o600, dir_fd=directory_fd)
        temporary_exists = True
        metadata = os.fstat(temporary_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            _fail("lexical visual temporary must be one single-link regular file")
        _write_all(temporary_fd, payload)
        os.fchmod(temporary_fd, 0o644)
        os.fsync(temporary_fd)
        written_metadata = os.fstat(temporary_fd)
        if not stat.S_ISREG(written_metadata.st_mode) or written_metadata.st_nlink != 1:
            _fail("lexical visual temporary changed during write")
        temporary_identity = (written_metadata.st_dev, written_metadata.st_ino)

        if _replaceable_target_identity(directory_fd, filename) != original_identity:
            _fail("lexical visual output target changed during atomic write")
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
            _fail("lexical visual temporary changed before atomic replacement")
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_exists = False
        installed = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(installed.st_mode)
            or installed.st_nlink != 1
            or (installed.st_dev, installed.st_ino) != temporary_identity
        ):
            _fail("lexical visual output changed during atomic replacement")
        os.fsync(directory_fd)
        os.close(temporary_fd)
        temporary_fd = -1
    except LexicalEvalRenderError:
        raise
    except OSError:
        _fail("lexical visual output could not be replaced atomically")
    finally:
        if original_fd >= 0:
            os.close(original_fd)
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)


def _atomic_write(filename: str, payload: bytes) -> None:
    if filename not in OUTPUT_NAMES or len(payload) > MAX_SVG_BYTES:
        _fail("lexical visual renderer refused an unexpected output")
    directory_fd = _open_directory_fd(
        OUTPUT_DIRECTORY,
        context="lexical visual output directory",
    )
    try:
        _atomic_write_at(directory_fd, filename, payload)
    finally:
        os.close(directory_fd)


def _read_output(filename: str) -> bytes:
    if filename not in OUTPUT_NAMES:
        _fail("lexical visual renderer refused an unexpected output name")
    return _read_regular_file(
        OUTPUT_DIRECTORY / filename,
        limit=MAX_SVG_BYTES,
        context=f"lexical visual output {filename}",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render source-bound SVGs from the frozen lexical evaluation.",
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write", action="store_true", help="atomically write all three SVGs")
    modes.add_argument("--check", action="store_true", help="compare all three SVGs byte-for-byte")
    modes.add_argument(
        "--stdout",
        choices=tuple(STDOUT_CHOICES),
        metavar="{summary,matrix,breakdown}",
        help="write exactly one named SVG to standard output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for deterministic eval visual generation."""

    arguments = _parser().parse_args(argv)
    try:
        rendered = render_all(load_snapshot())
        if arguments.write:
            for filename in OUTPUT_NAMES:
                _atomic_write(filename, rendered[filename])
        elif arguments.check:
            for filename in OUTPUT_NAMES:
                if _read_output(filename) != rendered[filename]:
                    _fail(f"{filename} is not byte-for-byte current")
        else:
            sys.stdout.buffer.write(rendered[STDOUT_CHOICES[cast(str, arguments.stdout)]])
    except (LexicalEvalRenderError, contract.LexicalEvalContractError, OSError) as error:
        sys.stderr.write(f"lexical eval visual error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
