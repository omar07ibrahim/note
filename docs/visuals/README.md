# RecallLedger source-bound visuals

This directory contains two evidence classes. The terminal panels are rendered
from a validated run of the console script installed from a clean-source wheel.
The architecture and failure diagrams are generated from bounded, versioned
JSON sources and checked against the current Python, packaging, and written
contracts. None of these artifacts is a latency measurement or benchmark.

## Artifacts

### Installed-wheel write, replay, and conflict

![Installed-wheel create, exact replay, revision conflict, and head proof](installed-wheel-write-replay.svg)

This is the normalized transcript of steps 1–5 from a real isolated run:
create revision 1, replay the exact command without another append, revise to
revision 2, reject a fresh stale command with `REVISION_CONFLICT` and exit 11,
then read the unchanged head.

### Installed-wheel paging and logical deletion

![Installed-wheel bounded history, JSONL continuation, tombstone, live get, and audit head](installed-wheel-history-tombstone.svg)

Steps 6–10 read bounded history as JSON and JSONL, append a content-free
tombstone at revision 3, prove that live `get` hides the note, and prove that
the audit `head` retains the linked terminal event.

The [canonical evidence document](evidence/installed-wheel-cli.v1.json)
records every normalized argv, stdout and stderr record, exit code, fixture
hash, verification assertion, source commit and tree, capture-input manifest,
source archive hash, builder version, wheel and `RECORD` hash, installed-file
digest, and normalized console-wrapper digest. Before normalization, the
capture contract validates exact argv and channel bytes, recomputes all event
hashes independently, checks cross-step state and pagination, and asks the
installed wheel to decode the raw event envelopes.

Only the run-specific note ID, three event digests, three storage-owned
microsecond timestamps, and temporary filesystem paths are normalized. The
tenant ID, command IDs, fixture content, operations, response fields, failure
code, retry guidance, and exit codes remain exact. The panels do not claim
authentication, physical deletion, immutable storage against its owner,
retrieval quality, or measured performance.

### Implemented architecture

![RecallLedger implemented architecture](architecture-workflow.svg)

Source-derived architecture of one installed operator CLI invocation: explicit
caller context, bounded input, command dispatch, the tenant-scoped
`SQLiteLedger` boundary, transaction-local event derivation, the durable event
log and head projection, verified reads, and machine-readable output. The
new-command path is distinct from the exact-replay bypass, which returns the
stored event after proof without another event append or head write.

The diagram does not claim authentication, search, model integration, physical
deletion, immutable storage against a database owner, or end-to-end stdout
delivery.

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

### Installed-wheel evidence

Evidence updates use two commits so the recorded source commit cannot
self-reference its own generated document. First commit and verify the runtime,
fixtures, and three evidence tools. From that clean implementation commit, run:

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
.venv/bin/python tools/capture_cli_evidence.py \
  --source-commit "$SOURCE_COMMIT" \
  --write
.venv/bin/python tools/render_cli_evidence.py --write
```

Then review and commit the JSON, both SVG files, and their documentation.
Rebuild the installed wheel and compare all three committed artifacts without
rewriting them:

```bash
.venv/bin/python tools/capture_cli_evidence.py --check
.venv/bin/python tools/render_cli_evidence.py --check
```

The installed RecallLedger runtime remains dependency-free; capture uses only
the pinned local build toolchain around it. It bounds Git archives, wheels,
`RECORD`, subprocess input and combined output, canonical JSON/JSONL, installed
files, and all document collections. It uses a private descriptor-pinned
workspace, sanitized environment, offline installation, no-follow reads and
atomic public-mode writes. Every subprocess receives its own session; timeout,
error, and normal completion clean up remaining group members.

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

## Source contract

The canonical sources are:

- `evidence/installed-wheel-cli.v1.json`
- `sources/architecture-workflow.v1.json`
- `sources/transaction-output-retry.v1.json`

The evidence document has its own closed cross-step contract. Every lane, node,
edge, and note in the two diagram sources references a binding. Tests resolve
those bindings against the named Python AST definition, literal constant,
installed console-script entry, or exact named Markdown section. An exact
visual-tree allowlist prevents unbound JSON, Markdown, or SVG files from
entering the package. The sdist gate also checks normalized file modes,
extracts a clean source archive, imports both renderers, and checks all four
committed SVG files byte-for-byte. When a bound behavior changes, update the
source or capture, regenerate its SVG, and keep the code and visual change in
reviewable commits.
