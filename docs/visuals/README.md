# RecallLedger source-bound visuals

This directory contains three evidence classes. Installed-wheel evidence
includes complete raster, ordered motion, and focused vector projections of one
validated console-script run from a clean-source wheel. Evaluation figures are
computed from one frozen, hash-bound lexical suite. Architecture and failure
diagrams are generated from bounded, versioned JSON sources and checked against
current Python, packaging, and written contracts. None of these artifacts is a
latency measurement or a production benchmark.

## Artifacts

### Complete installed-wheel transcript

![Complete normalized ten-command installed-wheel transcript](installed-wheel-cli.png)

The 1920px PNG shows all ten normalized commands and every stdout, stderr, and
exit record from the verified run. It is deterministic text rendering from
synthetic fixtures, not an OS-terminal screenshot.

### Five-phase installed-wheel workflow

![Five-phase installed-wheel workflow](installed-wheel-workflow.gif)

Five full-canvas frames cover each verified step exactly once: create/replay,
revise/stale rejection, head/history page one, history continuation/tombstone,
and hidden live read/retained audit head. The timing is presentation-only; this
is a deliberate workflow playback, not measured execution or an incident.

### Installed-wheel write, replay, and conflict

![Installed-wheel create, exact replay, revision conflict, and head proof](installed-wheel-write-replay.svg)

This focused vector panel covers steps 1–5: create revision 1, replay the exact
command without another append, revise to revision 2, reject a fresh stale
command with `REVISION_CONFLICT` and exit 11, then read the unchanged head.

### Installed-wheel paging and logical deletion

![Installed-wheel bounded history, JSONL continuation, tombstone, live get, and audit head](installed-wheel-history-tombstone.svg)

The second vector panel covers steps 6–10: bounded history as JSON and JSONL, a
content-free revision 3 tombstone, a live `get` that hides the note, and an
audit `head` that retains the linked terminal event.

The [canonical evidence document](evidence/installed-wheel-cli.v1.json) records
every normalized argv, channel record, exit, fixture hash, verification
assertion, source commit/tree, capture-input manifest, source archive, builder,
wheel, `RECORD`, and installed-file digest. The
[generated manifest](installed-wheel-media.manifest.json) binds all six hosted
outputs and five renderer inputs without self-including. The separate
[adoption record](evidence/installed-wheel-media.adoption.json) preserves the
reviewed run, artifact, archive digest, entry hashes/modes, and honest review
boundary; it is not a signature or proof of authorship.

Before normalization, the capture contract validates exact argv and channel
bytes, recomputes all event hashes independently, checks cross-step state and
pagination, and asks the installed wheel to decode raw event envelopes. Only
the run-specific note ID, three event digests, three storage-owned microsecond
timestamps, and temporary paths are normalized. No view claims authentication,
physical deletion, owner-resistant immutability, retrieval quality, or measured
performance.

### Frozen lexical conformance scorecard

![Frozen lexical conformance scorecard](lexical-search-eval-summary.svg)

Seven equal-footprint cards expose exact numerators, denominators, and
fixed-point percentages from the validated twelve-query suite. There is no bar
length encoding across unlike metrics. The warning panel preserves the known
`remove note` synonym miss and explains why the co-moving macro metrics are not
independent evidence.

### Complete query-by-document matrix

![Frozen lexical query-by-document matrix](lexical-search-query-matrix.svg)

All 96 judged cells are present. Fill encodes rank only within one query; raw
integer scores are printed as labels and are never compared across queries.
Gold outlines and `g1`–`g3` badges encode relevance without relying on color,
and the strongly relevant missed document is marked explicitly.

### Single-query score decomposition

![Frozen q01 lexical score decomposition](lexical-search-ranking-breakdown.svg)

The four hits for `q01-retrieval` share one zero baseline. Labeled title, tag,
and body segments expose the production integer components; phrase bonuses are
zero for all four hits. This figure does not generalize raw scores to other
queries.

