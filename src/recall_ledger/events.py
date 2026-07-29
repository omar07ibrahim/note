"""Bounded, canonical note-event envelopes.

This module deliberately contains no database or authorization adapter. It
defines the values accepted by the storage boundary and the only supported way
to advance one note's event chain.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Final, NewType, TypeAlias, cast

TenantId = NewType("TenantId", str)
NoteId = NewType("NoteId", str)
CommandId = NewType("CommandId", str)

SCHEMA_VERSION: Final = 1
MAX_EVENT_BYTES: Final = 262_144
MAX_REVISION: Final = (1 << 63) - 1
MAX_TITLE_CODEPOINTS: Final = 240
MAX_TITLE_BYTES: Final = 720
MAX_BODY_CODEPOINTS: Final = 32_768
MAX_BODY_BYTES: Final = 98_304
MAX_TAGS: Final = 16
MAX_TAG_CODEPOINTS: Final = 64
MAX_TAG_BYTES: Final = 192
MAX_RECORDED_AT_US: Final = 253_402_300_799_999_999
MAX_JSON_INTEGER_DIGITS: Final = 19

_HASH_DOMAIN: Final = b"recall-ledger:event:v1\x00"
_DIGEST_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SURROGATE_MIN: Final = 0xD800
_SURROGATE_MAX: Final = 0xDFFF
_MISSING: Final = object()
_ID_PATTERNS: Final = {
    "tenant_id": re.compile(r"tn_[0-9a-f]{32}\Z"),
    "note_id": re.compile(r"nt_[0-9a-f]{32}\Z"),
    "command_id": re.compile(r"cmd_[0-9a-f]{32}\Z"),
}


class EventKind(str, Enum):
    """The closed event vocabulary in schema version 1."""

    CREATED = "note.created"
    REVISED = "note.revised"
    TOMBSTONED = "note.tombstoned"


class TombstoneReason(str, Enum):
    """Bounded reason codes; no free-text deletion reason is retained."""

    USER_REQUEST = "user_request"
    RETENTION_POLICY = "retention_policy"
    ADMINISTRATIVE = "administrative"


class ContractViolation(ValueError):  # noqa: N818 - domain term used by the public API
    """A safe, machine-readable event-contract rejection."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NoteContent:
    """Exact untrusted note text with explicit storage bounds."""

    title: str
    body: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_content_fields(self)


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """One immutable, self-checking event in a note-local hash chain."""

    tenant_id: TenantId
    note_id: NoteId
    command_id: CommandId
    revision: int
    recorded_at_us: int
    kind: EventKind
    content: NoteContent | None
    tombstone_reason: TombstoneReason | None
    previous_event_hash: str | None
    event_hash: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validated_event(self)

    @classmethod
    def create(
        cls,
        *,
        tenant_id: TenantId,
        note_id: NoteId,
        command_id: CommandId,
        recorded_at_us: int,
        content: NoteContent,
    ) -> LedgerEvent:
        """Create the root event for a note."""

        _validate_identifier(tenant_id, "tenant_id")
        _validate_identifier(note_id, "note_id")
        _validate_identifier(command_id, "command_id")
        _validate_recorded_at(recorded_at_us)
        material: JsonObject = {
            "command_id": command_id,
            "content": _content_object(content),
            "kind": EventKind.CREATED.value,
            "note_id": note_id,
            "previous_event_hash": None,
            "recorded_at_us": recorded_at_us,
            "revision": 1,
            "schema_version": SCHEMA_VERSION,
            "tenant_id": tenant_id,
            "tombstone_reason": None,
        }
        return cls(
            tenant_id=tenant_id,
            note_id=note_id,
            command_id=command_id,
            revision=1,
            recorded_at_us=recorded_at_us,
            kind=EventKind.CREATED,
            content=content,
            tombstone_reason=None,
            previous_event_hash=None,
            event_hash=_digest(_canonical_bytes(material)),
        )

    def revise(
        self,
        *,
        command_id: CommandId,
        recorded_at_us: int,
        content: NoteContent,
    ) -> LedgerEvent:
        """Advance a live note without accepting tenant or note identity again."""

        return self._successor(
            command_id=command_id,
            recorded_at_us=recorded_at_us,
            kind=EventKind.REVISED,
            content=content,
            tombstone_reason=None,
        )

    def tombstone(
        self,
        *,
        command_id: CommandId,
        recorded_at_us: int,
        reason: TombstoneReason,
    ) -> LedgerEvent:
        """Advance a live note with a content-free logical deletion event."""

        return self._successor(
            command_id=command_id,
            recorded_at_us=recorded_at_us,
            kind=EventKind.TOMBSTONED,
            content=None,
            tombstone_reason=reason,
        )

    def _successor(
        self,
        *,
        command_id: CommandId,
        recorded_at_us: int,
        kind: EventKind,
        content: NoteContent | None,
        tombstone_reason: TombstoneReason | None,
    ) -> LedgerEvent:
        parent = _validated_event(self)
        if parent.kind is EventKind.TOMBSTONED:
            message = (
                "a tombstoned note cannot be revised"
                if kind is EventKind.REVISED
                else "a note can only be tombstoned once"
            )
            raise ContractViolation("NOTE_TOMBSTONED", message)
        _validate_identifier(command_id, "command_id")
        _validate_recorded_at(recorded_at_us)
        if recorded_at_us < parent.recorded_at_us:
            raise ContractViolation(
                "NON_MONOTONIC_TIME",
                "a successor timestamp cannot precede its parent",
            )
        if command_id == parent.command_id:
            raise ContractViolation(
                "DUPLICATE_COMMAND_ID",
                "a successor cannot reuse its parent's command identifier",
            )
        if parent.revision >= MAX_REVISION:
            raise ContractViolation(
                "REVISION_EXHAUSTED",
                "the note revision counter cannot advance",
            )
        if kind is EventKind.REVISED and (
            type(content) is not NoteContent or tombstone_reason is not None
        ):
            raise ContractViolation(
                "INVALID_REVISED_PAYLOAD",
                "a revision event requires content and no tombstone reason",
            )
        if kind is EventKind.TOMBSTONED and (
            content is not None or type(tombstone_reason) is not TombstoneReason
        ):
            raise ContractViolation(
                "INVALID_TOMBSTONE_PAYLOAD",
                "a tombstone requires a reason and cannot retain note content",
            )
        if kind not in (EventKind.REVISED, EventKind.TOMBSTONED):
            raise ContractViolation("INVALID_KIND", "a successor kind is not supported")
        revision = parent.revision + 1
        material: JsonObject = {
            "command_id": command_id,
            "content": _content_object(content),
            "kind": kind.value,
            "note_id": parent.note_id,
            "previous_event_hash": parent.event_hash,
            "recorded_at_us": recorded_at_us,
            "revision": revision,
            "schema_version": SCHEMA_VERSION,
            "tenant_id": parent.tenant_id,
            "tombstone_reason": (tombstone_reason.value if tombstone_reason is not None else None),
        }
        return LedgerEvent(
            tenant_id=parent.tenant_id,
            note_id=parent.note_id,
            command_id=command_id,
            revision=revision,
            recorded_at_us=recorded_at_us,
            kind=kind,
            content=content,
            tombstone_reason=tombstone_reason,
            previous_event_hash=parent.event_hash,
            event_hash=_digest(_canonical_bytes(material)),
        )

    def to_bytes(self) -> bytes:
        """Return the complete canonical envelope, without a trailing newline."""

        return _validated_event(self).envelope


