"""Deterministic lexical query and scoring contract.

The integer scorer in this module is the retrieval correctness oracle. A
future FTS5 projection may generate candidates, but it must not replace these
tenant-local matching and scoring semantics. Derived values bind the exact
Unicode profile; persistent projections must store and compare that profile.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import InitVar, dataclass
from itertools import chain
from typing import Final, NoReturn, SupportsIndex, cast

from .events import (
    MAX_BODY_CODEPOINTS,
    MAX_TAG_CODEPOINTS,
    MAX_TAGS,
    MAX_TITLE_CODEPOINTS,
    NoteContent,
    _validated_content_values,
)

LEXICAL_CONTRACT_VERSION: Final = 1
LEXICAL_UNICODE_PROFILE: Final = (
    f"nfkc-casefold-nfkc+lnm-tokenizer-v1;ucd={unicodedata.unidata_version}"
)
MAX_QUERY_CODEPOINTS: Final = 512
MAX_QUERY_BYTES: Final = 1_024
MAX_NORMALIZED_QUERY_CODEPOINTS: Final = 1_024
MAX_NORMALIZED_QUERY_BYTES: Final = 2_048
MAX_QUERY_TERMS: Final = 16
MAX_QUERY_TERM_CODEPOINTS: Final = 64
MAX_QUERY_TERM_BYTES: Final = 192
_MAX_CONTENT_CODEPOINTS: Final = (
    MAX_TITLE_CODEPOINTS + MAX_BODY_CODEPOINTS + MAX_TAGS * MAX_TAG_CODEPOINTS
)
_MAX_NORMALIZED_CODEPOINTS_PER_SCALAR: Final = 18
_MAX_NORMALIZED_BYTES_PER_SCALAR: Final = 33
MAX_NORMALIZED_CONTENT_CODEPOINTS: Final = (
    _MAX_CONTENT_CODEPOINTS * _MAX_NORMALIZED_CODEPOINTS_PER_SCALAR
)
MAX_NORMALIZED_CONTENT_BYTES: Final = _MAX_CONTENT_CODEPOINTS * _MAX_NORMALIZED_BYTES_PER_SCALAR
MAX_CONTENT_TOKEN_STREAM_BYTES: Final = 262_144
MAX_TERM_FREQUENCY: Final = 3

_TITLE_TERM_WEIGHT: Final = 12
_TAG_TERM_WEIGHT: Final = 8
_BODY_TERM_WEIGHT: Final = 3
_TITLE_PHRASE_BONUS: Final = 24
_TAG_PHRASE_BONUS: Final = 16
_BODY_PHRASE_BONUS: Final = 8
_QUERY_FACTORY_TOKEN: Final = object()
_STREAMS_FACTORY_TOKEN: Final = object()
_SCORE_FACTORY_TOKEN: Final = object()
_MISSING: Final = object()
_HEX_DIGITS: Final = frozenset("0123456789abcdef")
_MIN_ENCODED_TERM_LENGTH: Final = 3
_MAX_ENCODED_TERM_LENGTH: Final = 1 + 2 * MAX_QUERY_TERM_BYTES
_MAX_MATCH_EXPRESSION_LENGTH: Final = MAX_QUERY_TERMS * (_MAX_ENCODED_TERM_LENGTH + 2) + (
    MAX_QUERY_TERMS - 1
) * len(" AND ")


class RetrievalContractError(ValueError):
    """A bounded lexical-contract rejection with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _ExactValueTypeError(TypeError):
    def __init__(self, value_name: str, type_name: str) -> None:
        super().__init__(f"{value_name} must be an exact {type_name}")


class _DerivedValueSerializationError(TypeError):
    def __init__(self) -> None:
        super().__init__("lexical derived values cannot be serialized or restored")


@dataclass(frozen=True, slots=True)
class LexicalQuery:
    """A normalized query whose terms cannot be interpreted as FTS syntax."""

    terms: tuple[str, ...]
    encoded_terms: tuple[str, ...]
    match_expression: str
    contract_version: int = LEXICAL_CONTRACT_VERSION
    unicode_profile: str = LEXICAL_UNICODE_PROFILE
    _factory_token: InitVar[object] = None

    def __post_init__(self, _factory_token: object) -> None:
        if _factory_token is not _QUERY_FACTORY_TOKEN:
            raise RetrievalContractError(
                "INVALID_COMPILED_QUERY",
                "lexical queries must be created by compile_lexical_query",
            )
        _validated_query_values(self)

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise _DerivedValueSerializationError

    def __getstate__(self) -> NoReturn:
        raise _DerivedValueSerializationError

    def __setstate__(self, _state: object) -> NoReturn:
        raise _DerivedValueSerializationError


