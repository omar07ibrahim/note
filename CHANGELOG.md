# Changelog

All notable changes to RecallLedger are documented here.

## [Unreleased]

No unreleased changes.

## [0.1.0] - 2026-08-10

### Added

- A bounded, canonical event contract and versioned SQLite storage boundary.
- Atomic tenant-scoped create, revise, tombstone, head, history, and exact
  command-replay operations.
- A dependency-free installed operator CLI with canonical JSON/JSONL output,
  explicit exit codes, bounded stdin/file content, and optimistic revision
  checks.
- Deterministic tenant-local lexical retrieval, a frozen conformance suite, and
  a fail-closed audit that compares SQLite FTS5 candidates with the reference
  scorer.
- Real installed-wheel CLI evidence, a complete transcript PNG, a five-phase
  GIF, source-bound architecture and retry diagrams, and frozen retrieval
  evaluation figures.
- Hash-locked Python 3.11/3.12 CI, 100% branch coverage, reproducible media
  recapture, package checks, and extended CodeQL analysis.
- Security policy, third-party notices, contribution guidance, and private
  vulnerability reporting.

### Security

- Cross-tenant access fails closed and tenant identity remains explicit trusted
  caller context.
- Private ledger artifacts and evidence temporaries are excluded from public
  staging.
- Uncertain transaction settlement poisons the connection instead of claiming
  success or safe retry.

### Removed

- The unsupported historical shared-state Telegram notes bot from the current
  source tree.