JsonScalar: TypeAlias = str | int | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _ValidatedEvent:
    tenant_id: TenantId
    note_id: NoteId
    command_id: CommandId
    revision: int
    recorded_at_us: int
    kind: EventKind
    event_hash: str
    material: JsonObject
    envelope: bytes


@dataclass(frozen=True, slots=True)
class _EventFields:
    tenant_id: TenantId
    note_id: NoteId
    command_id: CommandId
    revision: int
    recorded_at_us: int
    kind: EventKind
    content: NoteContent | None
    tombstone_reason: TombstoneReason | None
    previous_event_hash: str | None
    event_hash: str


def new_tenant_id() -> TenantId:
    """Return an opaque 128-bit identifier; it is not an authorization token."""

    return TenantId(f"tn_{secrets.token_hex(16)}")


def new_note_id() -> NoteId:
    """Return an opaque 128-bit note identifier."""

    return NoteId(f"nt_{secrets.token_hex(16)}")


def new_command_id() -> CommandId:
    """Return an opaque 128-bit idempotency key."""

    return CommandId(f"cmd_{secrets.token_hex(16)}")


def decode_event(raw: bytes) -> LedgerEvent:
    """Decode one canonical, bounded envelope and verify its digest."""

    if type(raw) is not bytes:
        raise ContractViolation("INVALID_EVENT_BYTES", "event input must be exact bytes")
    if len(raw) > MAX_EVENT_BYTES:
        raise ContractViolation("EVENT_TOO_LARGE", "event input exceeds the byte limit")
    if not raw:
        raise ContractViolation("INVALID_EVENT_JSON", "event input is not canonical JSON")

    text: str | None = None
    with suppress(UnicodeDecodeError):
        text = raw.decode("utf-8", errors="strict")
    if text is None:
        raise ContractViolation("INVALID_EVENT_UTF8", "event input is not strict UTF-8")

    parsed: object | None = None
    parse_failed = False
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_json_float,
            parse_int=_parse_json_integer,
            parse_constant=_reject_json_constant,
        )
    except ContractViolation:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        parse_failed = True
    if parse_failed:
        raise ContractViolation("INVALID_EVENT_JSON", "event input is not canonical JSON")

    event = _event_from_object(parsed)
    if event.to_bytes() != raw:
        raise ContractViolation("NON_CANONICAL_EVENT", "event input is not in canonical form")
    return event


