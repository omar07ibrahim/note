from __future__ import annotations

import pickle
import unicodedata

import pytest

import recall_ledger.retrieval as retrieval_module
from recall_ledger import (
    LexicalQuery,
    LexicalScore,
    LexicalTokenStreams,
    NoteContent,
    RetrievalContractError,
    compile_lexical_query,
    lexical_token_streams,
    score_lexical_content,
)
from recall_ledger.events import MAX_BODY_CODEPOINTS, ContractViolation
from recall_ledger.retrieval import (
    LEXICAL_CONTRACT_VERSION,
    LEXICAL_UNICODE_PROFILE,
    MAX_CONTENT_TOKEN_STREAM_BYTES,
    MAX_NORMALIZED_CONTENT_BYTES,
    MAX_NORMALIZED_CONTENT_CODEPOINTS,
    MAX_NORMALIZED_QUERY_CODEPOINTS,
    MAX_QUERY_CODEPOINTS,
    MAX_QUERY_TERM_CODEPOINTS,
    MAX_QUERY_TERMS,
)


def test_query_normalization_deduplicates_terms_and_encodes_inert_fts_literals() -> None:
    query = compile_lexical_query("  \uff23afé CAFÉ, Straße + tensor_42 tensor_42 ")

    assert query.terms == ("café", "strasse", "tensor", "42")
    assert query.encoded_terms == tuple("u" + term.encode().hex() for term in query.terms)
    assert query.match_expression == " AND ".join(f'"{term}"' for term in query.encoded_terms)
    assert query.contract_version == LEXICAL_CONTRACT_VERSION
    assert query.unicode_profile == LEXICAL_UNICODE_PROFILE
    assert query.unicode_profile.endswith(f"ucd={unicodedata.unidata_version}")


def test_fts_operator_text_is_encoded_as_inert_literal_terms() -> None:
    query = compile_lexical_query('alpha OR * NEAR(beta) -gamma "delta" column:value')

    assert query.terms == (
        "alpha",
        "or",
        "near",
        "beta",
        "gamma",
        "delta",
        "column",
        "value",
    )
    assert query.match_expression == " AND ".join(
        f'"u{term.encode().hex()}"' for term in query.terms
    )
    assert all(character not in query.match_expression for character in "()*:-")


@pytest.mark.parametrize(
    ("value", "code"),
    (
        (object(), "INVALID_QUERY_TYPE"),
        ("\ud800", "INVALID_QUERY_UNICODE"),
        ("a" * (MAX_QUERY_CODEPOINTS + 1), "QUERY_TOO_LARGE"),
        ("𐀀" * MAX_QUERY_CODEPOINTS, "QUERY_TOO_LARGE"),
        ("<> _ - \x00 \u200b", "EMPTY_QUERY"),
        (" ".join(f"term{index}" for index in range(MAX_QUERY_TERMS + 1)), "TOO_MANY_QUERY_TERMS"),
        ("a" * (MAX_QUERY_TERM_CODEPOINTS + 1), "QUERY_TERM_TOO_LARGE"),
    ),
)
def test_query_contract_rejects_invalid_or_over_limit_input(value: object, code: str) -> None:
    with pytest.raises(RetrievalContractError) as captured:
        compile_lexical_query(value)  # type: ignore[arg-type]

    assert captured.value.code == code
    assert str(captured.value)


def test_query_normalization_has_an_independent_expansion_bound() -> None:
    expanding_character = "ﷺ"
    assert len(expanding_character) < MAX_NORMALIZED_QUERY_CODEPOINTS

    with pytest.raises(RetrievalContractError) as captured:
        compile_lexical_query(expanding_character * 100)

    assert captured.value.code == "QUERY_NORMALIZATION_TOO_LARGE"


def test_marks_attach_only_after_a_letter_or_number() -> None:
    query = compile_lexical_query("\u0301A\u0301 9\u20e3")

    assert query.terms == ("á", "9⃣")


def test_content_token_streams_preserve_fields_and_deduplicate_candidate_terms() -> None:
    streams = lexical_token_streams(
        NoteContent(
            title="Alpha alpha",
            body="Beta / Straße",
            tags=("Café", "tensor_42"),
        )
    )

    assert streams.title == "u616c706861"
    assert streams.body == "u62657461 u73747261737365"
    assert streams.tags == "u636166c3a9 u74656e736f72 u3432"
    assert streams.contract_version == LEXICAL_CONTRACT_VERSION
    assert streams.unicode_profile == LEXICAL_UNICODE_PROFILE


