# Event contract v1

RecallLedger stores changes as tenant-scoped note events. This document
describes the implemented event contract. The separate SQLite foundation can
identify, migrate, and verify its storage format. Its atomic transition API now
derives and persists these envelopes without accepting a client-authored event
or timestamp.

## Identity and ownership

`tenant_id`, `note_id`, and `command_id` are opaque 128-bit random identifiers
with domain-specific prefixes. They are identifiers, not credentials or
authorization proofs. A future authenticated adapter will resolve the tenant
before calling storage; request bodies and retrieved note text must never
select a tenant.

Successor factories accept a previous `LedgerEvent` and do not accept tenant or
note identity again. This makes accidental identity drift harder in application
code. Durable storage also enforces tenant, note, revision, predecessor, command
idempotency, and head-projection constraints transactionally.

## Canonical envelope

The canonical representation is strict UTF-8 JSON with keys sorted by their
Unicode code points, `,` and `:` as separators, and no insignificant whitespace
or trailing newline. Serialization uses `ensure_ascii=False`: non-ASCII Unicode
scalar values are emitted directly as UTF-8, while quotation marks, reverse
solidus, and U+0000–U+001F use JSON escaping. The short escapes are `\"`, `\\`,
`\b`, `\t`, `\n`, `\f`, and `\r`; other controls use lowercase `\u00xx`.
Solidus is not escaped. Surrogates are rejected rather than escaped. Integers
use base-10 JSON syntax without leading zeroes; floats and non-standard
constants are forbidden. Inputs with duplicate or unknown keys, or inputs that
decode to an equivalent but byte-different envelope, are rejected.

The executable definition is equivalent to:

```python
json.dumps(
    envelope,
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8", errors="strict")
```

The closed schema means this definition does not depend on floating-point
formatting or arbitrary object serialization.

```json
{
  "command_id": "cmd_<32 lowercase hex digits>",
  "content": {
    "body": "exact untrusted text",
    "tags": ["exact", "ordered", "values"],
    "title": "exact untrusted text"
  },
  "event_hash": "sha256:<64 lowercase hex digits>",
  "kind": "note.created",
  "note_id": "nt_<32 lowercase hex digits>",
  "previous_event_hash": null,
  "recorded_at_us": 1785325000000000,
  "revision": 1,
  "schema_version": 1,
  "tenant_id": "tn_<32 lowercase hex digits>",
  "tombstone_reason": null
}
```

The example above shows the shape, not a valid checked envelope. The committed
golden chain in `tests/test_events.py::test_canonical_chain_round_trip_and_golden_hashes`
contains checked hashes and byte round trips for creation, revision, and
tombstone events.

Creation is revision 1 and has no predecessor. Revision and tombstone events
must have a predecessor and a revision greater than one. Tombstones contain a
bounded reason code but no title, body, or tags. Successor timestamps cannot
move backwards relative to their parent, and an immediate successor cannot
reuse its parent's command identifier. SQLite storage enforces command
idempotency across the full tenant boundary.

A tombstone is a logical projection instruction. It does not erase content
from earlier events, backups, replicas, process memory, or storage media. A
future retention and compaction layer is required before RecallLedger can make
any stronger deletion claim.

## Integrity relationship

`event_hash` is:

```text
sha256("recall-ledger:event:v1\0" || canonical_event_without_event_hash)
```

The domain separator prevents the same bytes from being confused with another
hash protocol. The predecessor hash makes mutations or reordering evident only
relative to an authenticated latest checkpoint or expected tip held outside
the ledger.

This is not a signature, message authentication code, authorization receipt,
timestamp proof, branch-selection rule, or protection from an attacker who can
replace both a ledger and its checkpoint. Trusting only the creation event is
insufficient: multiple descendants can form valid forks, and a verifier with
no expected latest tip cannot distinguish truncation from a shorter history.
The SQLite layer linearizes predecessor and revision updates transactionally,
but a future authenticated adapter must still define how latest checkpoints are
published, retained, and compared. The in-memory event factories accept
`recorded_at_us` so canonical chains, imports, and tests can be deterministic.
Public SQLite mutations do not: storage reads UTC microseconds after excluding
replay and clamps a successor to at least its verified parent's timestamp. This
is causal metadata, not a trusted timestamp proof.

## Text and resource policy

Note text is preserved exactly; RecallLedger does not normalize, case-fold, or
interpret it at ingestion. Bidirectional marks, terminal controls, Markdown,
HTML, and model-directed instructions remain untrusted data that output
adapters must encode for their surface.

The contract rejects non-scalar Unicode and bounds code points and UTF-8 bytes
before canonicalization. JSON integer lexemes are limited to 19 digits before
integer conversion, independent of the interpreter's process-wide integer
limit. Titles, bodies, tags, tag counts, the 256 KiB canonical envelope,
timestamps, signed-64-bit revisions, identifiers, JSON keys, and event kinds
are bounded or closed. Construction, successor derivation, and serialization
revalidate the complete event shape, canonical digest, content bounds, and
envelope size. Error messages and chained exceptions do not echo note text.
