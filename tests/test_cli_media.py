from __future__ import annotations

import ast
import binascii
import hashlib
import stat
import tomllib
import zlib
from pathlib import Path
from typing import cast

import pytest

from tools import cli_evidence_contract as contract
from tools import render_cli_evidence, render_cli_media

ROOT = Path(__file__).resolve().parents[1]
ADOPTION_PATH = ROOT / "docs/visuals/evidence/installed-wheel-media.adoption.json"
EXPECTED_ADOPTED_MEDIA = {
    "installed-wheel-cli.png": (
        "docs/visuals/installed-wheel-cli.png",
        267_526,
        "5791a8cdd59287b3df61b4aa5e5e023f98b00bc049e2d45beccd94f2782580eb",
    ),
    "installed-wheel-cli.v1.json": (
        "docs/visuals/evidence/installed-wheel-cli.v1.json",
        13_603,
        "cb87a1d586a784a44d48ede263c198876c29bf586585fb7b0464b9421ac45c88",
    ),
    "installed-wheel-history-tombstone.svg": (
        "docs/visuals/installed-wheel-history-tombstone.svg",
        7_412,
        "e0b84541745096923536a7933fe0d573d812c3fba6a26b886fe912d02d4391c6",
    ),
    "installed-wheel-media.manifest.json": (
        "docs/visuals/installed-wheel-media.manifest.json",
        3_785,
        "672e594a227e7049cfcbf11787c778cbce48b0dc5922e6afb53f7686a3c1d290",
    ),
    "installed-wheel-workflow.gif": (
        "docs/visuals/installed-wheel-workflow.gif",
        222_249,
        "f3333c551d42b68094ec15aa804b17457c9a5f1672d248c1c80caec9fe278185",
    ),
    "installed-wheel-write-replay.svg": (
        "docs/visuals/installed-wheel-write-replay.svg",
        7_642,
        "975520ab728c989023c1236c962f81c85669c60e7b190ce69bf35d7099a7e9f2",
    ),
}


def _gif_sub_blocks(payload: bytes, offset: int) -> tuple[bytes, int]:
    result = bytearray()
    while True:
        assert offset < len(payload)
        size = payload[offset]
        offset += 1
        if size == 0:
            return bytes(result), offset
        end = offset + size
        assert end <= len(payload)
        result.extend(payload[offset:end])
        offset = end


def _assert_adopted_png_contract(payload: bytes) -> None:
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        assert end <= len(payload)
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(payload[offset + 8 + length : end], "big")
        assert binascii.crc32(kind + data) & 0xFFFF_FFFF == expected_crc
        chunks.append((kind, data))
        offset = end
        if kind == b"IEND":
            break
    assert offset == len(payload)
    kinds = tuple(kind for kind, _data in chunks)
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    assert set(kinds) == {b"IHDR", b"IDAT", b"IEND"}
    header = chunks[0][1]
    assert len(header) == 13
    width = int.from_bytes(header[0:4], "big")
    height = int.from_bytes(header[4:8], "big")
    assert (width, height) == (1_920, 2_120)
    assert header[8:] == bytes((8, 2, 0, 0, 0))
    scanlines = zlib.decompress(b"".join(data for kind, data in chunks if kind == b"IDAT"))
    stride = 1 + width * 3
    assert len(scanlines) == height * stride
    assert {scanlines[row * stride] for row in range(height)} <= {0, 1, 2, 3, 4}