@dataclass(frozen=True, slots=True)
class LexicalTokenStreams:
    """ASCII-only FTS5 token streams derived from exact untrusted content."""

    title: str
    body: str
    tags: str
    contract_version: int = LEXICAL_CONTRACT_VERSION
    unicode_profile: str = LEXICAL_UNICODE_PROFILE
    _factory_token: InitVar[object] = None

    def __post_init__(self, _factory_token: object) -> None:
        if _factory_token is not _STREAMS_FACTORY_TOKEN:
            raise RetrievalContractError(
                "INVALID_TOKEN_STREAMS",
                "lexical token streams must be derived from validated note content",
            )
        _validated_stream_values(self)

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise _DerivedValueSerializationError

    def __getstate__(self) -> NoReturn:
        raise _DerivedValueSerializationError

    def __setstate__(self, _state: object) -> NoReturn:
        raise _DerivedValueSerializationError


@dataclass(frozen=True, slots=True)
class LexicalScore:
    """Exact integer score and its reviewable field/phrase breakdown."""

    total: int
    title_term_frequency: int
    tag_term_frequency: int
    body_term_frequency: int
    title_phrase: bool
    tag_phrase: bool
    body_phrase: bool
    contract_version: int = LEXICAL_CONTRACT_VERSION
    unicode_profile: str = LEXICAL_UNICODE_PROFILE
    _factory_token: InitVar[object] = None

    def __post_init__(self, _factory_token: object) -> None:
        if _factory_token is not _SCORE_FACTORY_TOKEN:
            raise RetrievalContractError(
                "INVALID_LEXICAL_SCORE",
                "lexical scores must be produced by score_lexical_content",
            )

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise _DerivedValueSerializationError

    def __getstate__(self) -> NoReturn:
        raise _DerivedValueSerializationError

    def __setstate__(self, _state: object) -> NoReturn:
        raise _DerivedValueSerializationError


def compile_lexical_query(value: str) -> LexicalQuery:
    """Normalize bounded text into unique terms and a literal FTS5 AND query."""

    if type(value) is not str:
        raise RetrievalContractError("INVALID_QUERY_TYPE", "the query must be exact text")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise RetrievalContractError(
            "INVALID_QUERY_UNICODE",
            "the query must contain only Unicode scalar values",
        ) from None
    if len(value) > MAX_QUERY_CODEPOINTS or len(raw) > MAX_QUERY_BYTES:
        raise RetrievalContractError(
            "QUERY_TOO_LARGE",
            "the query exceeds its input bound",
        )

    normalized = _normalize(value)
    normalized_bytes = normalized.encode("utf-8")
    if (
        len(normalized) > MAX_NORMALIZED_QUERY_CODEPOINTS
        or len(normalized_bytes) > MAX_NORMALIZED_QUERY_BYTES
    ):
        raise RetrievalContractError(
            "QUERY_NORMALIZATION_TOO_LARGE",
            "the normalized query exceeds its expansion bound",
        )

    terms = tuple(dict.fromkeys(_tokenize_normalized(normalized)))
    if not terms:
        raise RetrievalContractError(
            "EMPTY_QUERY",
            "the query must contain at least one letter or number",
        )
    if len(terms) > MAX_QUERY_TERMS:
        raise RetrievalContractError(
            "TOO_MANY_QUERY_TERMS",
            "the query contains too many distinct terms",
        )
    if any(
        len(term) > MAX_QUERY_TERM_CODEPOINTS or len(term.encode("utf-8")) > MAX_QUERY_TERM_BYTES
        for term in terms
    ):
        raise RetrievalContractError(
            "QUERY_TERM_TOO_LARGE",
            "one normalized query term exceeds its bound",
        )

    encoded = tuple(_encode_term(term) for term in terms)
    return LexicalQuery(
        terms=terms,
        encoded_terms=encoded,
        match_expression=" AND ".join(f'"{term}"' for term in encoded),
        _factory_token=_QUERY_FACTORY_TOKEN,
    )


def lexical_token_streams(content: NoteContent) -> LexicalTokenStreams:
    """Encode content terms so FTS5 sees only inert lowercase ASCII tokens."""

    if type(content) is not NoteContent:
        raise _ExactValueTypeError("content", "NoteContent")
    title, body, tag_groups = _validated_content_token_groups(content)
    title_stream, body_stream, tags_stream = _candidate_stream_values(title, body, tag_groups)
    return LexicalTokenStreams(
        title=title_stream,
        body=body_stream,
        tags=tags_stream,
        _factory_token=_STREAMS_FACTORY_TOKEN,
    )