def _event_from_object(value: object) -> LedgerEvent:
    obj = _require_object(value, "event")
    _require_exact_keys(
        obj,
        {
            "command_id",
            "content",
            "event_hash",
            "kind",
            "note_id",
            "previous_event_hash",
            "recorded_at_us",
            "revision",
            "schema_version",
            "tenant_id",
            "tombstone_reason",
        },
        "event",
    )

    kind_value = _require_exact_str(obj["kind"], "kind")
    kind: EventKind | None = None
    with suppress(ValueError):
        kind = EventKind(kind_value)
    if kind is None:
        raise ContractViolation("INVALID_KIND", "event kind is not supported")

    content_value = obj["content"]
    content: NoteContent | None
    if content_value is None:
        content = None
    else:
        content_obj = _require_object(content_value, "content")
        _require_exact_keys(content_obj, {"body", "tags", "title"}, "content")
        tags_value = content_obj["tags"]
        if type(tags_value) is not list:
            raise ContractViolation("INVALID_TAGS", "tags must be a JSON array")
        if len(tags_value) > MAX_TAGS:
            raise ContractViolation("TOO_MANY_TAGS", "tag count exceeds the contract limit")
        tags = tuple(_require_exact_str(tag, "tag") for tag in tags_value)
        content = NoteContent(
            title=_require_exact_str(content_obj["title"], "title"),
            body=_require_exact_str(content_obj["body"], "body"),
            tags=tags,
        )

    reason_value = obj["tombstone_reason"]
    tombstone_reason: TombstoneReason | None
    if reason_value is None:
        tombstone_reason = None
    else:
        reason_text = _require_exact_str(reason_value, "tombstone_reason")
        tombstone_reason = None
        with suppress(ValueError):
            tombstone_reason = TombstoneReason(reason_text)
        if tombstone_reason is None:
            raise ContractViolation(
                "INVALID_TOMBSTONE_REASON",
                "tombstone reason is not supported",
            )

    previous_hash_value = obj["previous_event_hash"]
    previous_hash = (
        None
        if previous_hash_value is None
        else _require_exact_str(previous_hash_value, "previous_event_hash")
    )
    return LedgerEvent(
        tenant_id=TenantId(_require_exact_str(obj["tenant_id"], "tenant_id")),
        note_id=NoteId(_require_exact_str(obj["note_id"], "note_id")),
        command_id=CommandId(_require_exact_str(obj["command_id"], "command_id")),
        revision=_require_exact_int(obj["revision"], "revision"),
        recorded_at_us=_require_exact_int(obj["recorded_at_us"], "recorded_at_us"),
        kind=kind,
        content=content,
        tombstone_reason=tombstone_reason,
        previous_event_hash=previous_hash,
        event_hash=_require_exact_str(obj["event_hash"], "event_hash"),
        schema_version=_require_exact_int(obj["schema_version"], "schema_version"),
    )


