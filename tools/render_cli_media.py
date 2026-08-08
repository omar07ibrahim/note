#!/usr/bin/env python3
"""Render deterministic raster and motion evidence from installed-wheel records."""

# The fixed media geometry and five workflow phases are contract values.
# ruff: noqa: PLR2004

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import os
import secrets
import stat
import sys
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import Any, Final, NoReturn, cast

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import cli_evidence_contract as contract  # noqa: E402
from tools import render_cli_evidence  # noqa: E402

EVIDENCE_PATH: Final = ROOT / "docs" / "visuals" / "evidence" / "installed-wheel-cli.v1.json"
SVG_DIRECTORY: Final = ROOT / "docs" / "visuals"
PNG_NAME: Final = "installed-wheel-cli.png"
GIF_NAME: Final = "installed-wheel-workflow.gif"
MANIFEST_NAME: Final = "installed-wheel-media.manifest.json"
PAYLOAD_NAMES: Final = (
    "installed-wheel-cli.v1.json",
    *render_cli_evidence.OUTPUT_NAMES,
    PNG_NAME,
    GIF_NAME,
)
OUTPUT_NAMES: Final = (*PAYLOAD_NAMES, MANIFEST_NAME)
ADOPTED_PATHS: Final = {
    "installed-wheel-cli.v1.json": "docs/visuals/evidence/installed-wheel-cli.v1.json",
    "installed-wheel-write-replay.svg": "docs/visuals/installed-wheel-write-replay.svg",
    "installed-wheel-history-tombstone.svg": "docs/visuals/installed-wheel-history-tombstone.svg",
    PNG_NAME: f"docs/visuals/{PNG_NAME}",
    GIF_NAME: f"docs/visuals/{GIF_NAME}",
    MANIFEST_NAME: f"docs/visuals/{MANIFEST_NAME}",
}
RENDERER_INPUT_PATHS: Final = (
    "requirements-visuals.lock",
    "tools/capture_cli_evidence.py",
    "tools/cli_evidence_contract.py",
    "tools/render_cli_evidence.py",
    "tools/render_cli_media.py",
)
FRAME_SPECS: Final = (
    ("Create and exact replay", range(2)),
    ("Revise and reject stale command", range(2, 4)),
    ("Read head and first history page", range(4, 6)),
    ("Continue history and tombstone", range(6, 8)),
    ("Hide live read and retain audit head", range(8, 10)),
)
EXPECTED_PYTHON: Final = (3, 12, 3)
EXPECTED_PILLOW_VERSION: Final = "12.3.0"
EXPECTED_FONT_NAME: Final = ("Aileron", "Regular")
EXPECTED_WHEEL_FILENAME: Final = (
    "pillow-12.3.0-cp312-cp312-manylinux_2_27_x86_64."
    "manylinux_2_28_x86_64.whl"
)
EXPECTED_WHEEL_SHA256: Final = (
    "78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91"
)
PNG_WIDTH: Final = 1920
GIF_WIDTH: Final = 1600
HEADER_HEIGHT: Final = 164
FOOTER_HEIGHT: Final = 94
PNG_LINE_HEIGHT: Final = 24
GIF_LINE_HEIGHT: Final = 20
TERMINAL_TOP_PAD: Final = 58
TERMINAL_BOTTOM_PAD: Final = 28
GIF_DURATIONS_MS: Final = (1000, 1000, 1000, 1000, 1600)
MAX_SOURCE_BYTES: Final = 524_288
MAX_MEDIA_BYTES: Final = 8 * 1024 * 1024
MAX_MANIFEST_BYTES: Final = 131_072
MAX_BUNDLE_BYTES: Final = 24 * 1024 * 1024
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)


class MediaRenderError(ValueError):
    """The evidence cannot be rendered or the media boundary is unsafe."""


