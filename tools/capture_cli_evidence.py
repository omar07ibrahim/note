#!/usr/bin/env python3
"""Capture a validated RecallLedger workflow from a clean installed wheel."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import csv
import hashlib
import io
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Final, NoReturn, cast

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import cli_evidence_contract as contract  # noqa: E402

DEFAULT_EVIDENCE_PATH: Final = (
    ROOT / "docs" / "visuals" / "evidence" / "installed-wheel-cli.v1.json"
)
ARTIFACTS_DIRECTORY: Final = ROOT / "artifacts"
EXPECTED_WHEEL_NAME: Final = "recall_ledger-0.1.0-py3-none-any.whl"
EXPECTED_BUILD_VERSION: Final = "1.3.0"
EXPECTED_SETUPTOOLS_VERSION: Final = "83.0.0"
PORTABLE_RUNTIME_PROVENANCE_PATHS: Final = (
    ("provenance", "builder", "python_version"),
    ("provenance", "installation", "pip_version"),
    ("provenance", "installation", "python_version"),
    ("provenance", "installation", "sqlite_version"),
)
_PORTABLE_RUNTIME_SENTINEL: Final = "<PORTABLE_RUNTIME_PROVENANCE>"
_PYTHON_SERIES_COMPONENTS: Final = 2
EXPECTED_WHEEL_FILES: Final = frozenset(
    {
        "recall_ledger/__init__.py",
        "recall_ledger/_ledger_operations.py",
        "recall_ledger/cli.py",
        "recall_ledger/events.py",
        "recall_ledger/py.typed",
        "recall_ledger/retrieval.py",
        "recall_ledger/storage.py",
        "recall_ledger-0.1.0.dist-info/METADATA",
        "recall_ledger-0.1.0.dist-info/RECORD",
        "recall_ledger-0.1.0.dist-info/WHEEL",
        "recall_ledger-0.1.0.dist-info/entry_points.txt",
        "recall_ledger-0.1.0.dist-info/licenses/LICENSE",
        "recall_ledger-0.1.0.dist-info/top_level.txt",
    }
)
EXPECTED_PACKAGE_FILES: Final = frozenset(
    {
        "__init__.py",
        "_ledger_operations.py",
        "cli.py",
        "events.py",
        "py.typed",
        "retrieval.py",
        "storage.py",
    }
)
CAPTURE_INPUT_EXACT: Final = frozenset(
    {
        "pyproject.toml",
        "tools/capture_cli_evidence.py",
        "tools/cli_evidence_contract.py",
        "tools/render_cli_evidence.py",
        *contract.FIXTURE_FILES.values(),
    }
)
CAPTURE_INPUT_PREFIX: Final = "src/recall_ledger/"
MAX_SUBPROCESS_BYTES: Final = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES: Final = 32 * 1024 * 1024
MAX_ARCHIVE_FILES: Final = 512
MAX_ARCHIVE_FILE_BYTES: Final = 4 * 1024 * 1024
MAX_WHEEL_BYTES: Final = 8 * 1024 * 1024
MAX_WHEEL_FILES: Final = 64
RECORD_COLUMNS: Final = 3
COMMAND_TIMEOUT_SECONDS: Final = 30
BUILD_TIMEOUT_SECONDS: Final = 120
_OID_PATTERN: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_NOTE_PATTERN: Final = re.compile(r"nt_[0-9a-f]{32}\Z")
_RECORD_DIGEST_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SAFE_TEMP_PREFIX: Final = "cli-evidence-"
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)


class CaptureError(RuntimeError):
    """The clean-source capture could not be proven."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded subprocess result."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class WheelEvidence:
    """Validated wheel metadata used by the final provenance record."""

    path: Path
    sha256: str
    size_bytes: int
    record_sha256: str
    package_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class InstallationEvidence:
    """Externally measured installed executable state."""

    console_script: Path
    console_script_sha256: str
    console_script_normalized_sha256: str
    package_directory: Path
    package_manifest_sha256: str