def _validated_event(event: LedgerEvent) -> _ValidatedEvent:
    fields = _validated_event_fields(event)
    _validate_event_payload(fields)
    material = _material_from_fields(fields)
    expected = _digest(_canonical_bytes(material))
    if not secrets.compare_digest(fields.event_hash, expected):
        raise ContractViolation(
            "EVENT_HASH_MISMATCH",
            "event hash does not match canonical data",
        )

    envelope_object = dict(material)
    envelope_object["event_hash"] = fields.event_hash
    envelope = _canonical_bytes(envelope_object)
    if len(envelope) > MAX_EVENT_BYTES:
        raise ContractViolation("EVENT_TOO_LARGE", "canonical event exceeds the byte limit")
    return _ValidatedEvent(
        tenant_id=fields.tenant_id,
        note_id=fields.note_id,
        command_id=fields.command_id,
        revision=fields.revision,
        recorded_at_us=fields.recorded_at_us,
        kind=fields.kind,
        event_hash=fields.event_hash,
        material=material,
        envelope=envelope,
    )


def _validated_event_fields(event: LedgerEvent) -> _EventFields:
    schema_value = _event_field(event, "schema_version")
    tenant_value = _event_field(event, "tenant_id")
    note_value = _event_field(event, "note_id")
    command_value = _event_field(event, "command_id")
    revision_value = _event_field(event, "revision")
    recorded_at_value = _event_field(event, "recorded_at_us")
    kind_value = _event_field(event, "kind")
    content_value = _event_field(event, "content")
    reason_value = _event_field(event, "tombstone_reason")
    previous_hash_value = _event_field(event, "previous_event_hash")
    event_hash_value = _event_field(event, "event_hash")
    if type(schema_value) is not int or schema_value != SCHEMA_VERSION:
        raise ContractViolation("INVALID_SCHEMA_VERSION", "schema version is not supported")
    _validate_identifier(tenant_value, "tenant_id")
    _validate_identifier(note_value, "note_id")
    _validate_identifier(command_value, "command_id")
    if type(revision_value) is not int or not 1 <= revision_value <= MAX_REVISION:
        raise ContractViolation("INVALID_REVISION", "revision must be a bounded positive integer")
    _validate_recorded_at(recorded_at_value)
    if type(kind_value) is not EventKind:
        raise ContractViolation("INVALID_KIND", "event kind must be an EventKind")
    if type(event_hash_value) is not str or _DIGEST_PATTERN.fullmatch(event_hash_value) is None:
        raise ContractViolation("INVALID_EVENT_HASH", "event hash is not canonical sha256 text")
    return _EventFields(
        tenant_id=TenantId(cast(str, tenant_value)),
        note_id=NoteId(cast(str, note_value)),
        command_id=CommandId(cast(str, command_value)),
        revision=revision_value,
        recorded_at_us=cast(int, recorded_at_value),
        kind=kind_value,
        content=cast(NoteContent | None, content_value),
        tombstone_reason=cast(TombstoneReason | None, reason_value),
        previous_event_hash=cast(str | None, previous_hash_value),
        event_hash=event_hash_value,
    )