The three figures consume only the validated
[`lexical-v1` suite](../../evals/lexical-v1/DATA_CARD.md). Their roots carry the
full corpus, query, and expected-outcome SHA-256 values. They do not claim
semantic retrieval, representative traffic, statistical uncertainty, or
latency.

### Bounded reference-search correctness path

![RecallLedger bounded reference-search correctness path](reference-search-workflow.svg)

This focused source-derived diagram follows a query through normalization,
one deferred SQLite snapshot, complete bounded tenant-head inventory, event
orphan proof, canonical head reconciliation, live-content accounting, full-set
scoring, sorting, and only then top-K with citations. Every stage binds to a
named implementation symbol or the storage contract. It does not represent an
authorization check, persistent index, signature, or measured latency.

### Implemented architecture

![RecallLedger implemented architecture](architecture-workflow.svg)

Source-derived architecture of one installed operator CLI invocation: explicit
caller context, bounded content/query input, command dispatch, the tenant-scoped
`SQLiteLedger` boundary, transaction-local event derivation, the durable event
log and head projection, verified reads and reference search, and
machine-readable output. The new-command path is distinct from the exact-replay
bypass, which returns the stored event after proof without another event append
or head write.

The diagram does not claim authentication, a persistent search index, model
integration, physical deletion, immutable storage against a database owner, or
end-to-end stdout delivery.

### Failure and retry state machine

![RecallLedger write settlement and retry states](transaction-output-retry.svg)

Source-derived failure model separating SQLite transaction uncertainty, state
and busy errors, settled stdout failure, and the outer stderr delivery
boundary. State exit 11 distinguishes revision inspection, idempotency stop,
and no-retry missing or tombstoned outcomes instead of presenting one
universal action. Its phase names, exit codes, and code-specific retry guidance
are bound to the current implementation. It does not represent measured
failure rates or a recorded terminal session.

## Regeneration

### Installed-wheel evidence and media

Installed-wheel evidence uses a two-stage, non-self-referential workflow. The
first commit freezes runtime code, fixtures, capture/render tools, the hosted CI
contract, and exact visual-only dependency lock. CI then checks out that commit
explicitly, builds and installs its wheel in isolation, captures the ten-command
scenario, renders the exact six-file bundle twice, proves both bundles are
byte-identical, and uploads the first bundle.

A separate review verifies the hosted archive inventory, modes, hashes, source
and renderer provenance, privacy boundary, PNG, all GIF frame structures, the
first GIF frame visually, and both self-contained SVGs. Only then does a second
commit adopt the exact hosted bytes plus an independent adoption record. Later
CI reads the recorded source commit/tree, repeats the isolated capture and
render, and compares all six generated files byte-for-byte with the adoption.

To rerender the already captured source locally on exact CPython 3.12.3, keep
Pillow outside the runtime environment and use an isolated output directory:

```bash
python3.12 -m venv .visual-venv
.visual-venv/bin/python -m pip install --require-hashes -r requirements-visuals.lock
MEDIA_OUT="$(mktemp -d)"
.visual-venv/bin/python tools/render_cli_media.py \
  --write --output-directory "$MEDIA_OUT"
.visual-venv/bin/python tools/render_cli_media.py \
  --check --output-directory "$MEDIA_OUT"
cmp "$MEDIA_OUT/installed-wheel-cli.v1.json" \
  docs/visuals/evidence/installed-wheel-cli.v1.json
for name in installed-wheel-write-replay.svg \
  installed-wheel-history-tombstone.svg installed-wheel-cli.png \
  installed-wheel-workflow.gif installed-wheel-media.manifest.json
do
  cmp "$MEDIA_OUT/$name" "docs/visuals/$name"
done
```

The renderer requires CPython 3.12.3, Pillow 12.3.0 from the one hash-locked
Linux wheel, and Pillow's embedded Aileron Regular font. It emits the evidence
JSON, both SVGs, PNG, GIF, and acyclic manifest; writes are atomic 0644 regular
files. The installed RecallLedger runtime remains dependency-free. The hosted
job is authoritative for recapture because it reconstructs the recorded source
commit rather than treating adopted files as their own source.

