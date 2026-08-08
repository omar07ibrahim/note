from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from tools import cli_evidence_contract as contract
from tools import render_cli_evidence, render_cli_media

ROOT = Path(__file__).resolve().parents[1]


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