def _fail(message: str) -> NoReturn:
    raise MediaRenderError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular(path: Path, *, maximum: int, context: str) -> bytes:
    try:
        descriptor = os.open(path, _READ_FLAGS)
        metadata_value = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_value.st_mode) or metadata_value.st_nlink != 1:
            os.close(descriptor)
            _fail(f"{context} must be one regular single-link file")
        payload = bytearray()
        with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as stream:
            while len(payload) <= maximum:
                chunk = stream.read(min(65_536, maximum + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
    except MediaRenderError:
        raise
    except OSError:
        _fail(f"{context} is unavailable or unsafe")
    if len(payload) > maximum:
        _fail(f"{context} exceeds its byte limit")
    return bytes(payload)


def _open_directory(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))  # noqa: PTH100
    if absolute.anchor != "/":
        _fail("media output path is not canonical POSIX")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o755, dir_fd=descriptor)
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError:
        os.close(descriptor)
        _fail("media output directory is unavailable or unsafe")
    return descriptor


def _atomic_write(output_directory: Path, filename: str, payload: bytes) -> None:
    if filename not in OUTPUT_NAMES:
        _fail("media renderer refused an unexpected output name")
    directory = _open_directory(output_directory, create=True)
    temporary_name = f".{filename}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        try:
            existing = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            _fail("media output is not one regular single-link file")
        descriptor = os.open(temporary_name, _WRITE_FLAGS, 0o644, dir_fd=directory)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                _fail("media write made no progress")
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
    except MediaRenderError:
        raise
    except OSError:
        _fail("media output could not be replaced atomically")
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


def _read_bundle(output_directory: Path) -> dict[str, bytes]:
    directory = _open_directory(output_directory, create=False)
    try:
        try:
            names = os.listdir(directory)  # noqa: PTH208 - descriptor-pinned inventory
        except OSError:
            _fail("media bundle inventory is unavailable")
        if set(names) != set(OUTPUT_NAMES) or len(names) != len(OUTPUT_NAMES):
            _fail("media bundle inventory is not the exact six-file contract")
        payloads: dict[str, bytes] = {}
        total = 0
        for filename in OUTPUT_NAMES:
            try:
                descriptor = os.open(filename, _READ_FLAGS, dir_fd=directory)
                metadata_value = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata_value.st_mode)
                    or metadata_value.st_nlink != 1
                    or metadata_value.st_mode & 0o777 != 0o644
                ):
                    os.close(descriptor)
                    _fail("media bundle entries must be regular 0644 single-link files")
                maximum = MAX_MANIFEST_BYTES if filename == MANIFEST_NAME else MAX_MEDIA_BYTES
                payload = bytearray()
                with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as stream:
                    while len(payload) <= maximum:
                        chunk = stream.read(min(65_536, maximum + 1 - len(payload)))
                        if not chunk:
                            break
                        payload.extend(chunk)
            except MediaRenderError:
                raise
            except OSError:
                _fail("media bundle entry is unavailable or unsafe")
            if len(payload) > maximum:
                _fail("media bundle entry exceeds its byte limit")
            total += len(payload)
            if total > MAX_BUNDLE_BYTES:
                _fail("media bundle exceeds its aggregate byte limit")
            payloads[filename] = bytes(payload)
        return payloads
    finally:
        os.close(directory)


