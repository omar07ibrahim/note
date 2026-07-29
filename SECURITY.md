# Security Policy

## Supported state

RecallLedger is in phase 2a. The current tree implements an in-memory event
contract and a versioned SQLite open/migration foundation, but no durable note
mutation/query API or network-facing application. The historical shared-state
Telegram notes bot is unsupported and must not be deployed.

Canonical event parsing, text bounds, strict identifier forms, content-free
tombstones, note-local hash transitions, secure fixed-name file preflight,
cooperative migration locking, exact schema checks, and the defensive
connection profile are implemented. Atomic note transitions, authorization,
indexing, backup, adapter, and erasure controls below remain requirements for
future layers rather than current claims. See
[docs/storage-boundary.md](docs/storage-boundary.md) for the exact SQLite
deployment assumptions and non-claims.

The storage module currently supports Linux/POSIX deployments only. Close all
ledger connections before `fork`; a child that accidentally inherits one must
immediately `exec` or call `os._exit` without using or finalizing it.

## Primary trust boundaries

Every stored note belongs to exactly one immutable tenant. Tenant identity must
come from an authenticated adapter context, never from a request body, note
payload, callback string, search query, model output, or client-supplied event.
Storage queries and uniqueness constraints must include tenant identity, and
tests must attempt cross-tenant enumeration, search, mutation, and deletion.

Note titles, bodies, tags, attachment metadata, queries, imported events,
retrieval results, and model output are untrusted text. They may contain HTML,
Markdown, terminal controls, bidirectional marks, zero-width characters, CSV
formulas, SQL-like text, or instructions aimed at an LLM. Each output surface
must escape or visibly encode content for that surface.

Retrieved note text is evidence, not executable instruction. Prompt assembly
must keep application instructions, tool results, and retrieved content in
separate typed boundaries. Citations must bind a result to its tenant, note
revision, source event, index version, and retrieval score without exposing
another tenant's identifiers.

## Data and model handling

Local database, WAL, index, embedding, backup, export, benchmark, screenshot,
and recording files may all contain sensitive text or linkable metadata. They
must be excluded from source control by default. Synthetic fixtures must not be
derived by lightly editing real user notes.

No note content may leave the local process by default. Any future remote model
or embedding provider must be an explicit opt-in adapter with documented
retention, transport, logging, consent, and deletion behavior. Provider output
must not bypass tenant authorization or storage invariants.

Secrets must come from a managed runtime channel, never source files, command
arguments, callback payloads, logs, generated artifacts, or screenshots.
Development placeholders are not credentials and must not be presented as a
working configuration.

## Resource, storage, and deletion controls

Implementations must bound input bytes, Unicode code points, records, tags,
attachments, query terms, result counts, event replay, index growth, and model
context. SQLite access must use parameterized statements, explicit
transactions, migrations, busy-timeout policy, and durable failure handling.

Destructive actions require authorization at execution time, a scoped target,
and an explicit confirmation that cannot be replayed for another tenant or
revision. A delete operation must distinguish logical tombstoning, active
projection removal, index removal, backup retention, and cryptographic or
physical erasure. The project must not claim more deletion than it can prove.

Event and receipt hashes establish integrity relationships only when a verifier
has an authenticated latest checkpoint or expected tip. An authenticated
creation event alone still permits valid-looking forks and truncation. Hashes
do not select the authoritative branch, provide storage linearizability, act as
signatures, or prove that an event was authorized. A durable implementation
must combine transactional predecessor/revision constraints with an
independently authenticated checkpoint policy.

## Reporting a vulnerability

Use GitHub private vulnerability reporting when it is available. Do not place a
credential, private note, database, tenant identifier, exploit payload, or
personal data in a public issue.

Include the affected revision, trust boundary, minimal synthetic reproduction,
observed impact, and any proposed mitigation. Reports will be assessed before
public disclosure.