def _validate_event_payload(fields: _EventFields) -> None:
    if fields.kind is EventKind.CREATED:
        if fields.revision != 1 or fields.previous_event_hash is not None:
            raise ContractViolation(
                "INVALID_CHAIN_ROOT",
                "a creation event must be revision one without a predecessor",
            )
        if type(fields.content) is not NoteContent or fields.tombstone_reason is not None:
            raise ContractViolation(
                "INVALID_CREATED_PAYLOAD",
                "a creation event requires content and no tombstone reason",
            )
    elif fields.revision == 1 or not _is_digest(fields.previous_event_hash):
        raise ContractViolation(
            "INVALID_CHAIN_LINK",
            "a successor event requires a canonical predecessor hash",
        )
    elif fields.kind is EventKind.REVISED:
        if type(fields.content) is not NoteContent or fields.tombstone_reason is not None:
            raise ContractViolation(
                "INVALID_REVISED_PAYLOAD",
                "a revision event requires content and no tombstone reason",
            )
    elif fields.content is not None or type(fields.tombstone_reason) is not TombstoneReason:
        raise ContractViolation(
            "INVALID_TOMBSTONE_PAYLOAD",
            "a tombstone requires a reason and cannot retain note content",
        )


def _material_from_fields(fields: _EventFields) -> JsonObject:
    return {
        "command_id": fields.command_id,
        "content": _content_object(fields.content),
        "kind": fields.kind.value,
        "note_id": fields.note_id,
        "previous_event_hash": fields.previous_event_hash,
        "recorded_at_us": fields.recorded_at_us,
        "revision": fields.revision,
        "schema_version": SCHEMA_VERSION,
        "tenant_id": fields.tenant_id,
        "tombstone_reason": (
            fields.tombstone_reason.value if fields.tombstone_reason is not None else None
        ),
    }


def _event_field(event: LedgerEvent, field: str) -> object:
    value = getattr(event, field, _MISSING)
    if value is _MISSING:
        raise ContractViolation("INVALID_EVENT_STATE", "event state is incomplete")
    return value


def _validate_identifier(value: object, field: str) -> None:
    if type(value) is not str or _ID_PATTERNS[field].fullmatch(value) is None:
        raise ContractViolation(
            "INVALID_IDENTIFIER",
            f"{field} is not a canonical opaque identifier",
        )


def _validate_recorded_at(value: object) -> None:
    if type(value) is not int or value < 0 or value > MAX_RECORDED_AT_US:
        raise ContractViolation(
            "INVALID_RECORDED_AT",
            "recorded timestamp must be a bounded non-negative exact integer",
        )


def _validate_text(
    value: object,
    *,
    field: str,
    max_codepoints: int,
    max_bytes: int,
    allow_blank: bool,
) -> str:
    if type(value) is not str:
        raise ContractViolation("INVALID_TEXT", f"{field} must be an exact string")
    if len(value) > max_codepoints:
        raise ContractViolation("TEXT_TOO_LARGE", f"{field} exceeds the code-point limit")
    if not allow_blank and not value.strip():
        raise ContractViolation("BLANK_TEXT", f"{field} cannot be blank")
    if any(_SURROGATE_MIN <= ord(character) <= _SURROGATE_MAX for character in value):
        raise ContractViolation("INVALID_UNICODE", f"{field} must contain Unicode scalar values")
    if len(value.encode("utf-8")) > max_bytes:
        raise ContractViolation("TEXT_TOO_LARGE", f"{field} exceeds the UTF-8 byte limit")
    return value