def _fail(message: str) -> NoReturn:
    raise CaptureError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bounded(path: Path, *, maximum: int, context: str) -> bytes:
    try:
        descriptor = os.open(path, _READ_FLAGS)
        metadata_value = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_value.st_mode) or metadata_value.st_nlink != 1:
            os.close(descriptor)
            _fail(f"{context} must be one regular single-link file")
        chunks = bytearray()
        with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as stream:
            while len(chunks) <= maximum:
                chunk = stream.read(min(65_536, maximum + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
    except CaptureError:
        raise
    except OSError:
        _fail(f"{context} is unavailable or unsafe")
    if len(chunks) > maximum:
        _fail(f"{context} exceeds the byte limit")
    return bytes(chunks)


def _sanitized_environment(
    *,
    home: Path,
    temporary: Path,
    path: str,
    source_date_epoch: int | None = None,
) -> dict[str, str]:
    environment = {
        "HOME": os.fspath(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": path,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": os.fspath(temporary),
        "TZ": "UTC",
    }
    if source_date_epoch is not None:
        environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the session created for one bounded command, including descendants."""

    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.wait()


def _run(  # noqa: PLR0912, PLR0913, PLR0915 - bounded pipe pump stays explicit
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    input_bytes: bytes | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    maximum_output_bytes: int = MAX_SUBPROCESS_BYTES,
) -> ProcessResult:
    if not argv or any(type(item) is not str or "\x00" in item for item in argv):
        _fail("subprocess argv is empty or malformed")
    if (
        type(maximum_output_bytes) is not int
        or maximum_output_bytes < 1
        or (input_bytes is not None and len(input_bytes) > MAX_SUBPROCESS_BYTES)
    ):
        _fail("subprocess byte budget is malformed or exceeded")
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    try:
        process = subprocess.Popen(  # noqa: S603 - closed argv; shell is never used
            list(argv),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            _fail("evidence subprocess pipes are unavailable")
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout))
        selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr))
        input_offset = 0
        if input_bytes is not None:
            if process.stdin is None:
                _fail("evidence subprocess stdin is unavailable")
            os.set_blocking(process.stdin.fileno(), False)
            if input_bytes:
                selector.register(process.stdin, selectors.EVENT_WRITE, ("stdin", None))
            else:
                process.stdin.close()
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("a bounded evidence subprocess timed out")
            for key, _mask in selector.select(min(remaining, 0.5)):
                label, buffer = cast(
                    tuple[str, bytearray | None],
                    key.data,
                )
                stream = cast(io.BufferedIOBase, key.fileobj)
                if label == "stdin":
                    if input_bytes is None:
                        _fail("evidence subprocess stdin state is invalid")
                    try:
                        written = os.write(
                            stream.fileno(),
                            input_bytes[input_offset : input_offset + 65_536],
                        )
                    except BrokenPipeError:
                        written = 0
                    input_offset += written
                    if written == 0 or input_offset == len(input_bytes):
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if buffer is None:
                    _fail("evidence subprocess channel state is invalid")
                buffer.extend(chunk)
                if len(stdout) + len(stderr) > maximum_output_bytes:
                    _fail("an evidence subprocess exceeded the output byte limit")
        remaining = max(0.001, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
        _terminate_process_group(process)
    except CaptureError:
        if process is not None:
            _terminate_process_group(process)
        raise
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            _terminate_process_group(process)
        _fail("a bounded evidence subprocess could not complete")
    finally:
        selector.close()
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
    return ProcessResult(
        returncode=returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
    )


def _require_success(result: ProcessResult, context: str) -> bytes:
    if result.returncode != 0:
        _fail(f"{context} failed")
    return result.stdout


def _git(
    root: Path,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    input_bytes: bytes | None = None,
) -> bytes:
    return _require_success(
        _run(
            ("git", *arguments),
            cwd=root,
            environment=environment,
            input_bytes=input_bytes,
        ),
        "Git provenance command",
    )


def _resolve_source(
    root: Path,
    source_commit: str,
    *,
    environment: Mapping[str, str],
) -> tuple[str, str, int]:
    if _OID_PATTERN.fullmatch(source_commit) is None:
        _fail("--source-commit must be one full canonical Git object identifier")
    resolved = (
        _git(
            root,
            ("rev-parse", "--verify", f"{source_commit}^{{commit}}"),
            environment=environment,
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    if resolved != source_commit:
        _fail("--source-commit did not resolve to the exact requested commit")
    tree = (
        _git(
            root,
            ("rev-parse", f"{source_commit}^{{tree}}"),
            environment=environment,
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    epoch_text = (
        _git(
            root,
            ("show", "-s", "--format=%ct", source_commit),
            environment=environment,
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    if (
        _OID_PATTERN.fullmatch(tree) is None
        or not epoch_text.isascii()
        or not epoch_text.isdecimal()
    ):
        _fail("Git provenance output is malformed")
    epoch = int(epoch_text)
    if epoch <= 0:
        _fail("source commit timestamp is outside the supported range")
    return resolved, tree, epoch


def _archive_source(
    root: Path,
    source_commit: str,
    *,
    environment: Mapping[str, str],
) -> bytes:
    archive = _git(
        root,
        ("archive", "--format=tar", "--prefix=source/", source_commit),
        environment=environment,
    )
    if not 1 <= len(archive) <= MAX_ARCHIVE_BYTES:
        _fail("Git source archive is outside the byte bound")
    embedded = (
        _git(
            root,
            ("get-tar-commit-id",),
            environment=environment,
            input_bytes=archive,
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    if embedded != source_commit:
        _fail("Git archive did not retain the requested source commit")
    return archive


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "source":
        _fail("Git archive contains an unsafe member path")
    if path.as_posix() != name.rstrip("/"):
        _fail("Git archive contains a noncanonical member path")
    return path


def _extract_archive(archive: bytes, destination: Path) -> Path:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            members = source.getmembers()
            if not 1 <= len(members) <= MAX_ARCHIVE_FILES:
                _fail("Git archive member count is outside the supported range")
            for member in members:
                relative = _safe_archive_path(member.name)
                output = destination.joinpath(*relative.parts)
                if member.isdir():
                    output.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                if not member.isfile() or not 0 <= member.size <= MAX_ARCHIVE_FILE_BYTES:
                    _fail("Git archive contains an unsupported member")
                parent = output.parent
                parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                stream = source.extractfile(member)
                if stream is None:
                    _fail("Git archive regular member is unavailable")
                payload = stream.read(MAX_ARCHIVE_FILE_BYTES + 1)
                if len(payload) != member.size or len(payload) > MAX_ARCHIVE_FILE_BYTES:
                    _fail("Git archive member size changed during extraction")
                output.write_bytes(payload)
                output.chmod(0o644)
    except CaptureError:
        raise
    except (OSError, tarfile.TarError):
        _fail("Git source archive could not be safely extracted")
    extracted = destination / "source"
    if not extracted.is_dir() or extracted.is_symlink():
        _fail("Git archive did not produce one source directory")
    return extracted


def _capture_input_paths(source: Path) -> tuple[Path, ...]:
    paths = []
    for path in source.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source).as_posix()
        runtime_relative = relative.removeprefix(CAPTURE_INPUT_PREFIX)
        runtime_source = (
            relative.startswith(CAPTURE_INPUT_PREFIX)
            and "__pycache__" not in PurePosixPath(runtime_relative).parts
            and runtime_relative.endswith((".py", ".pyi", "py.typed"))
        )
        if relative in CAPTURE_INPUT_EXACT or runtime_source:
            paths.append(path)
    relative_paths = {path.relative_to(source).as_posix() for path in paths}
    missing = sorted(CAPTURE_INPUT_EXACT - relative_paths)
    if missing:
        _fail("source commit is missing a required capture input")
    if not any(path.startswith(CAPTURE_INPUT_PREFIX) for path in relative_paths):
        _fail("source commit contains no RecallLedger runtime package")
    return tuple(sorted(paths, key=lambda item: item.relative_to(source).as_posix()))


def _capture_input_manifest(source: Path) -> contract.JsonObject:
    entries: list[contract.JsonValue] = []
    for path in _capture_input_paths(source):
        payload = _read_bounded(
            path,
            maximum=MAX_ARCHIVE_FILE_BYTES,
            context="capture input",
        )
        entries.append(
            {
                "path": path.relative_to(source).as_posix(),
                "sha256": _sha256(payload),
                "size_bytes": len(payload),
            }
        )
    digest = _sha256(contract.canonical_json_bytes(entries))
    return {"files": entries, "sha256": digest}


def current_capture_input_digest(root: Path = ROOT) -> str:
    """Hash only current executable capture inputs, excluding generated evidence."""

    return cast(str, _capture_input_manifest(root)["sha256"])


def _builder_versions() -> tuple[str, str]:
    try:
        build_version = metadata.version("build")
        setuptools_version = metadata.version("setuptools")
    except metadata.PackageNotFoundError:
        _fail("the pinned local build toolchain is unavailable")
    if build_version != EXPECTED_BUILD_VERSION or setuptools_version != EXPECTED_SETUPTOOLS_VERSION:
        _fail("the local build toolchain does not match the pinned project versions")
    return build_version, setuptools_version


def _build_wheel(
    source: Path,
    *,
    builder_home: Path,
    temporary: Path,
    source_date_epoch: int,
) -> Path:
    output = source.parent / "dist"
    output.mkdir(mode=0o755)
    environment = _sanitized_environment(
        home=builder_home,
        temporary=temporary,
        path="/usr/bin:/bin",
        source_date_epoch=source_date_epoch,
    )
    result = _run(
        (
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--skip-dependency-check",
            "--outdir",
            os.fspath(output),
            os.fspath(source),
        ),
        cwd=source.parent,
        environment=environment,
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    _require_success(result, "deterministic wheel build")
    wheels = tuple(output.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != EXPECTED_WHEEL_NAME:
        _fail("clean source did not produce the one expected wheel")
    return wheels[0]


def _decode_record_hash(value: str) -> bytes:
    if not value.startswith("sha256="):
        _fail("wheel RECORD uses an unsupported digest")
    encoded = value.removeprefix("sha256=")
    if _RECORD_DIGEST_PATTERN.fullmatch(encoded) is None:
        _fail("wheel RECORD digest is not canonical URL-safe base64")
    try:
        decoded = base64.b64decode(
            encoded + "=",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        _fail("wheel RECORD digest is malformed")
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != encoded:
        _fail("wheel RECORD digest has non-zero base64 pad bits")
    return decoded


def _package_manifest_digest(files: Mapping[str, bytes]) -> str:
    entries: list[contract.JsonValue] = [
        {
            "path": name.removeprefix("recall_ledger/"),
            "sha256": _sha256(files[name]),
            "size_bytes": len(files[name]),
        }
        for name in sorted(files)
        if name.startswith("recall_ledger/")
    ]
    return _sha256(contract.canonical_json_bytes(entries))


def _validate_wheel(path: Path) -> WheelEvidence:  # noqa: PLR0912
    payload = _read_bounded(path, maximum=MAX_WHEEL_BYTES, context="built wheel")
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as wheel:
            infos = wheel.infolist()
            names = [info.filename for info in infos]
            if not 1 <= len(infos) <= MAX_WHEEL_FILES or len(names) != len(set(names)):
                _fail("wheel file list is empty, excessive, or duplicated")
            if frozenset(names) != EXPECTED_WHEEL_FILES:
                _fail("wheel contains an unexpected or missing file")
            files: dict[str, bytes] = {}
            for info in infos:
                pure = PurePosixPath(info.filename)
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or info.is_dir()
                    or info.file_size > MAX_ARCHIVE_FILE_BYTES
                ):
                    _fail("wheel contains an unsafe entry")
                files[info.filename] = wheel.read(info)
    except CaptureError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        _fail("built wheel is not a readable bounded ZIP archive")

    record_name = "recall_ledger-0.1.0.dist-info/RECORD"
    record = files[record_name]
    try:
        rows = list(csv.reader(io.StringIO(record.decode("utf-8", errors="strict"))))
    except (UnicodeDecodeError, csv.Error):
        _fail("wheel RECORD is not strict UTF-8 CSV")
    if len(rows) != len(files):
        _fail("wheel RECORD does not enumerate every member exactly once")
    seen: set[str] = set()
    for row in rows:
        if len(row) != RECORD_COLUMNS or row[0] in seen or row[0] not in files:
            _fail("wheel RECORD row is malformed or duplicated")
        seen.add(row[0])
        if row[0] == record_name:
            if row[1:] != ["", ""]:
                _fail("wheel RECORD self-entry must omit digest and size")
            continue
        if not row[2].isdecimal() or int(row[2]) != len(files[row[0]]):
            _fail("wheel RECORD size does not match its member")
        if _decode_record_hash(row[1]) != hashlib.sha256(files[row[0]]).digest():
            _fail("wheel RECORD digest does not match its member")

    return WheelEvidence(
        path=path,
        sha256=_sha256(payload),
        size_bytes=len(payload),
        record_sha256=_sha256(record),
        package_manifest_sha256=_package_manifest_digest(files),
    )


_INSTALLATION_METADATA_PROBE: Final = """
import importlib.metadata as metadata
import json
import platform
import sqlite3

distribution = metadata.distribution("recall-ledger")
entry_points = sorted(
    f"{entry.name} = {entry.value}"
    for entry in distribution.entry_points
    if entry.group == "console_scripts"
)
result = {
    "console_scripts": entry_points,
    "distribution": distribution.metadata["Name"],
    "pip_version": metadata.version("pip"),
    "python_version": platform.python_version(),
    "sqlite_version": sqlite3.sqlite_version,
    "version": metadata.version("recall-ledger"),
}
print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
""".strip()

_MODULE_ORIGIN_PROBE: Final = """
import json
import pathlib
import recall_ledger
import sys

prefix = pathlib.Path(sys.prefix).resolve()
module = pathlib.Path(recall_ledger.__file__).resolve()
if not module.is_relative_to(prefix):
    raise SystemExit(2)
print(
    json.dumps(
        {"module_relative": module.relative_to(prefix).as_posix()},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
)
""".strip()

_PURELIB_PROBE: Final = (
    'import sysconfig;value=sysconfig.get_path("purelib");assert value is not None;print(value)'
)

_DECODE_PROBE: Final = """
import json
import sys
from recall_ledger.events import decode_event

values = json.loads(sys.stdin.buffer.read())
digests = []
for value in values:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digests.append(decode_event(raw).event_hash)
print(
    json.dumps(
        {"digests": digests},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
)
""".strip()


def _installed_package_manifest(package_directory: Path) -> str:
    try:
        directory = os.open(package_directory, _DIRECTORY_FLAGS)
        metadata_value = os.fstat(directory)
        if not stat.S_ISDIR(metadata_value.st_mode):
            os.close(directory)
            _fail("installed package path is not a directory")
        names = os.listdir(directory)  # noqa: PTH208 - descriptor pins the directory
        if frozenset(names) != EXPECTED_PACKAGE_FILES:
            os.close(directory)
            _fail("installed package contains an unexpected or missing file")
        entries: list[contract.JsonValue] = []
        for name in sorted(names):
            descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
            file_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(file_metadata.st_mode) or file_metadata.st_nlink != 1:
                os.close(descriptor)
                os.close(directory)
                _fail("installed package member is not one regular single-link file")
            payload = bytearray()
            with os.fdopen(descriptor, "rb", buffering=0, closefd=True) as stream:
                while len(payload) <= MAX_ARCHIVE_FILE_BYTES:
                    chunk = stream.read(
                        min(
                            65_536,
                            MAX_ARCHIVE_FILE_BYTES + 1 - len(payload),
                        )
                    )
                    if not chunk:
                        break
                    payload.extend(chunk)
            if len(payload) > MAX_ARCHIVE_FILE_BYTES:
                os.close(directory)
                _fail("installed package member exceeds the byte limit")
            entries.append(
                {
                    "path": name,
                    "sha256": _sha256(bytes(payload)),
                    "size_bytes": len(payload),
                }
            )
        os.close(directory)
    except CaptureError:
        raise
    except OSError:
        _fail("installed package could not be measured safely")
    return _sha256(contract.canonical_json_bytes(entries))


def _installed_paths(
    *,
    python: Path,
    runtime: Path,
    environment: Mapping[str, str],
) -> tuple[Path, Path]:
    probe = _run(
        (os.fspath(python), "-I", "-c", _PURELIB_PROBE),
        cwd=runtime.parent,
        environment=environment,
    )
    raw = _require_success(probe, "isolated purelib path probe")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("isolated purelib path is not strict UTF-8")
    if not text.endswith("\n") or text.count("\n") != 1:
        _fail("isolated purelib path probe is malformed")
    purelib = Path(text.rstrip("\n"))
    if not purelib.is_absolute():
        _fail("isolated purelib path is not absolute")
    try:
        purelib.resolve(strict=True).relative_to(runtime.resolve(strict=True))
    except (OSError, ValueError):
        _fail("isolated purelib path escaped the runtime environment")
    return purelib / "recall_ledger", runtime / "bin" / "recall-ledger"


def _measure_installation(
    *,
    python: Path,
    runtime: Path,
    environment: Mapping[str, str],
) -> InstallationEvidence:
    package_directory, console_script = _installed_paths(
        python=python,
        runtime=runtime,
        environment=environment,
    )
    package_digest = _installed_package_manifest(package_directory)
    console_payload = _read_bounded(
        console_script,
        maximum=65_536,
        context="installed console script",
    )
    try:
        wrapper = console_payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("installed console script is not strict UTF-8")
    first_line, separator, remaining_wrapper = wrapper.partition("\n")
    if (
        not separator
        or not first_line.startswith("#!")
        or "from recall_ledger.cli import main" not in wrapper
    ):
        _fail("installed console script wrapper does not match its entry point")
    interpreter = Path(first_line.removeprefix("#!"))
    if not interpreter.is_absolute() or interpreter != runtime / "bin" / "python":
        _fail("installed console script interpreter escaped the runtime")
    normalized_wrapper = f"#!<VENV>/bin/python\n{remaining_wrapper}".encode()
    return InstallationEvidence(
        console_script=console_script,
        console_script_sha256=_sha256(console_payload),
        console_script_normalized_sha256=_sha256(normalized_wrapper),
        package_directory=package_directory,
        package_manifest_sha256=package_digest,
    )


def _verify_installation_unchanged(evidence: InstallationEvidence) -> None:
    if _installed_package_manifest(evidence.package_directory) != evidence.package_manifest_sha256:
        _fail("installed package files changed during evidence capture")
    console_payload = _read_bounded(
        evidence.console_script,
        maximum=65_536,
        context="installed console script",
    )
    if _sha256(console_payload) != evidence.console_script_sha256:
        _fail("installed console script changed during evidence capture")


def _install_wheel(
    wheel: WheelEvidence,
    *,
    runtime: Path,
    home: Path,
    temporary: Path,
) -> tuple[InstallationEvidence, contract.JsonObject]:
    venv_result = _run(
        (sys.executable, "-m", "venv", os.fspath(runtime)),
        cwd=runtime.parent,
        environment=_sanitized_environment(
            home=home,
            temporary=temporary,
            path="/usr/bin:/bin",
        ),
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    _require_success(venv_result, "isolated virtual environment creation")
    python = runtime / "bin" / "python"
    environment = _sanitized_environment(
        home=home,
        temporary=temporary,
        path=f"{runtime / 'bin'}:/usr/bin:/bin",
    )
    install_result = _run(
        (
            os.fspath(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            os.fspath(wheel.path),
        ),
        cwd=runtime.parent,
        environment=environment,
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    _require_success(install_result, "offline wheel installation")
    installation = _measure_installation(
        python=python,
        runtime=runtime,
        environment=environment,
    )
    if installation.package_manifest_sha256 != wheel.package_manifest_sha256:
        _fail("installed runtime files differ from the built wheel")
    metadata_probe = _run(
        (os.fspath(python), "-I", "-B", "-c", _INSTALLATION_METADATA_PROBE),
        cwd=runtime.parent,
        environment=environment,
    )
    probe_raw = _require_success(
        metadata_probe,
        "installed distribution metadata probe",
    )
    provenance = contract.decode_canonical_document(
        probe_raw,
        context="installed distribution metadata",
    )
    expected_keys = {
        "console_scripts",
        "distribution",
        "pip_version",
        "python_version",
        "sqlite_version",
        "version",
    }
    if set(provenance) != expected_keys:
        _fail("installed package provenance has an unexpected shape")
    if provenance["distribution"] != "recall-ledger" or provenance["version"] != "0.1.0":
        _fail("installed distribution identity does not match the project")
    if provenance["console_scripts"] != ["recall-ledger = recall_ledger.cli:main"]:
        _fail("installed console-script entry point does not match the project")

    module_probe = _run(
        (os.fspath(python), "-I", "-B", "-c", _MODULE_ORIGIN_PROBE),
        cwd=runtime.parent,
        environment=environment,
    )
    module_document = contract.decode_canonical_document(
        _require_success(module_probe, "installed module-origin probe"),
        context="installed module origin",
    )
    if set(module_document) != {"module_relative"}:
        _fail("installed module origin has an unexpected shape")
    module_relative = module_document["module_relative"]
    if type(module_relative) is not str or not module_relative.endswith(
        "/recall_ledger/__init__.py"
    ):
        _fail("installed module did not resolve inside the isolated environment")
    provenance["module_relative"] = module_relative
    _verify_installation_unchanged(installation)
    provenance["installed_files_sha256"] = installation.package_manifest_sha256
    provenance["console_script_normalized_sha256"] = installation.console_script_normalized_sha256
    return installation, provenance


def _copy_fixture(source: Path, destination: Path) -> None:
    payload = _read_bounded(
        source,
        maximum=contract.MAX_CHANNEL_BYTES,
        context="source fixture",
    )
    contract.decode_canonical_document(payload, context="source fixture")
    try:
        with destination.open("xb") as stream:
            stream.write(payload)
        destination.chmod(0o600)
    except OSError:
        _fail("capture fixture could not be created safely")


def _invoke(  # noqa: PLR0913 - invocation context is explicit
    *,
    identifier: str,
    console_script: Path,
    data_directory: Path,
    tenant_id: str,
    command: Sequence[str],
    input_fixture: str | None,
    cwd: Path,
    environment: Mapping[str, str],
) -> contract.Invocation:
    argv = (
        os.fspath(console_script),
        "--data-dir",
        os.fspath(data_directory),
        "--tenant-id",
        tenant_id,
        *command,
    )
    result = _run(argv, cwd=cwd, environment=environment)
    return contract.Invocation(
        identifier=identifier,
        argv=argv,
        input_fixture=input_fixture,
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _extract_created_note(invocation: contract.Invocation) -> str:
    if invocation.exit_code != 0 or invocation.stderr:
        _fail("create did not produce a successful stdout document")
    result = contract.decode_canonical_document(
        invocation.stdout,
        context="create stdout",
    )
    event_value = result.get("event")
    if type(event_value) is not dict:
        _fail("create stdout did not contain an event")
    note_value = event_value.get("note_id")
    if type(note_value) is not str or _NOTE_PATTERN.fullmatch(note_value) is None:
        _fail("create stdout did not contain a canonical note identifier")
    return note_value


def _run_scenario(  # noqa: PLR0913 - isolated runtime boundaries stay explicit
    *,
    source: Path,
    console_script: Path,
    runtime: Path,
    runner: Path,
    home: Path,
    temporary: Path,
) -> tuple[
    contract.ValidatedScenario,
    dict[str, contract.JsonObject],
]:
    fixtures_directory = runner / "fixtures"
    data_directory = runner / "data"
    fixtures_directory.mkdir(mode=0o700)
    data_directory.mkdir(mode=0o700)
    fixture_paths: dict[str, Path] = {}
    fixtures: dict[str, contract.JsonObject] = {}
    for fixture_id, relative in contract.FIXTURE_FILES.items():
        destination = fixtures_directory / Path(relative).name
        _copy_fixture(source / relative, destination)
        fixture_paths[fixture_id] = destination
        fixtures[fixture_id] = contract.load_fixture(destination)

    environment = _sanitized_environment(
        home=home,
        temporary=temporary,
        path=f"{runtime / 'bin'}:/usr/bin:/bin",
    )
    invocations: list[contract.Invocation] = []
    create_command = (
        "create",
        "--command-id",
        contract.CREATE_COMMAND_ID,
        "--content-file",
        os.fspath(fixture_paths["content-v1"]),
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[0],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=create_command,
            input_fixture="content-v1",
            cwd=runner,
            environment=environment,
        )
    )
    note_id = _extract_created_note(invocations[0])
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[1],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=create_command,
            input_fixture="content-v1",
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[2],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=(
                "revise",
                "--note-id",
                note_id,
                "--command-id",
                contract.REVISE_COMMAND_ID,
                "--expected-revision",
                "1",
                "--content-file",
                os.fspath(fixture_paths["content-v2"]),
            ),
            input_fixture="content-v2",
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[3],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=(
                "revise",
                "--note-id",
                note_id,
                "--command-id",
                contract.STALE_COMMAND_ID,
                "--expected-revision",
                "1",
                "--content-file",
                os.fspath(fixture_paths["content-stale"]),
            ),
            input_fixture="content-stale",
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[4],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=("head", "--note-id", note_id),
            input_fixture=None,
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[5],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=(
                "history",
                "--note-id",
                note_id,
                "--after-revision",
                "0",
                "--limit",
                "1",
            ),
            input_fixture=None,
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[6],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=(
                "history",
                "--note-id",
                note_id,
                "--after-revision",
                "1",
                "--limit",
                "1",
                "--jsonl",
            ),
            input_fixture=None,
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[7],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=(
                "tombstone",
                "--note-id",
                note_id,
                "--command-id",
                contract.TOMBSTONE_COMMAND_ID,
                "--expected-revision",
                "2",
                "--reason",
                "user_request",
            ),
            input_fixture=None,
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[8],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=("get", "--note-id", note_id),
            input_fixture=None,
            cwd=runner,
            environment=environment,
        )
    )
    invocations.append(
        _invoke(
            identifier=contract.STEP_IDS[9],
            console_script=console_script,
            data_directory=data_directory,
            tenant_id=contract.TENANT_ID,
            command=("head", "--note-id", note_id),
            input_fixture=None,
            cwd=runner,
            environment=environment,
        )
    )
    context = contract.CaptureContext(
        executable=os.fspath(console_script),
        data_directory=os.fspath(data_directory),
        fixture_paths=tuple(
            (fixture_id, os.fspath(fixture_paths[fixture_id]))
            for fixture_id in contract.FIXTURE_FILES
        ),
    )
    return (
        contract.validate_raw_scenario(
            tuple(invocations),
            fixtures,
            context,
        ),
        fixtures,
    )


def _verify_installed_decode_array(
    scenario: contract.ValidatedScenario,
    *,
    runtime: Path,
    cwd: Path,
    environment: Mapping[str, str],
) -> None:
    events: list[contract.JsonValue] = list(scenario.events)
    result = _run(
        (
            os.fspath(runtime / "bin" / "python"),
            "-I",
            "-B",
            "-c",
            _DECODE_PROBE,
        ),
        cwd=cwd,
        environment=environment,
        input_bytes=contract.canonical_json_bytes(events),
    )
    decoded = contract.decode_canonical_document(
        _require_success(result, "installed event decoder"),
        context="installed event decoder result",
    )
    if decoded != {"digests": list(scenario.event_hashes)}:
        _fail("installed wheel did not decode the independently verified events")


def _provenance(  # noqa: PLR0913 - provenance inputs stay named
    *,
    source_commit: str,
    source_tree: str,
    source_date_epoch: int,
    archive: bytes,
    capture_inputs: contract.JsonObject,
    wheel: WheelEvidence,
    build_version: str,
    setuptools_version: str,
    installation_probe: contract.JsonObject,
) -> contract.JsonObject:
    return {
        "builder": {
            "build_version": build_version,
            "python_version": ".".join(str(value) for value in sys.version_info[:3]),
            "setuptools_version": setuptools_version,
        },
        "capture_inputs": capture_inputs,
        "installation": {
            "console_script": "recall-ledger = recall_ledger.cli:main",
            "console_script_normalized_sha256": cast(
                str,
                installation_probe["console_script_normalized_sha256"],
            ),
            "distribution": "recall-ledger",
            "installed_files_sha256": cast(str, installation_probe["installed_files_sha256"]),
            "method": "pip --no-index --no-deps --no-compile",
            "module_origin": "installed-venv/recall_ledger/__init__.py",
            "pip_version": cast(str, installation_probe["pip_version"]),
            "python_version": cast(str, installation_probe["python_version"]),
            "sqlite_version": cast(str, installation_probe["sqlite_version"]),
            "version": "0.1.0",
        },
        "source_archive": {
            "format": "git-archive-tar",
            "sha256": _sha256(archive),
            "size_bytes": len(archive),
        },
        "source_commit": source_commit,
        "source_date_epoch": source_date_epoch,
        "source_tree": source_tree,
        "wheel": {
            "filename": EXPECTED_WHEEL_NAME,
            "record_sha256": wheel.record_sha256,
            "sha256": wheel.sha256,
            "size_bytes": wheel.size_bytes,
        },
    }


def _clear_directory(directory: int) -> None:
    try:
        names = os.listdir(directory)
        for name in names:
            metadata_value = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(metadata_value.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
                try:
                    _clear_directory(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=directory)
            else:
                os.unlink(name, dir_fd=directory)
    except OSError:
        _fail("private capture workspace could not be cleaned safely")


@contextmanager
def _private_workspace() -> Iterator[Path]:
    artifacts = _open_directory(
        ARTIFACTS_DIRECTORY,
        create=True,
        create_mode=0o700,
    )
    workspace = -1
    name = f"{_SAFE_TEMP_PREFIX}{secrets.token_hex(12)}"
    try:
        artifacts_metadata = os.fstat(artifacts)
        if not stat.S_ISDIR(artifacts_metadata.st_mode) or artifacts_metadata.st_mode & 0o077:
            _fail("artifacts directory must be a private owner-only directory")
        os.mkdir(name, mode=0o700, dir_fd=artifacts)
        workspace = os.open(name, _DIRECTORY_FLAGS, dir_fd=artifacts)
        workspace_metadata = os.fstat(workspace)
        if not stat.S_ISDIR(workspace_metadata.st_mode) or workspace_metadata.st_mode & 0o077:
            _fail("capture workspace must be a private owner-only directory")
        # Build and venv tools need the canonical path for interpreter
        # shebangs. The directory itself was created relative to the pinned
        # no-follow parent. A concurrent same-authority rename remains outside
        # this local operator evidence boundary because that actor could also
        # rewrite any installed file after measurement.
        workspace_path = ARTIFACTS_DIRECTORY / name
        path_metadata = workspace_path.stat(follow_symlinks=False)
        if (
            path_metadata.st_dev != workspace_metadata.st_dev
            or path_metadata.st_ino != workspace_metadata.st_ino
        ):
            _fail("capture workspace path does not match the pinned directory")
        yield workspace_path
        final_metadata = workspace_path.stat(follow_symlinks=False)
        if (
            final_metadata.st_dev != workspace_metadata.st_dev
            or final_metadata.st_ino != workspace_metadata.st_ino
        ):
            _fail("capture workspace identity changed during execution")
    finally:
        if workspace >= 0:
            _clear_directory(workspace)
            os.close(workspace)
            try:
                os.rmdir(name, dir_fd=artifacts)
            except OSError:
                _fail("private capture workspace could not be removed safely")
        os.close(artifacts)


def capture_evidence(root: Path, source_commit: str) -> bytes:
    """Build, install, execute, verify, normalize, and serialize one capture."""

    root = root.resolve()
    if root != ROOT:
        _fail("capture root must be this repository")
    with _private_workspace() as workspace:
        home = workspace / "home"
        temporary = workspace / "tmp"
        extract = workspace / "extract"
        builder_home = workspace / "builder-home"
        runner = workspace / "runner"
        runtime = workspace / "runtime"
        for directory in (home, temporary, extract, builder_home, runner):
            directory.mkdir(mode=0o700)

        git_environment = _sanitized_environment(
            home=home,
            temporary=temporary,
            path="/usr/bin:/bin",
        )
        resolved_commit, source_tree, source_date_epoch = _resolve_source(
            root,
            source_commit,
            environment=git_environment,
        )
        archive = _archive_source(
            root,
            resolved_commit,
            environment=git_environment,
        )
        source = _extract_archive(archive, extract)
        capture_inputs = _capture_input_manifest(source)
        if capture_inputs["sha256"] != _capture_input_manifest(root)["sha256"]:
            _fail("executing harness or runtime inputs differ from the source commit")
        build_version, setuptools_version = _builder_versions()
        wheel_path = _build_wheel(
            source,
            builder_home=builder_home,
            temporary=temporary,
            source_date_epoch=source_date_epoch,
        )
        wheel = _validate_wheel(wheel_path)
        installation, installation_probe = _install_wheel(
            wheel,
            runtime=runtime,
            home=home,
            temporary=temporary,
        )
        scenario, fixtures = _run_scenario(
            source=source,
            console_script=installation.console_script,
            runtime=runtime,
            runner=runner,
            home=home,
            temporary=temporary,
        )
        runtime_environment = _sanitized_environment(
            home=home,
            temporary=temporary,
            path=f"{runtime / 'bin'}:/usr/bin:/bin",
        )
        _verify_installed_decode_array(
            scenario,
            runtime=runtime,
            cwd=runner,
            environment=runtime_environment,
        )
        _verify_installation_unchanged(installation)
        provenance = _provenance(
            source_commit=resolved_commit,
            source_tree=source_tree,
            source_date_epoch=source_date_epoch,
            archive=archive,
            capture_inputs=capture_inputs,
            wheel=wheel,
            build_version=build_version,
            setuptools_version=setuptools_version,
            installation_probe=installation_probe,
        )
        evidence = contract.build_evidence_document(
            scenario,
            provenance=provenance,
            fixtures=fixtures,
        )
        return contract.canonical_json_bytes(evidence)


def _open_directory(
    path: Path,
    *,
    create: bool,
    create_mode: int = 0o755,
) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))  # noqa: PTH100
    if absolute.anchor != "/":
        _fail("evidence directory is not a canonical POSIX path")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=create_mode, dir_fd=descriptor)
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError:
        os.close(descriptor)
        _fail("evidence directory is unavailable or unsafe")
    return descriptor


def _atomic_write(path: Path, payload: bytes) -> None:
    if path != DEFAULT_EVIDENCE_PATH:
        _fail("evidence writer only supports the canonical output path")
    directory = _open_directory(path.parent, create=True)
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
            _fail("existing evidence output is not one regular single-link file")
        descriptor = os.open(
            temporary_name,
            _WRITE_FLAGS,
            0o644,
            dir_fd=directory,
        )
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                _fail("evidence output write made no progress")
            written += count
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except CaptureError:
        raise
    except OSError:
        _fail("evidence output could not be replaced atomically")
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


def _read_committed_evidence() -> bytes:
    return _read_bounded(
        DEFAULT_EVIDENCE_PATH,
        maximum=contract.MAX_CHANNEL_BYTES,
        context="committed CLI evidence",
    )


def _source_commit_from_evidence(raw: bytes) -> str:
    document = contract.decode_evidence_bytes(raw)
    provenance = cast(contract.JsonObject, document["provenance"])
    return cast(str, provenance["source_commit"])


def _check_current_inputs(document: contract.JsonObject) -> None:
    provenance = cast(contract.JsonObject, document["provenance"])
    inputs = cast(contract.JsonObject, provenance["capture_inputs"])
    if current_capture_input_digest(ROOT) != inputs["sha256"]:
        _fail("current executable capture inputs differ from the recorded source")


def _python_major_minor(version: str, *, context: str) -> tuple[int, int]:
    components = version.split(".")
    series = components[:_PYTHON_SERIES_COMPONENTS]
    if len(series) < _PYTHON_SERIES_COMPONENTS or not all(
        component.isdecimal() for component in series
    ):
        _fail(f"{context} is not a numeric Python major.minor version")
    return int(series[0]), int(series[1])


def _portable_python_series(document: contract.JsonObject, *, context: str) -> tuple[int, int]:
    provenance = cast(contract.JsonObject, document["provenance"])
    builder = cast(contract.JsonObject, provenance["builder"])
    installation = cast(contract.JsonObject, provenance["installation"])
    builder_python = cast(str, builder["python_version"])
    installed_python = cast(str, installation["python_version"])
    if builder_python != installed_python:
        _fail(f"{context} builder and installed Python versions differ")
    return _python_major_minor(builder_python, context=f"{context} Python version")


def _mask_portable_runtime_provenance(
    document: contract.JsonObject,
) -> contract.JsonObject:
    masked = copy.deepcopy(document)
    for path in PORTABLE_RUNTIME_PROVENANCE_PATHS:
        parent = masked
        for component in path[:-1]:
            child = parent.get(component)
            if not isinstance(child, dict):
                _fail("portable runtime provenance path is absent from validated evidence")
            parent = child
        leaf = path[-1]
        if not isinstance(parent.get(leaf), str):
            _fail("portable runtime provenance field is absent from validated evidence")
        parent[leaf] = _PORTABLE_RUNTIME_SENTINEL
    return masked


def _check_portable_runtime_evidence(committed: bytes, recaptured: bytes) -> None:
    committed_document = contract.decode_evidence_bytes(committed)
    recaptured_document = contract.decode_evidence_bytes(recaptured)
    committed_series = _portable_python_series(committed_document, context="committed evidence")
    recaptured_series = _portable_python_series(recaptured_document, context="recaptured evidence")
    if recaptured_series != committed_series:
        _fail("portable runtime check requires the recorded Python major.minor series")
    committed_comparable = contract.canonical_json_bytes(
        _mask_portable_runtime_provenance(committed_document)
    )
    recaptured_comparable = contract.canonical_json_bytes(
        _mask_portable_runtime_provenance(recaptured_document)
    )
    if recaptured_comparable != committed_comparable:
        _fail("committed CLI evidence differs outside portable runtime provenance")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture source-bound RecallLedger installed-wheel CLI evidence.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the canonical evidence JSON")
    mode.add_argument("--check", action="store_true", help="recapture and compare byte-for-byte")
    mode.add_argument(
        "--check-portable-runtime",
        action="store_true",
        help=("recapture and compare except the explicit runtime-version provenance allowlist"),
    )
    parser.add_argument(
        "--source-commit",
        help="full committed Git object ID; required for --write",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for deterministic evidence capture."""

    arguments = _parser().parse_args(argv)
    try:
        if arguments.write:
            if arguments.source_commit is None:
                _fail("--write requires --source-commit")
            payload = capture_evidence(ROOT, cast(str, arguments.source_commit))
            _atomic_write(DEFAULT_EVIDENCE_PATH, payload)
            return 0

        committed = _read_committed_evidence()
        document = contract.decode_evidence_bytes(committed)
        _check_current_inputs(document)
        recorded_commit = _source_commit_from_evidence(committed)
        if arguments.source_commit is not None and arguments.source_commit != recorded_commit:
            _fail("--source-commit does not match the committed evidence provenance")
        actual = capture_evidence(ROOT, recorded_commit)
        if arguments.check_portable_runtime:
            _check_portable_runtime_evidence(committed, actual)
        elif actual != committed:
            _fail("committed CLI evidence is not byte-for-byte current")
    except (CaptureError, contract.EvidenceContractError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