def test_unqueryable_long_content_terms_are_omitted_from_candidate_streams() -> None:
    long_term = "x" * (MAX_QUERY_TERM_CODEPOINTS + 1)
    streams = lexical_token_streams(NoteContent("Searchable", long_term))

    assert streams.title == "u73656172636861626c65"
    assert streams.body == ""
    assert streams.tags == ""


def test_utf8_oversized_content_term_is_omitted_and_cannot_be_queried() -> None:
    long_term = "𐀀" * 49
    streams = lexical_token_streams(NoteContent("Searchable", long_term))

    assert streams.body == ""
    with pytest.raises(RetrievalContractError) as captured:
        compile_lexical_query(long_term)
    assert captured.value.code == "QUERY_TERM_TOO_LARGE"


def test_derived_content_expansion_and_stream_size_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pathological = NoteContent("Bounded", "ﷺ" * MAX_BODY_CODEPOINTS)
    streams = lexical_token_streams(pathological)
    assert sum(len(value) for value in (streams.title, streams.body, streams.tags)) <= (
        MAX_CONTENT_TOKEN_STREAM_BYTES
    )

    monkeypatch.setattr(
        "recall_ledger.retrieval.MAX_NORMALIZED_CONTENT_CODEPOINTS",
        1,
    )
    with pytest.raises(RetrievalContractError) as normalized:
        score_lexical_content(compile_lexical_query("bounded"), NoteContent("Bounded", ""))
    assert normalized.value.code == "CONTENT_NORMALIZATION_TOO_LARGE"

    monkeypatch.setattr(
        retrieval_module,
        "MAX_NORMALIZED_CONTENT_CODEPOINTS",
        MAX_NORMALIZED_CONTENT_CODEPOINTS,
    )
    monkeypatch.setattr(retrieval_module, "MAX_NORMALIZED_CONTENT_BYTES", 1)
    with pytest.raises(RetrievalContractError) as normalized_bytes:
        score_lexical_content(compile_lexical_query("bounded"), NoteContent("Bounded", ""))
    assert normalized_bytes.value.code == "CONTENT_NORMALIZATION_TOO_LARGE"

    monkeypatch.setattr(
        retrieval_module,
        "MAX_NORMALIZED_CONTENT_BYTES",
        MAX_NORMALIZED_CONTENT_BYTES,
    )
    monkeypatch.setattr(
        retrieval_module,
        "MAX_CONTENT_TOKEN_STREAM_BYTES",
        1,
    )
    with pytest.raises(RetrievalContractError) as stream:
        score_lexical_content(compile_lexical_query("bounded"), NoteContent("Bounded", ""))
    assert stream.value.code == "CONTENT_TOKEN_STREAM_TOO_LARGE"
    assert MAX_CONTENT_TOKEN_STREAM_BYTES > 1