def _validate_content_fields(content: NoteContent) -> None:
    _validated_content_values(content)


def _validated_content_values(content: NoteContent) -> tuple[str, str, tuple[str, ...]]:
    missing = object()
    title_value = getattr(content, "title", missing)
    body_value = getattr(content, "body", missing)
    tags = getattr(content, "tags", missing)
    title = _validate_text(
        title_value,
        field="title",
        max_codepoints=MAX_TITLE_CODEPOINTS,
        max_bytes=MAX_TITLE_BYTES,
        allow_blank=False,
    )
    body = _validate_text(
        body_value,
        field="body",
        max_codepoints=MAX_BODY_CODEPOINTS,
        max_bytes=MAX_BODY_BYTES,
        allow_blank=True,
    )
    if type(tags) is not tuple:
        raise ContractViolation("INVALID_TAGS", "tags must be an exact tuple of strings")
    if len(tags) > MAX_TAGS:
        raise ContractViolation("TOO_MANY_TAGS", "tag count exceeds the contract limit")

    seen: set[str] = set()
    validated_tags: list[str] = []
    for tag in tags:
        validated_tag = _validate_text(
            tag,
            field="tag",
            max_codepoints=MAX_TAG_CODEPOINTS,
            max_bytes=MAX_TAG_BYTES,
            allow_blank=False,
        )
        if tag in seen:
            raise ContractViolation("DUPLICATE_TAG", "tags must be unique by exact value")
        seen.add(tag)
        validated_tags.append(validated_tag)
    return title, body, tuple(validated_tags)


def _event_material(event: LedgerEvent) -> JsonObject:
    return dict(_validated_event(event).material)


def _content_object(content: NoteContent | None) -> JsonObject | None:
    if content is None:
        return None
    if type(content) is not NoteContent:
        raise ContractViolation("INVALID_TEXT", "content must be a NoteContent value")
    title, body, tags = _validated_content_values(content)
    return {"body": body, "tags": list(tags), "title": title}


def _canonical_bytes(value: JsonObject) -> bytes:
    canonical_text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload: bytes | None = None
    with suppress(UnicodeEncodeError):
        payload = canonical_text.encode("utf-8")
    if payload is None:
        raise ContractViolation(
            "INVALID_UNICODE",
            "canonical data must contain Unicode scalar values",
        )
    return payload


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(_HASH_DOMAIN + payload).hexdigest()}"


def _is_digest(value: object) -> bool:
    return type(value) is str and _DIGEST_PATTERN.fullmatch(value) is not None


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ContractViolation("DUPLICATE_JSON_KEY", "event JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> JsonValue:
    raise ContractViolation("INVALID_EVENT_JSON", "event input is not canonical JSON")


def _reject_json_float(_value: str) -> JsonValue:
    raise ContractViolation("INVALID_EVENT_JSON", "event input is not canonical JSON")


def _parse_json_integer(value: str) -> int:
    digit_count = len(value) - (1 if value.startswith("-") else 0)
    if digit_count > MAX_JSON_INTEGER_DIGITS:
        raise ContractViolation("INVALID_EVENT_JSON", "event input is not canonical JSON")
    return int(value)


def _require_object(value: object, field: str) -> JsonObject:
    if type(value) is not dict:
        raise ContractViolation("INVALID_OBJECT", f"{field} must be a JSON object")
    return cast(JsonObject, value)


def _require_exact_keys(value: JsonObject, expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise ContractViolation("INVALID_OBJECT_KEYS", f"{field} contains missing or unknown keys")


def _require_exact_str(value: JsonValue, field: str) -> str:
    if type(value) is not str:
        raise ContractViolation("INVALID_FIELD_TYPE", f"{field} must be an exact string")
    return value


def _require_exact_int(value: JsonValue, field: str) -> int:
    if type(value) is not int:
        raise ContractViolation("INVALID_FIELD_TYPE", f"{field} must be an exact integer")
    return value
