# Contributing to RecallLedger

RecallLedger is a local-first, tenant-isolated event ledger. Contributions
should preserve explicit trust boundaries, deterministic behavior, and
source-backed evidence.

The project is available under the [MIT license](LICENSE). Do not contribute
code, fixtures, models, fonts, or data unless you have the right to license
them on compatible terms.

## Development setup

Use Linux/POSIX with Python 3.11 or 3.12:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation --editable .
```

Run the same quality gates as CI:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests tools
python tools/render_visuals.py --check
python tools/render_cli_evidence.py --check
python tools/lexical_eval_contract.py
python tools/render_lexical_eval_visuals.py --check
python tools/capture_cli_evidence.py --check-portable-runtime
python -m build --no-isolation
```

The test suite enforces 100% branch coverage.

## Storage and retrieval changes

- Keep tenant identity in trusted caller context and in every storage,
  uniqueness, replay, and retrieval boundary.
- Preserve bounded inputs, canonical events, optimistic revisions, exact
  command replay, content-free tombstones, and fail-closed uncertain states.
- Treat note text, queries, filenames, event envelopes, and model output as
  untrusted.
- Add cross-tenant, malformed-input, resource-limit, rollback, and recovery
  tests for every new path.
- Do not introduce a network or model adapter without explicit authentication,
  retention, transport, logging, deletion, and prompt-boundary documentation.

## Evidence changes

Visual evidence is executable review material, not decoration.

- Use only deterministic synthetic fixtures; never use a real ledger, private
  note, user identifier, token, hostname, email address, or absolute host path.
- Do not hand-edit generated SVG, PNG, GIF, JSON evidence, or manifests.
- Regenerate through the narrow tool for the affected evidence class and run
  its read-only check.
- Inspect the actual assets at original size and review GIF frame order,
  duration, and loop behavior.
- Keep temporary candidates private until independent review. Adopt hosted
  bytes only when source commit/tree, archive inventory, modes, hashes,
  renderer inputs, and privacy scans agree.
- Commit source, output, provenance, and documentation changes together when
  the executable workflow changes.

## Pull requests

Keep history linear and give each commit one meaningful responsibility. Explain
the trust-boundary effect, tests run, evidence changed, migration impact, and
nonclaims. Never commit local databases, WAL files, exports, screenshots of
real notes, or credentials.

Report vulnerabilities through a
[private advisory](https://github.com/omar07ibrahim/note/security/advisories/new),
not a public issue or pull request.