def _assert_adopted_gif_contract(payload: bytes) -> None:  # noqa: PLR0915
    assert payload.startswith(b"GIF89a")
    width = int.from_bytes(payload[6:8], "little")
    height = int.from_bytes(payload[8:10], "little")
    assert (width, height) == (1_600, 708)
    packed = payload[10]
    assert packed & 0x80
    offset = 13 + 3 * (2 ** ((packed & 0x07) + 1))
    pending: tuple[int, int, bool] | None = None
    frames: list[tuple[int, int, bool, int, int, int, int]] = []
    loop_count: int | None = None
    while offset < len(payload):
        marker = payload[offset]
        offset += 1
        if marker == 0x3B:
            assert offset == len(payload) and pending is None
            break
        if marker == 0x21:
            label = payload[offset]
            offset += 1
            if label == 0xF9:
                assert payload[offset] == 4
                flags = payload[offset + 1]
                delay_ms = int.from_bytes(payload[offset + 2 : offset + 4], "little") * 10
                pending = ((flags >> 2) & 0x07, delay_ms, bool(flags & 0x01))
                assert payload[offset + 5] == 0
                offset += 6
                continue
            header_size = payload[offset]
            offset += 1
            header = payload[offset : offset + header_size]
            offset += header_size
            extension, offset = _gif_sub_blocks(payload, offset)
            assert label == 0xFF and header == b"NETSCAPE2.0"
            assert len(extension) == 3 and extension[0] == 1
            loop_count = int.from_bytes(extension[1:3], "little")
            continue
        assert marker == 0x2C and pending is not None
        descriptor = payload[offset : offset + 9]
        assert len(descriptor) == 9
        left = int.from_bytes(descriptor[0:2], "little")
        top = int.from_bytes(descriptor[2:4], "little")
        frame_width = int.from_bytes(descriptor[4:6], "little")
        frame_height = int.from_bytes(descriptor[6:8], "little")
        offset += 9
        if descriptor[8] & 0x80:
            offset += 3 * (2 ** ((descriptor[8] & 0x07) + 1))
        assert 2 <= payload[offset] <= 8
        compressed, offset = _gif_sub_blocks(payload, offset + 1)
        assert compressed
        frames.append((*pending, left, top, frame_width, frame_height))
        pending = None
    else:
        raise AssertionError
    assert loop_count == 0
    assert frames == [
        (2, 1_000, False, 0, 0, 1_600, 708),
        (2, 1_000, False, 0, 0, 1_600, 708),
        (2, 1_000, False, 0, 0, 1_600, 708),
        (2, 1_000, False, 0, 0, 1_600, 708),
        (2, 1_600, False, 0, 0, 1_600, 708),
    ]


def _document() -> contract.JsonObject:
    raw = (ROOT / "docs/visuals/evidence/installed-wheel-cli.v1.json").read_bytes()
    return contract.decode_evidence_bytes(raw)


def test_media_projection_covers_every_verified_command_channel_and_exit() -> None:
    document = _document()
    frames = render_cli_media.workflow_frames(document)

    assert len(frames) == 5
    assert tuple(title for title, _lines in frames) == tuple(
        title for title, _indexes in render_cli_media.FRAME_SPECS
    )
    expected = tuple(
        render_cli_evidence.transcript_lines(document, indexes)
        for _title, indexes in render_cli_media.FRAME_SPECS
    )
    assert tuple(lines for _title, lines in frames) == expected

    projected = "\n".join(line for _title, lines in frames for line, _kind in lines)
    assert projected.count("$ recall-ledger") == 10
    assert projected.count("exit   | 0") == 9
    assert projected.count("exit   | 11") == 1
    assert "stdout |" in projected
    assert "stderr |" in projected
    assert '"code":"REVISION_CONFLICT"' in projected
    assert '"kind":"note.tombstoned"' in projected
    assert contract.NOTE_TOKEN in projected


def test_media_bundle_contract_is_bounded_acyclic_and_maps_only_reviewed_paths() -> None:
    assert render_cli_media.OUTPUT_NAMES == (
        "installed-wheel-cli.v1.json",
        "installed-wheel-write-replay.svg",
        "installed-wheel-history-tombstone.svg",
        "installed-wheel-cli.png",
        "installed-wheel-workflow.gif",
        "installed-wheel-media.manifest.json",
    )
    assert render_cli_media.MANIFEST_NAME not in render_cli_media.PAYLOAD_NAMES
    assert set(render_cli_media.ADOPTED_PATHS) == set(render_cli_media.OUTPUT_NAMES)
    assert render_cli_media.ADOPTED_PATHS["installed-wheel-cli.v1.json"] == (
        "docs/visuals/evidence/installed-wheel-cli.v1.json"
    )
    assert all(path.startswith("docs/visuals/") for path in render_cli_media.ADOPTED_PATHS.values())
    covered_indexes = tuple(
        index for _title, indexes in render_cli_media.FRAME_SPECS for index in indexes
    )
    assert covered_indexes == tuple(range(10))
    assert len(render_cli_media.GIF_DURATIONS_MS) == len(render_cli_media.FRAME_SPECS)
    assert render_cli_media.MAX_BUNDLE_BYTES == 24 * 1024 * 1024


