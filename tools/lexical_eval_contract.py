#!/usr/bin/env python3
"""Validate the frozen RecallLedger lexical conformance evaluation.

The evaluator is intentionally check-only. Source fixtures are human-reviewed
inputs; this module never rewrites judgments or expected outcomes from the
current scorer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final, NoReturn, TypeAlias, cast

ROOT: Final = Path(__file__).resolve().parents[1]
SOURCE_ROOT: Final = ROOT / "src"
if os.fspath(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SOURCE_ROOT))

from recall_ledger.events import ContractViolation, NoteContent  # noqa: E402
from recall_ledger.retrieval import (  # noqa: E402
    LEXICAL_CONTRACT_VERSION,
    LEXICAL_UNICODE_PROFILE,
    LexicalScore,
    RetrievalContractError,
    compile_lexical_query,
    score_lexical_content,
)

JsonScalar: TypeAlias = str | int | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

DEFAULT_SUITE_DIRECTORY: Final = ROOT / "evals" / "lexical-v1"
CORPUS_FILE: Final = "corpus.v1.json"
QUERIES_FILE: Final = "queries.v1.json"
EXPECTED_FILE: Final = "expected.v1.json"
SUITE_ID: Final = "recall-ledger-lexical-v1"
ARTIFACT_NAME: Final = "recall-ledger-lexical-expected"
SCHEMA_VERSION: Final = 1
CUTOFF: Final = 5
DOCUMENT_COUNT: Final = 8
QUERY_COUNT: Final = 12
FROZEN_CORPUS_SHA256: Final = "a41bbb16bf618f0dbfaf9f62f7c85696db6d0e7ff6113491c4bcfe71cb7658e7"
FROZEN_QUERIES_SHA256: Final = "d0fbf57ea8396eaa97a9093a9ca24a473564f3f3c3d8aa87dce70f6002aba7dd"
FROZEN_EXPECTED_SHA256: Final = "ae5a3c4815ef9988e72de76e51408790dd9ac980bacc3a8ce00db58102a288c9"
OPERATOR_QUERY_ID: Final = "q12-literal-operators"
OPERATOR_QUERY_TEXT: Final = "OR NEAR wildcard*"
OPERATOR_TERMS: Final = ("or", "near", "wildcard")
OPERATOR_ENCODED_TERMS: Final = ("u6f72", "u6e656172", "u77696c6463617264")
OPERATOR_MATCH_EXPRESSION: Final = '"u6f72" AND "u6e656172" AND "u77696c6463617264"'
MAX_JSON_BYTES: Final = 65_536
MAX_JSON_DEPTH: Final = 12
MAX_CONTAINER_ITEMS: Final = 256
MAX_TEXT_CODEPOINTS: Final = 65_536
MAX_INTEGER_DIGITS: Final = 19
MAX_RECORDED_AT_US: Final = 253_402_300_799_999_999
MAX_SCORE_TOTAL: Final = 4_096
MAX_SCORE_FREQUENCY: Final = 48
MIN_MULTI_TERM_COUNT: Final = 2
CONTROL_CODEPOINT_LIMIT: Final = 0x20
C1_CONTROL_MIN: Final = 0x7F
C1_CONTROL_MAX: Final = 0x9F
SURROGATE_MIN: Final = 0xD800
SURROGATE_MAX: Final = 0xDFFF
NONCHARACTER_MIN: Final = 0xFDD0
NONCHARACTER_MAX: Final = 0xFDEF
PLANE_CODEPOINT_MASK: Final = 0xFFFF
PLANE_NONCHARACTERS: Final = frozenset({0xFFFE, 0xFFFF})

CASE_TAGS: Final = (
    "all-terms",
    "body",
    "field-weighting",
    "no-match",
    "operator-inertness",
    "phrase",
    "semantic-miss",
    "tag",
    "title",
    "unicode",
)
METRIC_NAMES: Final = (
    "exact_outcome_rate",
    "macro_ndcg_at_5",
    "macro_recall_at_5",
    "micro_recall_at_5",
    "mrr_at_5",
    "success_at_1",
)
SCORE_KEYS: Final = frozenset(
    {
        "body_phrase",
        "body_term_frequency",
        "tag_phrase",
        "tag_term_frequency",
        "title_phrase",
        "title_term_frequency",
        "total",
    }
)

_DOCUMENT_ID_PATTERN: Final = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_QUERY_ID_PATTERN: Final = re.compile(r"q[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
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


class LexicalEvalContractError(ValueError):
    """A frozen lexical evaluation source violated its closed contract."""


@dataclass(frozen=True, slots=True)
class Document:
    """One validated synthetic current-head document."""

    document_id: str
    recorded_at_us: int
    content: NoteContent


@dataclass(frozen=True, slots=True)
class QueryCase:
    """One validated query with complete graded judgments."""

    query_id: str
    text: str
    case_tags: tuple[str, ...]
    judgments: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    """Reviewable result of checking the complete frozen suite."""

    corpus_sha256: str
    expected_sha256: str
    queries_sha256: str
    document_count: int
    query_count: int
    cutoff: int
    unicode_profile: str
    known_semantic_miss_count: int
    operator_inertness_case_count: int
    metrics: JsonObject

    def to_object(self) -> JsonObject:
        """Return the canonical machine-readable success record."""

        return {
            "corpus_sha256": self.corpus_sha256,
            "cutoff": self.cutoff,
            "document_count": self.document_count,
            "expected_sha256": self.expected_sha256,
            "known_semantic_miss_count": self.known_semantic_miss_count,
            "metrics": self.metrics,
            "operator_inertness_case_count": self.operator_inertness_case_count,
            "queries_sha256": self.queries_sha256,
            "query_count": self.query_count,
            "suite_id": SUITE_ID,
            "unicode_profile": self.unicode_profile,
        }


def _fail(message: str) -> NoReturn:
    raise LexicalEvalContractError(message)


def _reject_number(_value: str) -> NoReturn:
    _fail("lexical evaluation JSON permits bounded integers only")


def _parse_integer(value: str) -> int:
    digits = value.removeprefix("-")
    if not digits or len(digits) > MAX_INTEGER_DIGITS:
        _fail("lexical evaluation JSON integer exceeds its lexical bound")
    return int(value)


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            _fail("lexical evaluation JSON contains a duplicate object key")
        result[key] = value
    return result


def _is_noncharacter(codepoint: int) -> bool:
    return (
        NONCHARACTER_MIN <= codepoint <= NONCHARACTER_MAX
        or codepoint & PLANE_CODEPOINT_MASK in PLANE_NONCHARACTERS
    )


def _validate_text(value: str) -> None:
    if len(value) > MAX_TEXT_CODEPOINTS:
        _fail("lexical evaluation JSON text exceeds its bound")
    for character in value:
        codepoint = ord(character)
        if (
            codepoint < CONTROL_CODEPOINT_LIMIT
            or C1_CONTROL_MIN <= codepoint <= C1_CONTROL_MAX
            or SURROGATE_MIN <= codepoint <= SURROGATE_MAX
            or codepoint in _BIDI_CONTROL_CODEPOINTS
            or _is_noncharacter(codepoint)
        ):
            _fail("lexical evaluation JSON contains unsafe text")


def _validate_json_value(value: JsonValue, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("lexical evaluation JSON nesting exceeds its bound")
    if type(value) is str:
        _validate_text(value)
        return
    if type(value) is int:
        if len(str(abs(value))) > MAX_INTEGER_DIGITS:
            _fail("lexical evaluation JSON integer exceeds its bound")
        return
    if value is None or type(value) is bool:
        return
    if type(value) is list:
        if len(value) > MAX_CONTAINER_ITEMS:
            _fail("lexical evaluation JSON array has too many items")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > MAX_CONTAINER_ITEMS:
            _fail("lexical evaluation JSON object has too many keys")
        for key, item in value.items():
            _validate_text(key)
            _validate_json_value(item, depth=depth + 1)
        return
    _fail("lexical evaluation JSON contains an unsupported value")


def canonical_json_bytes(value: JsonValue) -> bytes:
    """Serialize one bounded value with the evaluation's UTF-8 convention."""

    _validate_json_value(value)
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError, ValueError):
        _fail("lexical evaluation value cannot be serialized canonically")
    return payload + b"\n"


