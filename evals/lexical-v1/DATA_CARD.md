# RecallLedger lexical evaluation v1

## Intended use

This frozen suite is a small, reviewable conformance evaluation for RecallLedger's deterministic lexical reference-search contract. It is intended to catch changes in query normalization, all-terms matching, field weighting, phrase bonuses, result order, and the published aggregate metrics.

## Dataset grain and scope

The suite contains eight synthetic current-head documents and twelve hand-authored queries for one logical tenant. Every query carries an explicit relevance grade for every document. Grades are independent source data rather than output copied from the scorer.

The corpus models lexical retrieval only. Revision replacement, tombstone visibility, tenant isolation, snapshot verification, and identifier tie-breaking remain covered by storage and contract tests rather than being mixed into these relevance metrics.

## Construction and provenance

The documents and queries were written specifically for this repository. They exercise the vocabulary used in its architecture and safety contracts without copying production notes or external datasets. Fixed synthetic timestamps provide deterministic secondary-sort inputs; this slice contains no equal-score outcome and therefore makes no tie-break quality claim.

`expected.v1.json` binds the exact canonical source-file digests and the lexical contract version. The Unicode profile is runtime-bound because the project's currently targeted Python 3.11 and 3.12 interpreters may expose different Unicode Character Database versions. The selected normalization examples are required to reproduce the exact frozen outcomes on every tested runtime.

## Relevance rubric

- `0` — irrelevant to the information need.
- `1` — marginally relevant context.
- `2` — relevant supporting material.
- `3` — highly relevant direct answer.

Each query contains exactly one judgment for each of the eight corpus documents. Missing judgments are invalid and never silently treated as zero.

## Covered cases

The suite covers single- and multi-term queries, conjunctive all-terms matching, title/tag/body weighting, tag/body phrase bonuses plus negative title-phrase cases, compatibility-width and case-fold normalization, literal `OR`, `NEAR`, and wildcard-shaped input that must remain inert data, an exact empty-result case, and a deliberately relevant semantic synonym that lexical matching cannot recover. Positive title-phrase behavior remains covered by the focused scorer tests.

The `remove note` query intentionally misses the relevant `tombstone-boundary` document. Keeping this failure visible prevents the fixture from implying semantic-search or embedding capabilities that the implementation does not have.

## Metric definitions

All published metrics use cutoff 5. A document is relevant when its grade is at least 1.

- Success@1 is the fraction of queries whose first hit is relevant.
- Macro Recall@5 is the arithmetic mean of each query's relevant hits in its first five results divided by that query's relevant-document count.
- Micro Recall@5 divides all relevant hits in the first five results by all relevant judgments across the suite.
- MRR@5 is the arithmetic mean of reciprocal rank for the first relevant hit at rank 1–5, or zero when none is returned.
- nDCG@5 uses gain `2^grade - 1` and discount `1 / log2(rank + 1)`, divided by the ideal DCG for the same query.
- Exact outcome rate compares compiled terms, complete matched-document order, integer score breakdowns, and total match counts with the frozen expected outcomes.
- Unexpected hit count includes any matched document absent from a frozen expected outcome.

Parts-per-million integers accompany exact numerators and denominators so committed JSON does not depend on floating-point serialization.

## Integrity and reproducibility

The JSON sources use strict UTF-8, sorted keys, compact separators, and exactly one trailing newline. Duplicate keys, non-finite values, floats, unknown fields, noncanonical bytes, incomplete judgments, and digest mismatches are invalid. The expected file stores SHA-256 digests of the exact canonical corpus and query bytes, while the check-only validator pins all three version 1 file digests. Any changed source must therefore use a new version instead of silently rebinding v1.

The repository validator reconstructs `NoteContent`, compiles each query through the production query contract, recomputes each score with the production scorer, and applies the documented deterministic order. It derives every aggregate numerator and denominator independently from the judgments and resulting order.

## Privacy and licensing

All text, identifiers, timestamps, and judgments are synthetic. The suite contains no user records, credentials, secrets, personal data, or telemetry. It is distributed under the repository's MIT license.

## Limitations and non-claims

This is a deliberately small synthetic conformance fixture, not a production benchmark. It does not measure latency, throughput, recall on real user traffic, semantic retrieval, embeddings, LLM answer quality, multilingual coverage, authentication, or persistent-index behavior. The frozen judgments were authored for regression visibility and are not evidence of external validity.

The corpus is too small for statistical significance. The aggregate values describe only these twelve disclosed queries. The known synonym miss is a product boundary, not a hidden failure. Every recovered query has an ideal relevance order, so MRR@5, macro Recall@5, and nDCG@5 co-move in this slice; their equal values are not three independent quality claims. Positive title-phrase and equal-score tie behavior remain focused contract-test concerns rather than benchmark claims.

## Versioning policy

Version 1 is immutable once published. Any substantive document, query, judgment, metric definition, or schema change creates a new versioned directory and new source digests. Typographical documentation corrections that do not change evaluation meaning may be made with an explicit reviewable commit.