def score_lexical_content(
    query: LexicalQuery,
    content: NoteContent,
) -> LexicalScore | None:
    """Return the exact all-terms score, or ``None`` when one term is absent."""

    if type(query) is not LexicalQuery:
        raise _ExactValueTypeError("query", "LexicalQuery")
    if type(content) is not NoteContent:
        raise _ExactValueTypeError("content", "NoteContent")

    terms = _validated_query_values(query)
    title, body, tag_groups = _validated_content_token_groups(content)
    _candidate_stream_values(title, body, tag_groups)
    tags = tuple(token for group in tag_groups for token in group)
    available = frozenset(chain(title, body, tags))
    if any(term not in available for term in terms):
        return None

    title_frequency = _capped_frequency(title, terms)
    tag_frequency = _capped_frequency(tags, terms)
    body_frequency = _capped_frequency(body, terms)
    title_phrase = _contains_phrase(title, terms)
    tag_phrase = any(_contains_phrase(group, terms) for group in tag_groups)
    body_phrase = _contains_phrase(body, terms)
    total = (
        title_frequency * _TITLE_TERM_WEIGHT
        + tag_frequency * _TAG_TERM_WEIGHT
        + body_frequency * _BODY_TERM_WEIGHT
        + int(title_phrase) * _TITLE_PHRASE_BONUS
        + int(tag_phrase) * _TAG_PHRASE_BONUS
        + int(body_phrase) * _BODY_PHRASE_BONUS
    )
    return LexicalScore(
        total=total,
        title_term_frequency=title_frequency,
        tag_term_frequency=tag_frequency,
        body_term_frequency=body_frequency,
        title_phrase=title_phrase,
        tag_phrase=tag_phrase,
        body_phrase=body_phrase,
        _factory_token=_SCORE_FACTORY_TOKEN,
    )


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())


def _validated_query_values(query: LexicalQuery) -> tuple[str, ...]:
    terms = getattr(query, "terms", _MISSING)
    encoded_terms = getattr(query, "encoded_terms", _MISSING)
    match_expression = getattr(query, "match_expression", _MISSING)
    contract_version = getattr(query, "contract_version", _MISSING)
    unicode_profile = getattr(query, "unicode_profile", _MISSING)
    if (
        type(terms) is not tuple
        or not 1 <= len(terms) <= MAX_QUERY_TERMS
        or any(type(term) is not str for term in terms)
        or len(set(terms)) != len(terms)
        or type(encoded_terms) is not tuple
        or len(encoded_terms) != len(terms)
        or any(type(term) is not str for term in encoded_terms)
        or any(len(term) > _MAX_ENCODED_TERM_LENGTH for term in encoded_terms)
        or type(match_expression) is not str
        or len(match_expression) > _MAX_MATCH_EXPRESSION_LENGTH
        or type(contract_version) is not int
        or contract_version != LEXICAL_CONTRACT_VERSION
    ):
        raise RetrievalContractError(
            "INVALID_COMPILED_QUERY",
            "the compiled lexical query has invalid state",
        )
    if type(unicode_profile) is not str or unicode_profile != LEXICAL_UNICODE_PROFILE:
        raise RetrievalContractError(
            "UNICODE_PROFILE_MISMATCH",
            "the compiled query uses a different Unicode profile",
        )

    typed_terms = tuple(terms)
    if any(not _is_queryable_term(term) for term in typed_terms):
        raise RetrievalContractError(
            "INVALID_COMPILED_QUERY",
            "the compiled lexical query has invalid terms",
        )
    if any(
        _normalize(term) != term or _tokenize_normalized(term) != (term,) for term in typed_terms
    ):
        raise RetrievalContractError(
            "INVALID_COMPILED_QUERY",
            "the compiled lexical query has invalid terms",
        )
    canonical_encoded = tuple(_encode_term(term) for term in typed_terms)
    canonical_expression = " AND ".join(f'"{term}"' for term in canonical_encoded)
    if encoded_terms != canonical_encoded or match_expression != canonical_expression:
        raise RetrievalContractError(
            "INVALID_COMPILED_QUERY",
            "the compiled lexical query is not canonical",
        )
    return typed_terms