def decode_canonical_document(raw: bytes, *, context: str) -> JsonObject:
    """Decode one compact, duplicate-free, newline-terminated JSON object."""

    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_JSON_BYTES:
        _fail(f"{context} byte length is outside its bound")
    if raw.startswith(b"\xef\xbb\xbf") or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        _fail(f"{context} is not exactly one newline-terminated UTF-8 document")
    try:
        value = cast(
            JsonValue,
            json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_int=_parse_integer,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            ),
        )
    except LexicalEvalContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail(f"{context} is not strict bounded UTF-8 JSON")
    _validate_json_value(value)
    if type(value) is not dict:
        _fail(f"{context} must contain one JSON object")
    if canonical_json_bytes(value) != raw:
        _fail(f"{context} is not compact canonical JSON")
    return value


def _read_regular_file(path: Path, *, context: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail(f"{context} is unavailable")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= MAX_JSON_BYTES:
            _fail(f"{context} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8_192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError:
        _fail(f"{context} could not be read safely")
    else:
        if len(raw) != metadata.st_size or len(raw) > MAX_JSON_BYTES:
            _fail(f"{context} changed while it was read")
        return raw
    finally:
        os.close(descriptor)


def _load(directory: Path, filename: str) -> tuple[bytes, JsonObject]:
    raw = _read_regular_file(directory / filename, context=filename)
    return raw, decode_canonical_document(raw, context=filename)


def _object(value: JsonValue, context: str) -> JsonObject:
    if type(value) is not dict:
        _fail(f"{context} must be an object")
    return value


def _array(value: JsonValue, context: str) -> list[JsonValue]:
    if type(value) is not list:
        _fail(f"{context} must be an array")
    return value


def _text(value: JsonValue, context: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _fail(f"{context} must be exact non-empty text")
    return value


def _integer(
    value: JsonValue,
    context: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_RECORDED_AT_US,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{context} must be an exact bounded integer")
    return value


def _boolean(value: JsonValue, context: str) -> bool:
    if type(value) is not bool:
        _fail(f"{context} must be an exact boolean")
    return value


def _keys(value: JsonObject, expected: frozenset[str], context: str) -> None:
    if value.keys() != expected:
        _fail(f"{context} has unknown or missing fields")


def _exact(value: JsonValue, expected: JsonScalar, context: str) -> None:
    if type(value) is not type(expected) or value != expected:
        _fail(f"{context} does not match the frozen contract")


def _parse_corpus(value: JsonObject) -> tuple[Document, ...]:
    _keys(value, frozenset({"documents", "schema_version", "suite_id"}), "corpus")
    _exact(value["schema_version"], SCHEMA_VERSION, "corpus schema version")
    _exact(value["suite_id"], SUITE_ID, "corpus suite identifier")
    rows = _array(value["documents"], "corpus documents")
    if len(rows) != DOCUMENT_COUNT:
        _fail("corpus must contain exactly eight documents")

    documents: list[Document] = []
    for index, item in enumerate(rows, start=1):
        row = _object(item, f"corpus document {index}")
        _keys(
            row,
            frozenset({"content", "document_id", "recorded_at_us"}),
            f"corpus document {index}",
        )
        document_id = _text(row["document_id"], f"corpus document {index} identifier")
        if _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None:
            _fail("corpus document identifier is not canonical")
        recorded_at_us = _integer(
            row["recorded_at_us"],
            f"corpus document {document_id} recorded time",
            minimum=1,
        )
        content = _object(row["content"], f"corpus document {document_id} content")
        _keys(content, frozenset({"body", "tags", "title"}), "corpus document content")
        title = _text(content["title"], f"corpus document {document_id} title")
        body = _text(
            content["body"],
            f"corpus document {document_id} body",
            allow_empty=True,
        )
        tag_values = _array(content["tags"], f"corpus document {document_id} tags")
        tags = tuple(_text(tag, f"corpus document {document_id} tag") for tag in tag_values)
        try:
            note_content = NoteContent(title=title, body=body, tags=tags)
        except ContractViolation:
            _fail(f"corpus document {document_id} violates the note-content contract")
        documents.append(Document(document_id, recorded_at_us, note_content))

    identifiers = tuple(document.document_id for document in documents)
    timestamps = tuple(document.recorded_at_us for document in documents)
    if identifiers != tuple(sorted(set(identifiers))):
        _fail("corpus documents must have unique sorted identifiers")
    if len(set(timestamps)) != len(timestamps):
        _fail("corpus document timestamps must be unique")
    return tuple(documents)


def _parse_queries(value: JsonObject, document_ids: tuple[str, ...]) -> tuple[QueryCase, ...]:
    _keys(
        value,
        frozenset({"case_tag_vocabulary", "queries", "schema_version", "suite_id"}),
        "queries",
    )
    _exact(value["schema_version"], SCHEMA_VERSION, "query schema version")
    _exact(value["suite_id"], SUITE_ID, "query suite identifier")
    vocabulary = tuple(
        _text(item, "query case-tag vocabulary item")
        for item in _array(value["case_tag_vocabulary"], "query case-tag vocabulary")
    )
    if vocabulary != CASE_TAGS:
        _fail("query case-tag vocabulary does not match version 1")
    rows = _array(value["queries"], "query cases")
    if len(rows) != QUERY_COUNT:
        _fail("query source must contain exactly twelve cases")

    cases: list[QueryCase] = []
    used_tags: set[str] = set()
    for index, item in enumerate(rows, start=1):
        row = _object(item, f"query case {index}")
        _keys(
            row,
            frozenset({"case_tags", "judgments", "query_id", "text"}),
            f"query case {index}",
        )
        query_id = _text(row["query_id"], f"query case {index} identifier")
        if _QUERY_ID_PATTERN.fullmatch(query_id) is None:
            _fail("query identifier is not canonical")
        text = _text(row["text"], f"query {query_id} text")
        tag_values = tuple(
            _text(tag, f"query {query_id} case tag")
            for tag in _array(row["case_tags"], f"query {query_id} case tags")
        )
        if not tag_values or tag_values != tuple(sorted(set(tag_values))):
            _fail(f"query {query_id} case tags must be non-empty, unique, and sorted")
        if not set(tag_values) <= set(CASE_TAGS):
            _fail(f"query {query_id} uses an unknown case tag")
        used_tags.update(tag_values)

        judgments: list[tuple[str, int]] = []
        for judgment_item in _array(row["judgments"], f"query {query_id} judgments"):
            judgment = _object(judgment_item, f"query {query_id} judgment")
            _keys(judgment, frozenset({"document_id", "grade"}), "query judgment")
            judged_id = _text(judgment["document_id"], f"query {query_id} judged document")
            grade = _integer(
                judgment["grade"],
                f"query {query_id} relevance grade",
                maximum=3,
            )
            judgments.append((judged_id, grade))
        if tuple(document_id for document_id, _grade in judgments) != document_ids:
            _fail(f"query {query_id} must judge every document once in sorted order")
        if not any(grade >= 1 for _document_id, grade in judgments):
            _fail(f"query {query_id} must have at least one relevant judgment")
        try:
            compile_lexical_query(text)
        except RetrievalContractError:
            _fail(f"query {query_id} violates the production query contract")
        cases.append(QueryCase(query_id, text, tag_values, tuple(judgments)))

    query_ids = tuple(case.query_id for case in cases)
    if query_ids != tuple(sorted(set(query_ids))):
        _fail("query cases must have unique sorted identifiers")
    if used_tags != set(CASE_TAGS):
        _fail("query cases do not exercise every declared case tag")
    return tuple(cases)


def _validate_digest_record(
    value: JsonValue,
    *,
    expected_file: str,
    actual_digest: str,
    context: str,
) -> None:
    record = _object(value, context)
    _keys(record, frozenset({"file", "sha256"}), context)
    _exact(record["file"], expected_file, f"{context} filename")
    digest = _text(record["sha256"], f"{context} digest")
    if _SHA256_PATTERN.fullmatch(digest) is None or digest != actual_digest:
        _fail(f"{context} digest does not bind the canonical source bytes")


def _validate_score(value: JsonValue, context: str) -> None:
    score = _object(value, context)
    _keys(score, SCORE_KEYS, context)
    _integer(score["total"], f"{context} total", maximum=MAX_SCORE_TOTAL)
    for field in ("title_term_frequency", "tag_term_frequency", "body_term_frequency"):
        _integer(score[field], f"{context} {field}", maximum=MAX_SCORE_FREQUENCY)
    for field in ("title_phrase", "tag_phrase", "body_phrase"):
        _boolean(score[field], f"{context} {field}")


def _parse_expected(
    value: JsonObject,
    *,
    corpus_digest: str,
    queries_digest: str,
    query_ids: tuple[str, ...],
    document_ids: frozenset[str],
) -> tuple[dict[str, JsonObject], JsonObject]:
    _keys(
        value,
        frozenset(
            {
                "artifact",
                "cutoff",
                "expected_metrics",
                "inputs",
                "lexical_contract_version",
                "outcomes",
                "schema_version",
                "suite_id",
                "unicode_profile_policy",
            }
        ),
        "expected outcomes",
    )
    _exact(value["artifact"], ARTIFACT_NAME, "expected artifact")
    _exact(value["cutoff"], CUTOFF, "expected cutoff")
    _exact(value["lexical_contract_version"], LEXICAL_CONTRACT_VERSION, "lexical contract")
    _exact(value["schema_version"], SCHEMA_VERSION, "expected schema version")
    _exact(value["suite_id"], SUITE_ID, "expected suite identifier")
    _exact(value["unicode_profile_policy"], "runtime-bound", "Unicode profile policy")

    inputs = _object(value["inputs"], "expected inputs")
    _keys(inputs, frozenset({"corpus", "queries"}), "expected inputs")
    _validate_digest_record(
        inputs["corpus"],
        expected_file=CORPUS_FILE,
        actual_digest=corpus_digest,
        context="expected corpus input",
    )
    _validate_digest_record(
        inputs["queries"],
        expected_file=QUERIES_FILE,
        actual_digest=queries_digest,
        context="expected query input",
    )

    metrics = _object(value["expected_metrics"], "expected metrics")
    _keys(metrics, frozenset({*METRIC_NAMES, "unexpected_hit_count"}), "expected metrics")
    for name in METRIC_NAMES:
        metric = _object(metrics[name], f"expected metric {name}")
        _keys(metric, frozenset({"denominator", "numerator", "ppm"}), f"metric {name}")
        denominator = _integer(metric["denominator"], f"metric {name} denominator", minimum=1)
        numerator = _integer(
            metric["numerator"],
            f"metric {name} numerator",
            maximum=denominator,
        )
        _integer(metric["ppm"], f"metric {name} ppm", maximum=1_000_000)
        if metric["ppm"] != _ppm(Fraction(numerator, denominator)):
            _fail(f"metric {name} ppm does not match its exact fraction")
    unexpected = _object(metrics["unexpected_hit_count"], "unexpected hit metric")
    _keys(unexpected, frozenset({"count"}), "unexpected hit metric")
    _integer(unexpected["count"], "unexpected hit count", maximum=DOCUMENT_COUNT * QUERY_COUNT)

    rows = _array(value["outcomes"], "expected outcomes")
    outcomes: dict[str, JsonObject] = {}
    for index, item in enumerate(rows, start=1):
        outcome = _object(item, f"expected outcome {index}")
        _keys(
            outcome,
            frozenset({"expected_hits", "expected_terms", "expected_total_matches", "query_id"}),
            f"expected outcome {index}",
        )
        query_id = _text(outcome["query_id"], f"expected outcome {index} query identifier")
        if query_id in outcomes:
            _fail("expected outcomes contain a duplicate query identifier")
        terms = tuple(
            _text(term, f"expected outcome {query_id} term")
            for term in _array(outcome["expected_terms"], f"expected outcome {query_id} terms")
        )
        if not terms or terms != tuple(dict.fromkeys(terms)):
            _fail(f"expected outcome {query_id} terms must be non-empty and unique")
        hits = _array(outcome["expected_hits"], f"expected outcome {query_id} hits")
        hit_ids: list[str] = []
        for rank, hit_item in enumerate(hits, start=1):
            hit = _object(hit_item, f"expected outcome {query_id} hit {rank}")
            _keys(hit, frozenset({"document_id", "rank", "score"}), "expected hit")
            document_id = _text(hit["document_id"], "expected hit document identifier")
            if document_id not in document_ids or document_id in hit_ids:
                _fail(f"expected outcome {query_id} has an unknown or duplicate document")
            hit_ids.append(document_id)
            _exact(hit["rank"], rank, f"expected outcome {query_id} rank")
            _validate_score(hit["score"], f"expected outcome {query_id} score")
        _exact(
            outcome["expected_total_matches"],
            len(hits),
            f"expected outcome {query_id} total matches",
        )
        outcomes[query_id] = outcome
    if tuple(outcomes) != query_ids:
        _fail("expected outcomes must cover every query once in sorted order")
    return outcomes, metrics


def _score_object(score: LexicalScore) -> JsonObject:
    if (
        score.contract_version != LEXICAL_CONTRACT_VERSION
        or score.unicode_profile != LEXICAL_UNICODE_PROFILE
    ):
        _fail("production scorer returned a mismatched lexical profile")
    return {
        "body_phrase": score.body_phrase,
        "body_term_frequency": score.body_term_frequency,
        "tag_phrase": score.tag_phrase,
        "tag_term_frequency": score.tag_term_frequency,
        "title_phrase": score.title_phrase,
        "title_term_frequency": score.title_term_frequency,
        "total": score.total,
    }


def _ppm(value: Fraction) -> int:
    numerator = value.numerator * 1_000_000
    return (numerator + value.denominator // 2) // value.denominator


def _metric(numerator: int, denominator: int) -> JsonObject:
    value = Fraction(numerator, denominator)
    return {
        "denominator": denominator,
        "numerator": numerator,
        "ppm": _ppm(value),
    }


def _evaluate(  # noqa: PLR0912,PLR0915 - closed v1 protocol stays linear and reviewable
    documents: tuple[Document, ...],
    cases: tuple[QueryCase, ...],
    expected_outcomes: dict[str, JsonObject],
) -> tuple[JsonObject, int, int]:
    exact_count = 0
    success_count = 0
    macro_recall_sum = Fraction()
    micro_relevant = 0
    micro_retrieved = 0
    reciprocal_rank_sum = Fraction()
    ndcg_perfect_count = 0
    unexpected_count = 0
    known_semantic_misses = 0
    operator_cases = 0

    for case in cases:
        compiled = compile_lexical_query(case.text)
        if (
            compiled.contract_version != LEXICAL_CONTRACT_VERSION
            or compiled.unicode_profile != LEXICAL_UNICODE_PROFILE
        ):
            _fail("production query compiler returned a mismatched lexical profile")
        scored: list[tuple[Document, LexicalScore]] = []
        for document in documents:
            score = score_lexical_content(compiled, document.content)
            if score is not None:
                scored.append((document, score))
        scored.sort(
            key=lambda item: (
                -item[1].total,
                -item[0].recorded_at_us,
                item[0].document_id,
            )
        )
        actual_hits: list[JsonValue] = [
            {
                "document_id": document.document_id,
                "rank": rank,
                "score": _score_object(score),
            }
            for rank, (document, score) in enumerate(scored, start=1)
        ]
        actual_outcome: JsonObject = {
            "expected_hits": actual_hits,
            "expected_terms": list(compiled.terms),
            "expected_total_matches": len(actual_hits),
            "query_id": case.query_id,
        }
        frozen = expected_outcomes[case.query_id]
        if actual_outcome != frozen:
            _fail(f"query {case.query_id} does not match its frozen production outcome")
        exact_count += 1
        frozen_hit_ids = {
            _text(_object(hit, "frozen hit")["document_id"], "frozen hit document")
            for hit in _array(frozen["expected_hits"], "frozen hits")
        }
        unexpected_count += sum(
            document.document_id not in frozen_hit_ids for document, _score in scored
        )

        grades = dict(case.judgments)
        relevant = {document_id for document_id, grade in case.judgments if grade >= 1}
        matched = {document.document_id for document, _score in scored}
        top = tuple(document.document_id for document, _score in scored[:CUTOFF])
        retrieved_relevant = tuple(document_id for document_id in top if document_id in relevant)
        success_count += int(bool(top) and top[0] in relevant)
        macro_recall_sum += Fraction(len(retrieved_relevant), len(relevant))
        micro_retrieved += len(retrieved_relevant)
        micro_relevant += len(relevant)
        first_rank = next(
            (rank for rank, document_id in enumerate(top, start=1) if document_id in relevant),
            None,
        )
        reciprocal_rank_sum += Fraction(0 if first_rank is None else 1, first_rank or 1)

        dcg = sum(
            (2 ** grades[document_id] - 1) / math.log2(rank + 1)
            for rank, document_id in enumerate(top, start=1)
            if grades[document_id] > 0
        )
        ideal_grades = sorted((grade for grade in grades.values() if grade > 0), reverse=True)[
            :CUTOFF
        ]
        ideal_dcg = sum(
            (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal_grades, start=1)
        )
        ndcg = dcg / ideal_dcg
        if math.isclose(ndcg, 1.0, rel_tol=0.0, abs_tol=1e-12):
            ndcg_perfect_count += 1
        elif not math.isclose(ndcg, 0.0, rel_tol=0.0, abs_tol=1e-12):
            _fail("version 1 nDCG is no longer exactly representable by its frozen fraction")

        no_match = "no-match" in case.case_tags
        semantic_miss = "semantic-miss" in case.case_tags
        missed_relevant = relevant - matched
        if no_match != (not scored):
            _fail(f"query {case.query_id} no-match tag disagrees with its result")
        if semantic_miss != bool(missed_relevant):
            _fail(f"query {case.query_id} does not disclose every relevance miss")
        if semantic_miss:
            if scored or not relevant:
                _fail(f"query {case.query_id} is not a real judged semantic miss")
            known_semantic_misses += 1
        if "operator-inertness" in case.case_tags:
            if (
                case.query_id != OPERATOR_QUERY_ID
                or case.text != OPERATOR_QUERY_TEXT
                or compiled.terms != OPERATOR_TERMS
                or compiled.encoded_terms != OPERATOR_ENCODED_TERMS
                or compiled.match_expression != OPERATOR_MATCH_EXPRESSION
            ):
                _fail("operator-shaped query did not compile to the frozen inert expression")
            operator_cases += 1
        if "all-terms" in case.case_tags and len(compiled.terms) < MIN_MULTI_TERM_COUNT:
            _fail(f"query {case.query_id} does not exercise multi-term conjunction")

    query_count = len(cases)
    if macro_recall_sum.denominator != 1 or reciprocal_rank_sum.denominator != 1:
        _fail("version 1 macro metrics are no longer exact whole-query counts")
    observed: JsonObject = {
        "exact_outcome_rate": _metric(exact_count, query_count),
        "macro_ndcg_at_5": _metric(ndcg_perfect_count, query_count),
        "macro_recall_at_5": _metric(macro_recall_sum.numerator, query_count),
        "micro_recall_at_5": _metric(micro_retrieved, micro_relevant),
        "mrr_at_5": _metric(reciprocal_rank_sum.numerator, query_count),
        "success_at_1": _metric(success_count, query_count),
        "unexpected_hit_count": {"count": unexpected_count},
    }
    return observed, known_semantic_misses, operator_cases


def validate_suite(directory: Path = DEFAULT_SUITE_DIRECTORY) -> EvaluationSummary:
    """Validate exact source bytes, schemas, outcomes, and all aggregate metrics."""

    if not isinstance(directory, Path) or not directory.is_absolute():
        _fail("suite directory must be one absolute pathlib.Path")
    corpus_raw, corpus_value = _load(directory, CORPUS_FILE)
    queries_raw, queries_value = _load(directory, QUERIES_FILE)
    expected_raw, expected_value = _load(directory, EXPECTED_FILE)
    corpus_digest = hashlib.sha256(corpus_raw).hexdigest()
    queries_digest = hashlib.sha256(queries_raw).hexdigest()
    expected_digest = hashlib.sha256(expected_raw).hexdigest()
    documents = _parse_corpus(corpus_value)
    document_ids = tuple(document.document_id for document in documents)
    cases = _parse_queries(queries_value, document_ids)
    outcomes, expected_metrics = _parse_expected(
        expected_value,
        corpus_digest=corpus_digest,
        queries_digest=queries_digest,
        query_ids=tuple(case.query_id for case in cases),
        document_ids=frozenset(document_ids),
    )
    frozen_digests = (
        (corpus_digest, FROZEN_CORPUS_SHA256, CORPUS_FILE),
        (queries_digest, FROZEN_QUERIES_SHA256, QUERIES_FILE),
        (expected_digest, FROZEN_EXPECTED_SHA256, EXPECTED_FILE),
    )
    if any(actual != frozen for actual, frozen, _filename in frozen_digests):
        _fail("version 1 frozen file digest changed; create a new suite version")
    observed_metrics, known_misses, operator_cases = _evaluate(documents, cases, outcomes)
    if observed_metrics != expected_metrics:
        _fail("recomputed lexical metrics do not match the frozen expected metrics")
    if known_misses != 1 or operator_cases != 1:
        _fail("version 1 must retain one disclosed semantic miss and one operator case")
    return EvaluationSummary(
        corpus_sha256=corpus_digest,
        expected_sha256=expected_digest,
        queries_sha256=queries_digest,
        document_count=len(documents),
        query_count=len(cases),
        cutoff=CUTOFF,
        unicode_profile=LEXICAL_UNICODE_PROFILE,
        known_semantic_miss_count=known_misses,
        operator_inertness_case_count=operator_cases,
        metrics=observed_metrics,
    )


def main() -> int:
    """Validate the committed suite and emit one canonical success record."""

    try:
        summary = validate_suite()
        sys.stdout.buffer.write(canonical_json_bytes(summary.to_object()))
    except (LexicalEvalContractError, OSError) as error:
        sys.stderr.write(f"lexical evaluation invalid: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