def test_candidate_stream_aggregate_bound_accepts_exact_size_and_rejects_one_less(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = NoteContent("alpha beta", "gamma", ("delta",))
    expected = lexical_token_streams(content)
    exact_size = sum(len(value) for value in (expected.title, expected.body, expected.tags))

    monkeypatch.setattr(retrieval_module, "MAX_CONTENT_TOKEN_STREAM_BYTES", exact_size)
    assert lexical_token_streams(content) == expected

    monkeypatch.setattr(retrieval_module, "MAX_CONTENT_TOKEN_STREAM_BYTES", exact_size - 1)
    with pytest.raises(RetrievalContractError) as captured:
        lexical_token_streams(content)
    assert captured.value.code == "CONTENT_TOKEN_STREAM_TOO_LARGE"


def test_integer_score_caps_frequency_and_exposes_phrase_breakdown() -> None:
    query = compile_lexical_query("alpha beta")
    content = NoteContent(
        title="alpha alpha alpha alpha beta",
        body="alpha beta alpha",
        tags=("alpha beta",),
    )

    score = score_lexical_content(query, content)

    assert score is not None
    assert score.total == 121
    assert score.title_term_frequency == 4
    assert score.tag_term_frequency == 2
    assert score.body_term_frequency == 3
    assert score.title_phrase
    assert score.tag_phrase
    assert score.body_phrase
    assert score.contract_version == LEXICAL_CONTRACT_VERSION
    assert score.unicode_profile == LEXICAL_UNICODE_PROFILE


def test_all_query_terms_are_required_and_tag_phrases_do_not_cross_tags() -> None:
    query = compile_lexical_query("alpha beta")

    assert score_lexical_content(query, NoteContent("alpha", "only")) is None
    score = score_lexical_content(
        query,
        NoteContent("alpha", "beta", tags=("alpha", "beta")),
    )
    assert score is not None
    assert not score.title_phrase
    assert not score.tag_phrase
    assert not score.body_phrase
    assert score.total == 31


def test_single_term_never_receives_a_phrase_bonus() -> None:
    score = score_lexical_content(
        compile_lexical_query("alpha alpha"),
        NoteContent("alpha alpha alpha alpha", "", ()),
    )

    assert score is not None
    assert score.total == 36
    assert not score.title_phrase
    assert not score.tag_phrase
    assert not score.body_phrase


def test_unqueryable_long_term_remains_a_phrase_barrier_for_the_oracle() -> None:
    long_term = "x" * (MAX_QUERY_TERM_CODEPOINTS + 1)
    score = score_lexical_content(
        compile_lexical_query("alpha beta"),
        NoteContent("Context", f"alpha {long_term} beta"),
    )

    assert score is not None
    assert not score.body_phrase
    assert score.total == 6


@pytest.mark.parametrize(
    ("query_text", "content"),
    (
        ("Café STRASSE", NoteContent("ＣＡＦÉ", "Straße")),
        ("alpha beta", NoteContent("Context", "alpha beta alpha")),
        ("tensor 42", NoteContent("Index", "", ("tensor_42",))),
    ),
)
def test_candidate_streams_contain_every_term_accepted_by_the_oracle(
    query_text: str,
    content: NoteContent,
) -> None:
    query = compile_lexical_query(query_text)
    assert score_lexical_content(query, content) is not None

    streams = lexical_token_streams(content)
    candidates = set(f"{streams.title} {streams.body} {streams.tags}".split())

    assert set(query.encoded_terms) <= candidates


def test_exact_public_value_types_are_required() -> None:
    query = compile_lexical_query("alpha")
    content = NoteContent("alpha", "body")

    with pytest.raises(TypeError, match="exact NoteContent"):
        lexical_token_streams(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exact LexicalQuery"):
        score_lexical_content(object(), content)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exact NoteContent"):
        score_lexical_content(query, object())  # type: ignore[arg-type]


def test_public_consumers_revalidate_mutated_query_and_content_state() -> None:
    query = compile_lexical_query("alpha")
    object.__setattr__(query, "terms", ())
    object.__setattr__(query, "encoded_terms", ())
    object.__setattr__(query, "match_expression", '" OR *')

    with pytest.raises(RetrievalContractError) as invalid_query:
        score_lexical_content(query, NoteContent("unrelated", "content"))
    assert invalid_query.value.code == "INVALID_COMPILED_QUERY"

    content = NoteContent("safe", "content")
    object.__setattr__(content, "body", "private-prefix\ud800private-suffix")
    with pytest.raises(ContractViolation) as invalid_content:
        lexical_token_streams(content)
    assert invalid_content.value.code == "INVALID_UNICODE"
    assert "private-prefix" not in str(invalid_content.value)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("encoded_terms", ("u00",)),
        ("encoded_terms", ("u" * 10_000,)),
        ("encoded_terms", ()),
        ("encoded_terms", (object(),)),
        ("match_expression", '"u616c706861" OR *'),
        ("match_expression", "x" * 10_000),
        ("contract_version", 2),
        ("terms", ("x" * (MAX_QUERY_TERM_CODEPOINTS + 1),)),
        ("terms", ("\ud800",)),
        ("terms", ("\uff21",)),
        ("unicode_profile", "nfkc-casefold-nfkc+lnm-tokenizer-v1;ucd=0.0.0"),
        ("unicode_profile", False),
    ),
)
def test_scoring_rejects_noncanonical_compiled_query_state(field: str, value: object) -> None:
    query = compile_lexical_query("alpha")
    object.__setattr__(query, field, value)

    with pytest.raises(RetrievalContractError) as captured:
        score_lexical_content(query, NoteContent("alpha", ""))

    expected = (
        "UNICODE_PROFILE_MISMATCH" if field == "unicode_profile" else "INVALID_COMPILED_QUERY"
    )
    assert captured.value.code == expected