def test_media_geometry_bounds_png_and_every_gif_phase_before_the_footer() -> None:
    document = _document()
    png_lines = render_cli_evidence.transcript_lines(document, range(10))
    gif_frames = render_cli_media.workflow_frames(document)
    gif_capacity = max(len(lines) for _title, lines in gif_frames)
    cases: list[tuple[int, int, int]] = [
        (len(png_lines), render_cli_media.PNG_LINE_HEIGHT, len(png_lines))
    ]
    cases.extend(
        (len(lines), render_cli_media.GIF_LINE_HEIGHT, gif_capacity) for _title, lines in gif_frames
    )

    assert len(cases) == 1 + len(render_cli_media.FRAME_SPECS)
    for line_count, line_height, canvas_line_count in cases:
        geometry = render_cli_media._canvas_geometry(
            line_count,
            line_height=line_height,
            canvas_line_count=canvas_line_count,
        )
        assert geometry.height == render_cli_media._canvas_height(
            canvas_line_count, line_height=line_height
        )
        assert geometry.terminal_top == (
            render_cli_media.HEADER_HEIGHT + render_cli_media.TERMINAL_OUTER_TOP_GAP
        )
        assert geometry.first_line_top == (
            geometry.terminal_top + render_cli_media.TERMINAL_TOP_PAD
        )
        assert geometry.last_line_top == (geometry.first_line_top + (line_count - 1) * line_height)
        assert geometry.last_line_top + line_height == geometry.last_line_box_bottom
        assert geometry.last_line_box_bottom <= (
            geometry.terminal_bottom - render_cli_media.TERMINAL_LINE_BOTTOM_CLEARANCE
        )
        assert geometry.terminal_bottom + render_cli_media.TERMINAL_OUTER_BOTTOM_GAP == (
            geometry.footer_top
        )
        assert geometry.footer_top <= geometry.footer_text_top < geometry.height


def test_media_security_ignores_binary_escape_coincidence_but_rejects_textual_ansi() -> None:
    payloads = dict.fromkeys(render_cli_media.OUTPUT_NAMES, b"safe\n")
    payloads[render_cli_media.PNG_NAME] = b"\x89PNG\r\n\x1a\nbinary-\x1b-byte"
    payloads[render_cli_media.GIF_NAME] = b"GIF89a-binary-\x1b-byte"

    render_cli_media._security_check(payloads)

    payloads["installed-wheel-cli.v1.json"] = b'{"ansi":"\x1b[31m"}\n'
    with pytest.raises(render_cli_media.MediaRenderError, match="contains a control byte"):
        render_cli_media._security_check(payloads)


