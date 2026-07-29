# RecallLedger source-bound visuals

These diagrams are generated from bounded, versioned JSON sources and are
checked against the current Python, packaging, and written contracts. They are
architecture and failure-model documentation. They are not runtime
screenshots, CLI captures, latency measurements, or benchmarks.

## Artifacts

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

- `sources/architecture-workflow.v1.json`
- `sources/transaction-output-retry.v1.json`

Every lane, node, edge, and note references a binding. Tests resolve those
bindings against the named Python AST definition, literal constant, installed
console-script entry, or exact named Markdown section. An exact visual-tree
allowlist prevents unbound JSON, Markdown, or SVG files from entering the
package. The sdist gate also checks normalized file modes, extracts a clean
source archive, imports the renderer there, and checks both committed SVG
files byte-for-byte. When a bound behavior changes, update the source claim,
regenerate the SVG, and keep the code and visual change in the same reviewable
commit.