def test_query_compiled_under_a_stale_runtime_profile_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = compile_lexical_query("alpha")
    monkeypatch.setattr(
        retrieval_module,
        "LEXICAL_UNICODE_PROFILE",
        "nfkc-casefold-nfkc+lnm-tokenizer-v1;ucd=future",
    )

    with pytest.raises(RetrievalContractError) as captured:
        score_lexical_content(query, NoteContent("alpha", ""))

    assert captured.value.code == "UNICODE_PROFILE_MISMATCH"


def test_query_with_missing_profile_is_rejected() -> None:
    query = compile_lexical_query("alpha")
    object.__delattr__(query, "unicode_profile")

    with pytest.raises(RetrievalContractError) as captured:
        score_lexical_content(query, NoteContent("alpha", ""))

    assert captured.value.code == "UNICODE_PROFILE_MISMATCH"


def test_scoring_uses_normalized_unicode_equivalence() -> None:
    query = compile_lexical_query("Café STRASSE")
    score = score_lexical_content(
        query,
        NoteContent("ＣＡＦÉ", "Straße"),
    )

    assert score is not None
    assert score.total == 15


def test_compiled_query_cannot_be_forged_through_the_public_value_constructor() -> None:
    with pytest.raises(RetrievalContractError) as captured:
        LexicalQuery(
            terms=("alpha",),
            encoded_terms=("u616c706861",),
            match_expression='"u616c706861"',
        )

    assert captured.value.code == "INVALID_COMPILED_QUERY"


def test_other_derived_values_require_their_factories() -> None:
    with pytest.raises(RetrievalContractError) as streams:
        LexicalTokenStreams(title='" OR *', body="", tags="")
    assert streams.value.code == "INVALID_TOKEN_STREAMS"

    with pytest.raises(RetrievalContractError) as score:
        LexicalScore(
            total=0,
            title_term_frequency=0,
            tag_term_frequency=0,
            body_term_frequency=0,
            title_phrase=False,
            tag_phrase=False,
            body_phrase=False,
        )
    assert score.value.code == "INVALID_LEXICAL_SCORE"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("title", object(), "INVALID_TOKEN_STREAMS"),
        ("title", '" OR *', "INVALID_TOKEN_STREAMS"),
        ("contract_version", 2, "INVALID_TOKEN_STREAMS"),
        (
            "unicode_profile",
            "nfkc-casefold-nfkc+lnm-tokenizer-v1;ucd=0.0.0",
            "UNICODE_PROFILE_MISMATCH",
        ),
    ),
)
def test_token_stream_state_is_revalidated(
    field: str,
    value: object,
    code: str,
) -> None:
    streams = lexical_token_streams(NoteContent("alpha", "body"))
    object.__setattr__(streams, field, value)

    with pytest.raises(RetrievalContractError) as captured:
        streams.__post_init__(retrieval_module._STREAMS_FACTORY_TOKEN)

    assert captured.value.code == code


def test_token_stream_state_rechecks_the_aggregate_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = lexical_token_streams(NoteContent("alpha", "body"))
    monkeypatch.setattr(retrieval_module, "MAX_CONTENT_TOKEN_STREAM_BYTES", 1)

    with pytest.raises(RetrievalContractError) as captured:
        streams.__post_init__(retrieval_module._STREAMS_FACTORY_TOKEN)

    assert captured.value.code == "INVALID_TOKEN_STREAMS"


@pytest.mark.parametrize(
    "derived",
    (
        compile_lexical_query("alpha"),
        lexical_token_streams(NoteContent("alpha", "body")),
        score_lexical_content(compile_lexical_query("alpha"), NoteContent("alpha", "body")),
    ),
)
def test_derived_values_reject_pickle_and_state_restoration(derived: object) -> None:
    assert derived is not None
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(derived)
    with pytest.raises(TypeError, match="cannot be serialized or restored"):
        derived.__getstate__()
    with pytest.raises(TypeError, match="cannot be serialized or restored"):
        derived.__setstate__({})  # type: ignore[attr-defined]
