#!/usr/bin/env python3
"""Render deterministic terminal transcripts from validated CLI evidence JSON."""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
import stat
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn, cast
from xml.sax.saxutils import escape

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import cli_evidence_contract as contract  # noqa: E402

SOURCE_PATH: Final = ROOT / "docs" / "visuals" / "evidence" / "installed-wheel-cli.v1.json"
OUTPUT_DIRECTORY: Final = ROOT / "docs" / "visuals"
OUTPUT_NAMES: Final = (
    "installed-wheel-write-replay.svg",
    "installed-wheel-history-tombstone.svg",
)
PANEL_SPECS: Final = (
    (
        "Installed wheel · write, replay, and conflict",
        "Steps 1-5: append, idempotent replay, optimistic revision, stale rejection, head proof",
        range(5),
    ),
    (
        "Installed wheel · paging and logical deletion",
        "Steps 6-10: bounded history, JSONL continuation, tombstone, live-read and audit views",
        range(5, 10),
    ),
)

CANVAS_WIDTH: Final = 1600
SIDE_MARGIN: Final = 48
TERMINAL_TOP: Final = 150
TERMINAL_INSET: Final = 28
LINE_HEIGHT: Final = 24
FONT_SIZE: Final = 15
WRAP_COLUMNS: Final = 142
FOOTER_HEIGHT: Final = 92
MAX_SVG_BYTES: Final = 524_288
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)


class TerminalRenderError(ValueError):
    """The evidence cannot be rendered or the output path is unsafe."""


def _fail(message: str) -> NoReturn:
    raise TerminalRenderError(message)