def test_adopted_media_matches_reviewed_hosted_artifact_and_generated_manifest() -> None:
    adoption = contract.decode_canonical_document(
        ADOPTION_PATH.read_bytes(), context="installed-wheel media adoption"
    )
    assert set(adoption) == {
        "adoption_status",
        "entries",
        "hosted_artifact",
        "review",
        "schema_version",
        "source",
    }
    assert (
        adoption["schema_version"] == 1
        and adoption["adoption_status"] == "adopted-after-independent-review"
    )
    source = cast(contract.JsonObject, adoption["source"])
    assert source == {
        "git_commit": "9ab4115e817deab4c843aa4d1680ef1cd63a9503",
        "git_tree": "bbc8dcc4d4e3f8456b469382902edd731e36ee56",
        "media_manifest_sha256": "672e594a227e7049cfcbf11787c778cbce48b0dc5922e6afb53f7686a3c1d290",
    }
    assert adoption["hosted_artifact"] == {
        "archive_digest": "sha256:bb6c2f45693392a4febb252c22856e81ec097d429e7a8378eed1474dc844878d",
        "artifact_id": 9_038_918_220,
        "created_at": "2026-08-09T13:44:33Z",
        "expires_at": "2026-08-10T13:44:32Z",
        "name": "fts5-installed-wheel-evidence-31316561987",
        "retention_days": 1,
        "run_id": 31_316_561_987,
        "size_bytes": 523_155,
        "workflow_job_id": 93_252_660_174,
    }
    review = cast(contract.JsonObject, adoption["review"])
    assert review["independence"] == "A second agent reviewed the hosted archive before adoption."
    assert "not a signature" in cast(str, review["attestation_boundary"])
    entries_value = adoption["entries"]
    assert type(entries_value) is list
    records = {
        cast(str, entry["artifact_path"]): entry
        for entry in cast(list[contract.JsonObject], entries_value)
    }
    assert set(records) == set(EXPECTED_ADOPTED_MEDIA)
    for artifact_path, (
        adopted_path,
        expected_size,
        expected_hash,
    ) in EXPECTED_ADOPTED_MEDIA.items():
        payload_path = ROOT / adopted_path
        payload = payload_path.read_bytes()
        assert (
            len(payload) == expected_size and hashlib.sha256(payload).hexdigest() == expected_hash
        )
        assert stat.S_IMODE(payload_path.stat().st_mode) == 0o644
        assert records[artifact_path] == {
            "adopted_path": adopted_path,
            "artifact_path": artifact_path,
            "mode": "100644",
            "sha256": expected_hash,
            "size_bytes": expected_size,
        }
    manifest_raw = (ROOT / "docs/visuals/installed-wheel-media.manifest.json").read_bytes()
    manifest = contract.decode_canonical_document(
        manifest_raw, context="installed-wheel media manifest"
    )
    assert hashlib.sha256(manifest_raw).hexdigest() == source["media_manifest_sha256"]
    assert manifest["adoption_status"] == "generated-not-adopted"
    manifest_source = cast(contract.JsonObject, manifest["source"])
    assert (
        manifest_source["git_commit"] == source["git_commit"]
        and manifest_source["git_tree"] == source["git_tree"]
    )
    manifest_entries = cast(list[contract.JsonObject], manifest["outputs"])
    for record in manifest_entries:
        assert record == records[cast(str, record["artifact_path"])]
    assert render_cli_media.MANIFEST_NAME not in {
        cast(str, record["artifact_path"]) for record in manifest_entries
    }
    assert records[render_cli_media.MANIFEST_NAME]["sha256"] == source["media_manifest_sha256"]
    provenance = cast(contract.JsonObject, _document()["provenance"])
    assert (
        provenance["source_commit"] == source["git_commit"]
        and provenance["source_tree"] == source["git_tree"]
    )
    _assert_adopted_png_contract(
        (ROOT / EXPECTED_ADOPTED_MEDIA[render_cli_media.PNG_NAME][0]).read_bytes()
    )
    _assert_adopted_gif_contract(
        (ROOT / EXPECTED_ADOPTED_MEDIA[render_cli_media.GIF_NAME][0]).read_bytes()
    )


def test_visual_dependency_is_exact_lazy_and_absent_from_runtime_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == []

    lock = (ROOT / "requirements-visuals.lock").read_text(encoding="utf-8")
    assert "pillow==12.3.0" in lock
    assert "sha256:78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91" in lock
    assert lock.count("--hash=") == 1

    source = (ROOT / "tools/render_cli_media.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(module == "PIL" or module.startswith("PIL.") for module in imported_modules)
    assert 'importlib.import_module("PIL.Image")' in source
    assert 'sys.implementation.name != "cpython"' in source
    assert render_cli_media.EXPECTED_PYTHON == (3, 12, 3)
    assert render_cli_media.EXPECTED_PILLOW_VERSION == "12.3.0"
    assert render_cli_media.EXPECTED_FONT_NAME == ("Aileron", "Regular")

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "MIT-CMU" in notices
    assert "Aileron Regular" in notices
    assert "No Rights Reserved" in notices
    assert "does not copy a font file" in notices


def test_hosted_media_job_is_source_explicit_hash_locked_and_adoption_aware() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    required = {
        "installed-wheel-media:",
        "needs: verify",
        "python-version: 3.12.3",
        "requirements-visuals.lock",
        "python tools/capture_cli_evidence.py",
        "python tools/render_cli_evidence.py --write",
        'python "$source_root/tools/render_cli_media.py"',
        "Compare the two byte-identical six-file bundles",
        "Compare replay with adopted installed-wheel media",
        "installed-wheel-media-",
        "github.run_id",
        "retention-days: 1",
        "compression-level: 0",
    }
    assert all(value in workflow for value in required)
    assert "persist-credentials: false" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "github.event.pull_request.head.sha || github.sha" in workflow
    assert "^[0-9a-f]{40}$" in workflow
    assert 'git -C "$source_root" diff --name-only | LC_ALL=C sort' in workflow
    assert 'git -C "$source_root" diff --cached --name-only' in workflow
    assert "ls-files --others --exclude-standard" in workflow
    assert "grep -v '^ M docs/visuals/'" not in workflow