def workflow_frames(
    document: contract.JsonObject,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Project all ten verified steps into five ordered motion frames."""

    contract.validate_evidence_document(document)
    return tuple(
        (title, render_cli_evidence.transcript_lines(document, indexes))
        for title, indexes in FRAME_SPECS
    )


def _pillow_modules() -> tuple[Any, Any, Any]:
    if sys.version_info[:3] != EXPECTED_PYTHON:
        _fail("media renderer requires exact CPython 3.12.3")
    try:
        pillow_version = metadata.version("Pillow")
        pillow = importlib.import_module("PIL")
        image_module = importlib.import_module("PIL.Image")
        draw_module = importlib.import_module("PIL.ImageDraw")
        font_module = importlib.import_module("PIL.ImageFont")
    except (ImportError, metadata.PackageNotFoundError):
        _fail("exact visual-only Pillow dependency is unavailable")
    if pillow_version != EXPECTED_PILLOW_VERSION or pillow.__version__ != pillow_version:
        _fail("media renderer requires exact Pillow 12.3.0")
    probe = font_module.load_default(size=15)
    if probe.getname() != EXPECTED_FONT_NAME:
        _fail("Pillow embedded default font is not Aileron Regular")
    return image_module, draw_module, font_module


def _line_color(kind: str) -> str:
    return {
        "blank": "#e6edf7",
        "command": "#d7f9ef",
        "exit": "#8ee6b7",
        "exit-error": "#ff9fab",
        "stderr": "#ffc9cf",
        "stdout": "#e6edf7",
    }[kind]


def _provenance(document: contract.JsonObject) -> tuple[str, str]:
    provenance = cast(contract.JsonObject, document["provenance"])
    wheel = cast(contract.JsonObject, provenance["wheel"])
    return cast(str, provenance["source_commit"]), cast(str, wheel["sha256"])


def _canvas_height(line_count: int, *, line_height: int) -> int:
    return (
        HEADER_HEIGHT
        + TERMINAL_TOP_PAD
        + line_count * line_height
        + TERMINAL_BOTTOM_PAD
        + FOOTER_HEIGHT
    )


def _draw_canvas(  # noqa: PLR0913 - explicit fixed rendering contract
    *,
    image_module: Any,
    draw_module: Any,
    font_module: Any,
    width: int,
    height: int,
    title: str,
    subtitle: str,
    source_commit: str,
    wheel_digest: str,
    lines: tuple[tuple[str, str], ...],
    line_height: int,
    body_size: int,
    footer: str,
) -> Any:
    image = image_module.new("RGB", (width, height), "#f7f9fc")
    draw = draw_module.Draw(image)
    title_font = font_module.load_default(size=30)
    subtitle_font = font_module.load_default(size=16)
    provenance_font = font_module.load_default(size=13)
    body_font = font_module.load_default(size=body_size)
    footer_font = font_module.load_default(size=13)
    for font in (title_font, subtitle_font, provenance_font, body_font, footer_font):
        if font.getname() != EXPECTED_FONT_NAME:
            _fail("embedded font identity changed during rendering")

    draw.rectangle((0, 0, width, HEADER_HEIGHT), fill="#142033")
    draw.text((48, 30), title, font=title_font, fill="#ffffff")
    draw.text((48, 78), subtitle, font=subtitle_font, fill="#d8e5f3")
    draw.text(
        (48, 112),
        f"source {source_commit[:12]} | wheel sha256 {wheel_digest[:16]}...",
        font=provenance_font,
        fill="#aebfd2",
    )

    terminal_left = 48
    terminal_right = width - 48
    terminal_top = HEADER_HEIGHT + 24
    terminal_bottom = height - FOOTER_HEIGHT - 20
    draw.rounded_rectangle(
        (terminal_left, terminal_top, terminal_right, terminal_bottom),
        radius=14,
        fill="#111827",
        outline="#45566d",
        width=2,
    )
    draw.ellipse((72, terminal_top + 18, 86, terminal_top + 32), fill="#ff6978")
    draw.ellipse((96, terminal_top + 18, 110, terminal_top + 32), fill="#f7c65d")
    draw.ellipse((120, terminal_top + 18, 134, terminal_top + 32), fill="#65d39b")
    draw.text(
        (152, terminal_top + 16),
        "installed-venv | synthetic tenant | canonical normalized records",
        font=provenance_font,
        fill="#b9c5d6",
    )

    x = terminal_left + 24
    y = terminal_top + TERMINAL_TOP_PAD
    for line, kind in lines:
        if line:
            bounds = draw.textbbox((x, y), line, font=body_font)
            if bounds[2] > terminal_right - 20:
                _fail("terminal raster line exceeds the verified canvas width")
            draw.text((x, y), line, font=body_font, fill=_line_color(kind))
        y += line_height
    if y > terminal_bottom - 8:
        _fail("terminal raster lines exceed the verified canvas height")
    draw.text((48, height - 64), footer, font=footer_font, fill="#405166")
    return image


def _render_png(
    document: contract.JsonObject,
    image_module: Any,
    draw_module: Any,
    font_module: Any,
) -> bytes:
    lines = render_cli_evidence.transcript_lines(document, range(10))
    source_commit, wheel_digest = _provenance(document)
    height = _canvas_height(len(lines), line_height=PNG_LINE_HEIGHT)
    image = _draw_canvas(
        image_module=image_module,
        draw_module=draw_module,
        font_module=font_module,
        width=PNG_WIDTH,
        height=height,
        title="RecallLedger verified installed-wheel transcript",
        subtitle="All 10 commands, normalized stdout/stderr records, and exact exits",
        source_commit=source_commit,
        wheel_digest=wheel_digest,
        lines=lines,
        line_height=PNG_LINE_HEIGHT,
        body_size=15,
        footer=(
            "Normalized verified installed-wheel transcript; synthetic fixtures only; "
            "not an OS-terminal screenshot."
        ),
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    payload = output.getvalue()
    if len(payload) > MAX_MEDIA_BYTES:
        _fail("terminal PNG exceeds the byte limit")
    return payload


def _render_gif(
    document: contract.JsonObject,
    image_module: Any,
    draw_module: Any,
    font_module: Any,
) -> bytes:
    frame_records = workflow_frames(document)
    source_commit, wheel_digest = _provenance(document)
    maximum_lines = max(len(lines) for _title, lines in frame_records)
    height = _canvas_height(maximum_lines, line_height=GIF_LINE_HEIGHT)
    frames: list[Any] = []
    for index, (phase_title, lines) in enumerate(frame_records, start=1):
        frame = _draw_canvas(
            image_module=image_module,
            draw_module=draw_module,
            font_module=font_module,
            width=GIF_WIDTH,
            height=height,
            title="RecallLedger installed-wheel workflow",
            subtitle=f"Phase {index}/5 | {phase_title}",
            source_commit=source_commit,
            wheel_digest=wheel_digest,
            lines=lines,
            line_height=GIF_LINE_HEIGHT,
            body_size=13,
            footer=(
                "Actual normalized argv/stdout/stderr/exit records from synthetic fixtures; "
                "deliberate workflow, not an incident."
            ),
        )
        frames.append(
            frame.convert(
                "P",
                palette=image_module.Palette.ADAPTIVE,
                colors=128,
                dither=image_module.Dither.NONE,
            )
        )
    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=list(GIF_DURATIONS_MS),
        loop=0,
        disposal=2,
        optimize=False,
    )
    payload = output.getvalue()
    if len(payload) > MAX_MEDIA_BYTES:
        _fail("workflow GIF exceeds the byte limit")
    return payload


def _verify_png(payload: bytes, document: contract.JsonObject, image_module: Any) -> None:
    expected_lines = render_cli_evidence.transcript_lines(document, range(10))
    expected_size = (
        PNG_WIDTH,
        _canvas_height(len(expected_lines), line_height=PNG_LINE_HEIGHT),
    )
    try:
        with image_module.open(io.BytesIO(payload)) as image:
            image.load()
            if (
                image.format != "PNG"
                or image.mode != "RGB"
                or image.size != expected_size
                or image.getbands() != ("R", "G", "B")
            ):
                _fail("terminal PNG structure is not the exact contract")
            if image.info or len(image.getexif()) != 0:
                _fail("terminal PNG contains unexpected metadata")
    except MediaRenderError:
        raise
    except Exception:
        _fail("terminal PNG is not decodable")


def _verify_gif(payload: bytes, document: contract.JsonObject, image_module: Any) -> None:
    frames = workflow_frames(document)
    expected_height = _canvas_height(
        max(len(lines) for _title, lines in frames),
        line_height=GIF_LINE_HEIGHT,
    )
    try:
        with image_module.open(io.BytesIO(payload)) as image:
            if (
                image.format != "GIF"
                or image.size != (GIF_WIDTH, expected_height)
                or image.n_frames != len(FRAME_SPECS)
                or image.info.get("loop") != 0
            ):
                _fail("workflow GIF structure is not the exact contract")
            forbidden_metadata = {"comment", "transparency", "icc_profile", "exif"}
            for index, expected_duration in enumerate(GIF_DURATIONS_MS):
                image.seek(index)
                if image.info.get("duration") != expected_duration:
                    _fail("workflow GIF frame duration changed")
                if getattr(image, "disposal_method", None) != 2:
                    _fail("workflow GIF disposal method changed")
                if forbidden_metadata.intersection(image.info):
                    _fail("workflow GIF contains forbidden metadata")
                image.load()
    except MediaRenderError:
        raise
    except Exception:
        _fail("workflow GIF is not decodable")


def _record(filename: str, payload: bytes) -> contract.JsonObject:
    return {
        "adopted_path": ADOPTED_PATHS[filename],
        "artifact_path": filename,
        "mode": "100644",
        "sha256": _sha256(payload),
        "size_bytes": len(payload),
    }


def _renderer_inputs() -> list[contract.JsonValue]:
    records: list[contract.JsonValue] = []
    for relative_path in RENDERER_INPUT_PATHS:
        payload = _read_regular(
            ROOT / relative_path,
            maximum=MAX_SOURCE_BYTES,
            context=f"renderer input {relative_path}",
        )
        records.append(
            {
                "path": relative_path,
                "sha256": _sha256(payload),
                "size_bytes": len(payload),
            }
        )
    return records


def _manifest(
    document: contract.JsonObject,
    payloads: dict[str, bytes],
) -> bytes:
    provenance = cast(contract.JsonObject, document["provenance"])
    wheel = cast(contract.JsonObject, provenance["wheel"])
    steps = cast(list[contract.JsonObject], document["steps"])
    stdout_records = sum(len(cast(list[contract.JsonValue], step["stdout"])) for step in steps)
    stderr_records = sum(len(cast(list[contract.JsonValue], step["stderr"])) for step in steps)
    manifest: contract.JsonObject = {
        "adoption_status": "generated-not-adopted",
        "artifact": "installed-wheel-terminal-media",
        "caption": (
            "Normalized verified installed-wheel transcript from synthetic fixtures; "
            "the PNG is not an OS-terminal screenshot and the GIF is a deliberate workflow."
        ),
        "environment": {
            "font": {
                "asset_policy": "embedded in Pillow; no repository font file or external asset",
                "family": EXPECTED_FONT_NAME[0],
                "loader": "PIL.ImageFont.load_default",
                "style": EXPECTED_FONT_NAME[1],
            },
            "pillow_version": EXPECTED_PILLOW_VERSION,
            "python_implementation": "CPython",
            "python_version": ".".join(str(value) for value in EXPECTED_PYTHON),
            "wheel_filename": EXPECTED_WHEEL_FILENAME,
            "wheel_sha256": EXPECTED_WHEEL_SHA256,
        },
        "manifest": {
            "adopted_path": ADOPTED_PATHS[MANIFEST_NAME],
            "artifact_path": MANIFEST_NAME,
            "mode": "100644",
            "self_included": False,
        },
        "media": {
            "gif": {
                "durations_ms": list(GIF_DURATIONS_MS),
                "frames": len(FRAME_SPECS),
                "projection": "five ordered phases covering each verified step exactly once",
            },
            "png": {
                "projection": "complete normalized transcript for all ten verified steps",
            },
        },
        "outputs": [_record(filename, payloads[filename]) for filename in PAYLOAD_NAMES],
        "renderer_inputs": _renderer_inputs(),
        "review_boundary": {
            "fixtures": "synthetic documentation fixtures only",
            "gif": "actual normalized commands/channels/exits; deliberate workflow, not incident",
            "normalization": contract.NORMALIZATION_DISCLOSURE,
            "png": "deterministic text rendering; not an OS-terminal screenshot",
            "unsupported_claims": (
                "no authentication, performance, retrieval-quality, or production proof"
            ),
        },
        "schema_version": 1,
        "source": {
            "evidence_sha256": _sha256(payloads["installed-wheel-cli.v1.json"]),
            "git_commit": cast(str, provenance["source_commit"]),
            "git_tree": cast(str, provenance["source_tree"]),
            "installed_wheel_sha256": cast(str, wheel["sha256"]),
        },
        "workflow": {
            "exit_0": sum(step["exit_code"] == 0 for step in steps),
            "exit_11": sum(step["exit_code"] == 11 for step in steps),
            "stderr_records": stderr_records,
            "stdout_records": stdout_records,
            "steps": len(steps),
        },
    }
    payload = contract.canonical_json_bytes(manifest)
    if len(payload) > MAX_MANIFEST_BYTES:
        _fail("media manifest exceeds the byte limit")
    return payload


def _security_check(payloads: dict[str, bytes]) -> None:
    forbidden = (
        b"/home/",
        b"/Users/",
        b"file://",
        b"BEGIN PRIVATE KEY",
        b"github_pat_",
        b"gho_",
        b"ghp_",
        b"\x1b",
    )
    combined = b"".join(payloads[name] for name in OUTPUT_NAMES)
    if any(token in combined for token in forbidden):
        _fail("media bundle contains a host path, escape, or credential marker")
    for filename in render_cli_evidence.OUTPUT_NAMES:
        text = payloads[filename].replace(b"http://www.w3.org/2000/svg", b"").lower()
        if any(
            token in text
            for token in (b"<script", b"<image", b"foreignobject", b"href=", b"://")
        ):
            _fail("terminal SVG contains an executable or external asset")


def render_bundle(
    *,
    evidence_path: Path = EVIDENCE_PATH,
    svg_directory: Path = SVG_DIRECTORY,
) -> dict[str, bytes]:
    """Render and validate the exact flat six-file hosted artifact."""

    image_module, draw_module, font_module = _pillow_modules()
    evidence = _read_regular(
        evidence_path,
        maximum=contract.MAX_CHANNEL_BYTES,
        context="installed-wheel evidence",
    )
    document = contract.decode_evidence_bytes(evidence)
    expected_svgs = render_cli_evidence.render_evidence(document)
    payloads: dict[str, bytes] = {"installed-wheel-cli.v1.json": evidence}
    for filename in render_cli_evidence.OUTPUT_NAMES:
        actual = _read_regular(
            svg_directory / filename,
            maximum=render_cli_evidence.MAX_SVG_BYTES,
            context=f"terminal SVG {filename}",
        )
        if actual != expected_svgs[filename]:
            _fail(f"{filename} does not match the captured evidence")
        payloads[filename] = actual
    payloads[PNG_NAME] = _render_png(document, image_module, draw_module, font_module)
    payloads[GIF_NAME] = _render_gif(document, image_module, draw_module, font_module)
    _verify_png(payloads[PNG_NAME], document, image_module)
    _verify_gif(payloads[GIF_NAME], document, image_module)
    payloads[MANIFEST_NAME] = _manifest(document, payloads)
    _security_check(payloads)
    if set(payloads) != set(OUTPUT_NAMES):
        _fail("renderer did not produce the exact six-file bundle")
    return payloads


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render verified installed-wheel PNG/GIF evidence and its manifest.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="atomically write the flat bundle")
    mode.add_argument("--check", action="store_true", help="compare the flat bundle byte-for-byte")
    parser.add_argument("--evidence-path", type=Path, default=EVIDENCE_PATH)
    parser.add_argument("--svg-directory", type=Path, default=SVG_DIRECTORY)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for deterministic raster and motion evidence."""

    arguments = _parser().parse_args(argv)
    try:
        expected = render_bundle(
            evidence_path=arguments.evidence_path,
            svg_directory=arguments.svg_directory,
        )
        if arguments.write:
            for filename in OUTPUT_NAMES:
                _atomic_write(arguments.output_directory, filename, expected[filename])
        actual = _read_bundle(arguments.output_directory)
        if actual != expected:
            _fail("media bundle is not byte-for-byte current")
        _security_check(actual)
    except (MediaRenderError, contract.EvidenceContractError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