def _validated_content_token_groups(
    content: NoteContent,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, ...], ...]]:
    title, body, tags = _validated_content_values(content)
    normalized_values: list[str] = []
    normalized_codepoints = 0
    normalized_bytes = 0
    for value in (title, body, *tags):
        normalized = _normalize(value)
        normalized_values.append(normalized)
        normalized_codepoints += len(normalized)
        normalized_bytes += len(normalized.encode("utf-8"))
        if (
            normalized_codepoints > MAX_NORMALIZED_CONTENT_CODEPOINTS
            or normalized_bytes > MAX_NORMALIZED_CONTENT_BYTES
        ):
            raise RetrievalContractError(
                "CONTENT_NORMALIZATION_TOO_LARGE",
                "normalized note content exceeds the retrieval bound",
            )

    title_tokens = _tokenize_normalized(normalized_values[0])
    body_tokens = _tokenize_normalized(normalized_values[1])
    tag_groups = tuple(_tokenize_normalized(value) for value in normalized_values[2:])
    return title_tokens, body_tokens, tag_groups


def _candidate_stream_values(
    title: tuple[str, ...],
    body: tuple[str, ...],
    tag_groups: tuple[tuple[str, ...], ...],
) -> tuple[str, str, str]:
    title_terms = _deduplicated_queryable_terms(title)
    body_terms = _deduplicated_queryable_terms(body)
    tag_terms = _deduplicated_queryable_terms(
        tuple(token for group in tag_groups for token in group)
    )
    used = 0
    title_stream, used = _encode_bounded_stream(title_terms, used)
    body_stream, used = _encode_bounded_stream(body_terms, used)
    tags_stream, _ = _encode_bounded_stream(tag_terms, used)
    return title_stream, body_stream, tags_stream


def _validated_stream_values(streams: LexicalTokenStreams) -> None:
    values = (
        getattr(streams, "title", _MISSING),
        getattr(streams, "body", _MISSING),
        getattr(streams, "tags", _MISSING),
    )
    contract_version = getattr(streams, "contract_version", _MISSING)
    unicode_profile = getattr(streams, "unicode_profile", _MISSING)
    if any(type(value) is not str for value in values):
        raise RetrievalContractError(
            "INVALID_TOKEN_STREAMS",
            "the lexical token streams have invalid state",
        )
    typed_values = cast(tuple[str, str, str], values)
    if (
        sum(len(value) for value in typed_values) > MAX_CONTENT_TOKEN_STREAM_BYTES
        or type(contract_version) is not int
        or contract_version != LEXICAL_CONTRACT_VERSION
    ):
        raise RetrievalContractError(
            "INVALID_TOKEN_STREAMS",
            "the lexical token streams have invalid state",
        )
    if type(unicode_profile) is not str or unicode_profile != LEXICAL_UNICODE_PROFILE:
        raise RetrievalContractError(
            "UNICODE_PROFILE_MISMATCH",
            "the token streams use a different Unicode profile",
        )
    if any(not _is_encoded_stream(value) for value in typed_values):
        raise RetrievalContractError(
            "INVALID_TOKEN_STREAMS",
            "the lexical token streams have invalid state",
        )


def _is_encoded_stream(value: str) -> bool:
    if not value:
        return True
    encoded_terms = value.split(" ")
    return all(
        len(term) >= _MIN_ENCODED_TERM_LENGTH
        and term.startswith("u")
        and len(term) % 2 == 1
        and set(term[1:]) <= _HEX_DIGITS
        for term in encoded_terms
    )


def _deduplicated_queryable_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(term for term in terms if _is_queryable_term(term)))


def _is_queryable_term(term: str) -> bool:
    if len(term) > MAX_QUERY_TERM_CODEPOINTS:
        return False
    try:
        encoded = term.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= MAX_QUERY_TERM_BYTES


def _tokenize_normalized(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    current: list[str] = []
    for character in value:
        category_prefix = unicodedata.category(character)[0]
        if category_prefix in {"L", "N"} or (category_prefix == "M" and current):
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current.clear()
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _encode_term(term: str) -> str:
    return "u" + term.encode("utf-8").hex()


def _encode_bounded_stream(terms: tuple[str, ...], used: int) -> tuple[str, int]:
    encoded_terms: list[str] = []
    for term in terms:
        encoded = _encode_term(term)
        added = len(encoded) + int(bool(encoded_terms))
        if used + added > MAX_CONTENT_TOKEN_STREAM_BYTES:
            raise RetrievalContractError(
                "CONTENT_TOKEN_STREAM_TOO_LARGE",
                "encoded note token streams exceed the retrieval bound",
            )
        encoded_terms.append(encoded)
        used += added
    return " ".join(encoded_terms), used


def _capped_frequency(tokens: tuple[str, ...], terms: tuple[str, ...]) -> int:
    counts = Counter(tokens)
    return sum(min(counts[term], MAX_TERM_FREQUENCY) for term in terms)


def _contains_phrase(tokens: tuple[str, ...], terms: tuple[str, ...]) -> bool:
    width = len(terms)
    return width > 1 and any(
        tokens[offset : offset + width] == terms for offset in range(len(tokens) - width + 1)
    )
