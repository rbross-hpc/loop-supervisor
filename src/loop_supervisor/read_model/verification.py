"""Secure discovery and explicitly requested bounded verification-log reads."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..state import StateError, validate_run_id, validate_verification_result

LOG_READ_LIMIT = 1024 * 1024
LOG_RENDER_BYTE_LIMIT = 256 * 1024
LOG_RENDER_LINE_LIMIT = 10_000
_MAX_LOG_LEAVES = 10_000
_MAX_COMMIT_DIRECTORIES = 10_000
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_LOG_RE = re.compile(r"^(?P<ordinal>[0-9]{2,})\.log$")


@dataclass(frozen=True)
class VerificationDiagnostic:
    """A safe logical artifact name and reason for unavailable evidence."""

    artifact: str
    reason: str


@dataclass(frozen=True)
class LogReference:
    """An authorized log identity, never an exposed filesystem path."""

    run_id: str
    commit: str
    ordinal: int
    _name: str


@dataclass(frozen=True)
class VerificationAttempt:
    """Validated compact command metadata and, where safe, its log reference."""

    commit: str
    ordinal: int
    command: str
    ok: bool
    returncode: int | None
    timed_out: bool
    duration: float
    summary: str
    log: LogReference | None


@dataclass(frozen=True)
class VerificationDiscovery:
    attempts: tuple[VerificationAttempt, ...]
    diagnostics: tuple[VerificationDiagnostic, ...]


@dataclass(frozen=True)
class LogContent:
    """An explicitly opened, bounded unredacted log result."""

    available: bool
    text: str
    byte_truncated: bool
    render_truncated: bool
    changed_during_read: bool
    diagnostic: str | None


def discover_verification(
    git_common_dir: Path, run_id: str, verification_result: object
) -> VerificationDiscovery:
    """Discover log leaves and associate only exact metadata-authorized leaves.

    The compact verification result is metadata only. Its persisted absolute
    output paths are normalized for equality with an expected in-tree path, but
    never opened or resolved.
    """
    validated_run = validate_run_id(run_id)
    try:
        validate_verification_result(verification_result)
    except StateError:
        return VerificationDiscovery(
            (), (VerificationDiagnostic(validated_run, "invalid verification metadata"),)
        )
    assert isinstance(verification_result, dict)
    commands = verification_result["commands"]
    assert isinstance(commands, list)
    diagnostics: list[VerificationDiagnostic] = []
    leaves = _discover_leaves(git_common_dir, validated_run, diagnostics)
    attempts: list[VerificationAttempt] = []
    for ordinal, command in enumerate(commands, start=1):
        assert isinstance(command, dict)
        output_path = command["output_path"]
        assert isinstance(output_path, str)
        identity = _output_identity(git_common_dir, validated_run, output_path, ordinal)
        if identity is None:
            diagnostics.append(
                VerificationDiagnostic(f"command-{ordinal}", "mismatched output path")
            )
            continue
        commit, name = identity
        leaf = leaves.get((commit, ordinal))
        if leaf != name:
            diagnostics.append(VerificationDiagnostic(name, "authorized log is unavailable"))
            continue
        attempts.append(
            VerificationAttempt(
                commit=commit,
                ordinal=ordinal,
                command=command["command"],  # validated above
                ok=command["ok"],
                returncode=command["returncode"],
                timed_out=command["timed_out"],
                duration=float(command["duration"]),
                summary=command["summary"],
                log=LogReference(validated_run, commit, ordinal, name),
            )
        )
    return VerificationDiscovery(tuple(attempts), tuple(diagnostics))


def read_log(git_common_dir: Path, reference: LogReference) -> LogContent:
    """Explicitly open one authorized log through no-follow descriptors."""
    try:
        run_id = validate_run_id(reference.run_id)
        if not _COMMIT_RE.fullmatch(reference.commit) or not _LOG_RE.fullmatch(reference._name):
            raise ValueError
        if int(reference._name[:-4]) != reference.ordinal or reference.ordinal <= 0:
            raise ValueError
    except (StateError, ValueError):
        return _unavailable("invalid log reference")
    try:
        with _open_log_directory(git_common_dir, run_id, reference.commit) as directory_fd:
            fd = os.open(
                reference._name, os.O_RDONLY | os.O_NONBLOCK | _nofollow(), dir_fd=directory_fd
            )
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    return _unavailable("log is unavailable")
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    raw = handle.read(LOG_READ_LIMIT + 1)
                    after = os.fstat(handle.fileno())
                    if after.st_nlink == 0:
                        return _unavailable("log is unavailable")
            finally:
                if fd != -1:
                    os.close(fd)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return _unavailable("log is unavailable")
    # A descriptor pins the opened inode. Compare its metadata after reading,
    # then check whether the name was replaced before the observation finished.
    changed = (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    try:
        with _open_log_directory(git_common_dir, run_id, reference.commit) as directory_fd:
            check_fd = os.open(
                reference._name, os.O_RDONLY | os.O_NONBLOCK | _nofollow(), dir_fd=directory_fd
            )
            try:
                checked = os.fstat(check_fd)
                changed = changed or checked.st_ino != before.st_ino
            finally:
                os.close(check_fd)
    except OSError:
        changed = True
    byte_truncated = len(raw) > LOG_READ_LIMIT
    text = raw[:LOG_READ_LIMIT].decode("utf-8", errors="replace")
    rendered, render_truncated = _bound_rendered(text)
    return LogContent(True, rendered, byte_truncated, render_truncated, changed, None)


def _discover_leaves(
    git_common_dir: Path, run_id: str, diagnostics: list[VerificationDiagnostic]
) -> dict[tuple[str, int], str]:
    leaves: dict[tuple[str, int], str] = {}
    try:
        with _open_directory_chain(
            git_common_dir, ("loop-supervisor", "verification", run_id)
        ) as run_fd:
            commit_names = _names(run_fd, _MAX_COMMIT_DIRECTORIES)
            for commit in commit_names:
                if not _COMMIT_RE.fullmatch(commit):
                    diagnostics.append(
                        VerificationDiagnostic(_safe_name(commit), "invalid commit directory name")
                    )
                    continue
                try:
                    commit_fd = os.open(commit, os.O_RDONLY | _directory_flags(), dir_fd=run_fd)
                except OSError:
                    diagnostics.append(
                        VerificationDiagnostic(commit, "commit directory is unavailable")
                    )
                    continue
                try:
                    by_ordinal: dict[int, list[str]] = {}
                    for name in _names(commit_fd, _MAX_LOG_LEAVES):
                        match = _LOG_RE.fullmatch(name)
                        if match is None:
                            diagnostics.append(
                                VerificationDiagnostic(_safe_name(name), "invalid log filename")
                            )
                            continue
                        ordinal = int(match["ordinal"])
                        if ordinal <= 0:
                            diagnostics.append(VerificationDiagnostic(name, "invalid log ordinal"))
                            continue
                        by_ordinal.setdefault(ordinal, []).append(name)
                    for ordinal, names in by_ordinal.items():
                        if len(names) != 1:
                            diagnostics.append(
                                VerificationDiagnostic(
                                    ", ".join(sorted(names)), "duplicate ordinal"
                                )
                            )
                            continue
                        name = names[0]
                        try:
                            fd = os.open(
                                name, os.O_RDONLY | os.O_NONBLOCK | _nofollow(), dir_fd=commit_fd
                            )
                            try:
                                regular = stat.S_ISREG(os.fstat(fd).st_mode)
                            finally:
                                os.close(fd)
                        except OSError:
                            regular = False
                        if not regular:
                            diagnostics.append(
                                VerificationDiagnostic(name, "log is not a regular file")
                            )
                            continue
                        leaves[(commit, ordinal)] = name
                finally:
                    os.close(commit_fd)
    except OSError:
        diagnostics.append(VerificationDiagnostic(run_id, "verification directory is unavailable"))
    return leaves


def _output_identity(
    git_common_dir: Path, run_id: str, output_path: str, ordinal: int
) -> tuple[str, str] | None:
    # abspath/normpath are lexical operations; unlike resolve(), neither follows
    # an attacker-controlled output_path.
    expected_root = os.path.normpath(
        os.path.abspath(git_common_dir / "loop-supervisor" / "verification" / run_id)
    )
    candidate = os.path.normpath(os.path.abspath(output_path))
    parts = Path(candidate).parts
    root_parts = Path(expected_root).parts
    if len(parts) != len(root_parts) + 2 or parts[: len(root_parts)] != root_parts:
        return None
    commit, name = parts[-2:]
    expected_name = f"{ordinal:02d}.log"
    if not _COMMIT_RE.fullmatch(commit) or name != expected_name:
        return None
    return commit, name


def _bound_rendered(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    size = 0
    truncated = False
    for index, line in enumerate(lines):
        if index >= LOG_RENDER_LINE_LIMIT:
            truncated = True
            break
        remaining = LOG_RENDER_BYTE_LIMIT - size
        encoded = line.encode("utf-8")
        if len(encoded) > remaining:
            result.append(_utf8_prefix(line, remaining))
            truncated = True
            break
        result.append(line)
        size += len(encoded)
    return "".join(result), truncated


def _utf8_prefix(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


def _unavailable(reason: str) -> LogContent:
    return LogContent(False, "", False, False, False, reason)


def _names(directory_fd: int, limit: int) -> list[str]:
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(names) == limit:
                break
            names.append(entry.name)
    return names


@contextmanager
def _open_log_directory(git_common_dir: Path, run_id: str, commit: str) -> Iterator[int]:
    with _open_directory_chain(
        git_common_dir, ("loop-supervisor", "verification", run_id, commit)
    ) as fd:
        yield fd


@contextmanager
def _open_directory_chain(git_common_dir: Path, names: tuple[str, ...]) -> Iterator[int]:
    fd = os.open(git_common_dir, os.O_RDONLY | _directory_flags())
    try:
        for name in names:
            next_fd = os.open(name, os.O_RDONLY | _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _directory_flags() -> int:
    return _required("O_DIRECTORY") | _nofollow()


def _nofollow() -> int:
    return _required("O_NOFOLLOW")


def _required(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise OSError(f"secure verification reads require os.{name}")
    return value


def _safe_name(name: str) -> str:
    return name if len(name) <= 256 else f"{name[:253]}..."