def _read_source(path: Path = SOURCE_PATH) -> bytes:
    try:
        descriptor = os.open(path, _READ_FLAGS)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            _fail("CLI evidence source must be one regular single-link file")
        payload = bytearray()
        with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as stream:
            while len(payload) <= contract.MAX_CHANNEL_BYTES:
                chunk = stream.read(min(65_536, contract.MAX_CHANNEL_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
    except TerminalRenderError:
        raise
    except OSError:
        _fail("CLI evidence source is unavailable or unsafe")
    if len(payload) > contract.MAX_CHANNEL_BYTES:
        _fail("CLI evidence source exceeds the byte limit")
    return bytes(payload)


def _wrap(prefix: str, value: str, *, kind: str) -> list[tuple[str, str]]:
    available = WRAP_COLUMNS - len(prefix)
    chunks = textwrap.wrap(
        value,
        width=max(20, available),
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
        replace_whitespace=False,
    )
    if not chunks:
        chunks = [""]
    lines = [(f"{prefix}{chunks[0]}", kind)]
    continuation = " " * len(prefix)
    lines.extend((f"{continuation}{chunk}", kind) for chunk in chunks[1:])
    return lines


def _text_array(value: contract.JsonValue, context: str) -> list[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _fail(f"{context} must be a text array")
    return cast(list[str], value)


def _record_array(value: contract.JsonValue, context: str) -> list[contract.JsonObject]:
    if type(value) is not list or any(type(item) is not dict for item in value):
        _fail(f"{context} must be an object array")
    return cast(list[contract.JsonObject], value)


def transcript_lines(
    document: contract.JsonObject,
    step_indexes: range,
) -> tuple[tuple[str, str], ...]:
    """Project normalized structured records into a complete terminal transcript."""

    contract.validate_evidence_document(document)
    steps_value = document["steps"]
    if type(steps_value) is not list:
        _fail("validated evidence steps unexpectedly changed type")
    steps = cast(list[contract.JsonObject], steps_value)
    lines: list[tuple[str, str]] = []
    for index in step_indexes:
        step = steps[index]
        argv = _text_array(step["argv"], "step argv")
        lines.extend(_wrap("$ ", shlex.join(argv), kind="command"))
        stdout = _record_array(step["stdout"], "step stdout")
        stderr = _record_array(step["stderr"], "step stderr")
        for record in stdout:
            serialized = contract.canonical_json_bytes(record).decode("ascii").rstrip("\n")
            lines.extend(_wrap("stdout | ", serialized, kind="stdout"))
        for record in stderr:
            serialized = contract.canonical_json_bytes(record).decode("ascii").rstrip("\n")
            lines.extend(_wrap("stderr | ", serialized, kind="stderr"))
        exit_code = step["exit_code"]
        if type(exit_code) is not int:
            _fail("validated evidence exit code unexpectedly changed type")
        lines.append((f"exit   | {exit_code}", "exit-error" if exit_code else "exit"))
        if index != step_indexes.stop - 1:
            lines.append(("", "blank"))
    return tuple(lines)


def _svg_text(line: str, kind: str, *, y: int) -> str:
    css_class = {
        "blank": "terminal-text",
        "command": "command",
        "exit": "exit",
        "exit-error": "exit-error",
        "stderr": "stderr",
        "stdout": "stdout",
    }[kind]
    return (
        f'    <text class="{css_class}" x="{SIDE_MARGIN + TERMINAL_INSET}" '
        f'y="{y}">{escape(line)}</text>'
    )


def _render_panel(
    document: contract.JsonObject,
    *,
    title: str,
    subtitle: str,
    indexes: range,
    slug: str,
) -> bytes:
    lines = transcript_lines(document, indexes)
    terminal_height = 78 + len(lines) * LINE_HEIGHT + TERMINAL_INSET
    canvas_height = TERMINAL_TOP + terminal_height + FOOTER_HEIGHT
    provenance = cast(contract.JsonObject, document["provenance"])
    wheel = cast(contract.JsonObject, provenance["wheel"])
    source_commit = cast(str, provenance["source_commit"])
    wheel_digest = cast(str, wheel["sha256"])
    description = (
        f"{title}. A deterministic terminal rendering of normalized structured records "
        f"captured from the installed RecallLedger wheel. {contract.NORMALIZATION_DISCLOSURE}"
    )
    caption_lines = textwrap.wrap(
        contract.NORMALIZATION_DISCLOSURE,
        width=132,
        break_long_words=False,
        break_on_hyphens=False,
    )
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_WIDTH}" '
            f'height="{canvas_height}" viewBox="0 0 {CANVAS_WIDTH} {canvas_height}" '
            f'role="img" aria-labelledby="{slug}-title {slug}-description">'
        ),
        f'  <title id="{slug}-title">{escape(title)}</title>',
        f'  <desc id="{slug}-description">{escape(description)}</desc>',
        "  <style>",
        "    .title { fill: #142033; font: 700 32px system-ui, sans-serif; }",
        "    .subtitle { fill: #405166; font: 500 17px system-ui, sans-serif; }",
        "    .provenance { fill: #526276; font: 500 13px ui-monospace, monospace; }",
        (
            f"    .terminal-text, .command, .stdout, .stderr, .exit, .exit-error "
            f"{{ font: 500 {FONT_SIZE}px ui-monospace, SFMono-Regular, "
            "Menlo, Consolas, monospace; }"
        ),
        "    .command { fill: #d7f9ef; font-weight: 700; }",
        "    .stdout { fill: #e6edf7; }",
        "    .stderr { fill: #ffc9cf; }",
        "    .exit { fill: #8ee6b7; font-weight: 700; }",
        "    .exit-error { fill: #ff9fab; font-weight: 700; }",
        "    .caption { fill: #405166; font: 500 13px system-ui, sans-serif; }",
        "  </style>",
        f'  <rect width="{CANVAS_WIDTH}" height="{canvas_height}" fill="#f7f9fc"/>',
        f'  <text class="title" x="{SIDE_MARGIN}" y="54">{escape(title)}</text>',
        f'  <text class="subtitle" x="{SIDE_MARGIN}" y="86">{escape(subtitle)}</text>',
        (
            f'  <text class="provenance" x="{SIDE_MARGIN}" y="116">'
            f"source {escape(source_commit[:12])} · wheel sha256 {escape(wheel_digest[:16])}…"
            "</text>"
        ),
        (
            f'  <rect x="{SIDE_MARGIN}" y="{TERMINAL_TOP}" '
            f'width="{CANVAS_WIDTH - 2 * SIDE_MARGIN}" height="{terminal_height}" '
            'rx="14" fill="#111827" stroke="#45566d" stroke-width="2"/>'
        ),
        f'  <circle cx="{SIDE_MARGIN + 24}" cy="{TERMINAL_TOP + 24}" r="7" fill="#ff6978"/>',
        f'  <circle cx="{SIDE_MARGIN + 48}" cy="{TERMINAL_TOP + 24}" r="7" fill="#f7c65d"/>',
        f'  <circle cx="{SIDE_MARGIN + 72}" cy="{TERMINAL_TOP + 24}" r="7" fill="#65d39b"/>',
        (
            f'  <text class="provenance" x="{SIDE_MARGIN + 98}" y="{TERMINAL_TOP + 29}" '
            'fill="#b9c5d6">installed-venv · synthetic tenant · canonical JSON</text>'
        ),
    ]
    line_y = TERMINAL_TOP + 72
    for line, kind in lines:
        body.append(_svg_text(line, kind, y=line_y))
        line_y += LINE_HEIGHT
    caption_y = TERMINAL_TOP + terminal_height + 35
    for line in caption_lines:
        body.append(
            f'  <text class="caption" x="{SIDE_MARGIN}" y="{caption_y}">{escape(line)}</text>'
        )
        caption_y += 19
    body.append("</svg>")
    payload = ("\n".join(body) + "\n").encode("utf-8")
    if len(payload) > MAX_SVG_BYTES:
        _fail("terminal evidence SVG exceeds the byte limit")
    return payload