For the standard-library evidence and vector checks, run:

```bash
.venv/bin/python tools/capture_cli_evidence.py --check
.venv/bin/python tools/render_cli_evidence.py --check
```

Byte-exact capture intentionally records Python, pip, SQLite, builder, source
archive, wheel, `RECORD`, installed files, wrapper, argv/channels, and all
verification results. Portable-runtime CI permits only the four documented
builder/installation runtime-version leaves and the wheel ZIP-envelope digest
to vary; the semantic and installed-file contracts remain exact.

### Architecture and failure diagrams

From the repository root, regenerate the SVG files with:

```bash
.venv/bin/python tools/render_visuals.py --write
```

Check that committed artifacts are byte-for-byte current without rewriting
them:

```bash
.venv/bin/python tools/render_visuals.py --check
```

The renderer uses only the Python standard library. Input and freshness checks
use bounded prefix reads, per-file and aggregate byte limits, a source-count
and directory-entry limit, and bounded output comparisons. The closed schema
rejects duplicate JSON keys, oversized integer lexemes, floats, unknown fields,
Unicode controls and noncharacters, unresolved references, reserved or
cross-kind SVG ID collisions, unsupported variants, repository-escaping
binding paths, and unreachable nodes from the declared entry.

Directory components for sources, bindings, and outputs are opened without
following symbolic links, and source or binding inputs must be regular files.
Writes use an exclusive regular temporary file in the destination directory
followed by atomic replacement; pre-existing output symlinks, hardlinks, and
special files are rejected. Freshness checks also reject unexpected SVG
artifacts instead of silently accepting an orphan generated file.

Layout validation rejects out-of-bounds geometry, fallback-font text overflow,
overlapping content, detached edge labels, routes through unrelated content,
edge crossings or overlaps, malformed orthogonal routes, and endpoints that
do not leave and enter the correct node boundaries. Tests probe the text bound
against a real DejaVu Sans Bold glyph and enforce label clearance plus text and
non-text contrast gates. Output is deterministic: it contains no generation
timestamp, host path, random identifier, remote font, script, or external
image.

### Lexical evaluation figures

Regenerate all three frozen-suite figures from the repository root:

```bash
.venv/bin/python tools/render_lexical_eval_visuals.py --write
```

Check their committed bytes without rewriting them:

```bash
.venv/bin/python tools/lexical_eval_contract.py
.venv/bin/python tools/render_lexical_eval_visuals.py --check
```

The renderer validates the canonical suite once, then re-reads each bounded
regular source without following links and requires the exact validated digest
before projecting it. Atomic writes reject symbolic links, hard links, special
files, unknown output names, and oversized SVG. The matrix contains the full
judgment grid; the scorecard avoids incomparable length encoding; the breakdown
uses only one query and one zero baseline. Outputs are deterministic and
self-contained, with visible source hashes, accessibility labels, and no
external assets.

## Source contract

The canonical sources are:

- `evidence/installed-wheel-cli.v1.json`
- `sources/architecture-workflow.v1.json`
- `sources/reference-search-workflow.v1.json`
- `sources/transaction-output-retry.v1.json`
- `../../evals/lexical-v1/corpus.v1.json`
- `../../evals/lexical-v1/queries.v1.json`
- `../../evals/lexical-v1/expected.v1.json`

The generated `installed-wheel-media.manifest.json` and reviewed
`evidence/installed-wheel-media.adoption.json` are provenance records, not new
runtime claims or self-authenticating attestations.

The evidence document has its own closed cross-step contract. Every lane, node,
edge, and note in the three diagram sources references a binding. Tests resolve
those bindings against the named Python AST definition, literal constant,
installed console-script entry, or exact named Markdown section. An exact
visual-tree allowlist prevents unbound JSON, Markdown, or SVG files from
entering the package. The sdist gate also checks normalized file modes,
extracts a clean source archive, imports all three renderers, and checks all
eight committed SVG files byte-for-byte. When a bound behavior changes, update
the source or capture, regenerate its evidence, and keep contract and adoption
changes in separate reviewable commits.