def render_evidence(document: contract.JsonObject) -> dict[str, bytes]:
    """Render the two canonical terminal evidence panels."""

    contract.validate_evidence_document(document)
    rendered: dict[str, bytes] = {}
    for filename, (title, subtitle, indexes) in zip(
        OUTPUT_NAMES,
        PANEL_SPECS,
        strict=True,
    ):
        rendered[filename] = _render_panel(
            document,
            title=title,
            subtitle=subtitle,
            indexes=indexes,
            slug=filename.removesuffix(".svg"),
        )
    return rendered


def _open_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))  # noqa: PTH100
    if absolute.anchor != "/":
        _fail("terminal evidence output path is not canonical POSIX")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o755, dir_fd=descriptor)
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError:
        os.close(descriptor)
        _fail("terminal evidence output directory is unavailable or unsafe")
    return descriptor


def _atomic_write(filename: str, payload: bytes) -> None:
    if filename not in OUTPUT_NAMES:
        _fail("terminal renderer refused an unexpected output name")
    directory = _open_directory(OUTPUT_DIRECTORY)
    temporary_name = f".{filename}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        try:
            existing = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
            _fail("terminal SVG output is not one regular single-link file")
        descriptor = os.open(temporary_name, _WRITE_FLAGS, 0o644, dir_fd=directory)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                _fail("terminal SVG write made no progress")
            offset += written
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except TerminalRenderError:
        raise
    except OSError:
        _fail("terminal SVG could not be replaced atomically")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(directory)


def _read_output(filename: str) -> bytes:
    path = OUTPUT_DIRECTORY / filename
    try:
        descriptor = os.open(path, _READ_FLAGS)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            _fail("terminal SVG output must be one regular single-link file")
        payload = bytearray()
        with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as stream:
            while len(payload) <= MAX_SVG_BYTES:
                chunk = stream.read(min(65_536, MAX_SVG_BYTES + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
    except TerminalRenderError:
        raise
    except OSError:
        _fail("terminal SVG output is unavailable or unsafe")
    if len(payload) > MAX_SVG_BYTES:
        _fail("terminal SVG output exceeds the byte limit")
    return bytes(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render source-bound terminal SVGs from installed-wheel evidence.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write both canonical SVG files")
    mode.add_argument("--check", action="store_true", help="compare both files byte-for-byte")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for deterministic terminal rendering."""

    arguments = _parser().parse_args(argv)
    try:
        document = contract.decode_evidence_bytes(_read_source())
        rendered = render_evidence(document)
        if arguments.write:
            for filename in OUTPUT_NAMES:
                _atomic_write(filename, rendered[filename])
            return 0
        for filename in OUTPUT_NAMES:
            if _read_output(filename) != rendered[filename]:
                _fail(f"{filename} is not byte-for-byte current")
    except (TerminalRenderError, contract.EvidenceContractError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
