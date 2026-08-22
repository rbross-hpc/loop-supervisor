"""ADR (architecture decision record) persistence.

The architect agent is read-only and only proposes decision content. The
supervisor is the sole writer of ADR files, so the exact approved text is
what lands in the repository — never text re-typed or reinterpreted by a
model after approval.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .contracts import ArchitectADR

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ADR_FILENAME_RE = re.compile(r"^[0-9]{4}-[a-z0-9-]+\.md$")


def slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")
    return slug or "decision"


def render_adr(adr: ArchitectADR) -> str:
    consequences = "\n".join(f"- {item}" for item in adr.consequences) or "- (none noted)"
    return (
        f"# {adr.title}\n"
        "\n"
        "## Status\n"
        "\n"
        "Accepted\n"
        "\n"
        "## Context\n"
        "\n"
        f"{adr.context}\n"
        "\n"
        "## Decision\n"
        "\n"
        f"{adr.decision}\n"
        "\n"
        "## Consequences\n"
        "\n"
        f"{consequences}\n"
    )


def adr_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class DecisionError(RuntimeError):
    pass


def validate_decisions_subpath(decisions_subpath: Path) -> None:
    """Validate the configured decisions subpath before it is ever joined
    with an active worktree root.

    Must be relative and must not escape the worktree via `..` components.
    """
    if decisions_subpath.is_absolute():
        raise DecisionError(f"decisions subpath {decisions_subpath} must be relative")
    if ".." in decisions_subpath.parts:
        raise DecisionError(f"decisions subpath {decisions_subpath} must not contain '..'")


def validate_adr_target(
    *,
    worktree_root: Path,
    decisions_dir: Path,
    target_path: Path,
) -> Path:
    """Validate a persisted ADR target path before any read or write.

    Enforces:
    - `target_path` resolves to a direct child of the exact `decisions_dir`
      (no nested subdirectories, no traversal, no prefix-confusion siblings
      such as `docs/decisions-evil`).
    - the resolved `decisions_dir` itself is contained within
      `worktree_root` (rejecting a symlinked decisions directory that
      escapes the worktree).
    - the filename matches the generated `NNNN-slug.md` form.
    - the target is not an existing symlink (never follow a symlink target
      for either reads or writes).

    Returns the resolved, validated target path.
    """
    try:
        resolved_root = worktree_root.resolve()
        resolved_decisions_dir = decisions_dir.resolve()
        try:
            resolved_decisions_dir.relative_to(resolved_root)
        except ValueError as exc:
            raise DecisionError(
                f"decisions directory {decisions_dir} resolves to {resolved_decisions_dir}, "
                f"which escapes the worktree root {resolved_root}"
            ) from exc

        target = Path(target_path)
        if target.parent.resolve() != resolved_decisions_dir:
            raise DecisionError(
                f"ADR target {target_path} is not a direct child of decisions directory "
                f"{decisions_dir}; refusing to write outside the exact decisions directory"
            )
        if not _ADR_FILENAME_RE.match(target.name):
            raise DecisionError(
                f"ADR target filename {target.name!r} does not match the required "
                "'NNNN-slug.md' form"
            )
        if target.is_symlink():
            raise DecisionError(
                f"ADR target {target_path} is an existing symlink; refusing to use it"
            )

        resolved_target = resolved_decisions_dir / target.name
    except OSError as exc:
        raise DecisionError(f"filesystem error validating ADR target: {exc}") from exc
    return resolved_target


def next_adr_number(decisions_dir: Path) -> int:
    try:
        if not decisions_dir.exists():
            return 1
        numbers = []
        for path in decisions_dir.glob("[0-9][0-9][0-9][0-9]-*.md"):
            try:
                numbers.append(int(path.name[:4]))
            except ValueError:
                continue
        return max(numbers, default=0) + 1
    except OSError as exc:
        raise DecisionError(f"filesystem error enumerating ADR directory: {exc}") from exc


def write_adr(decisions_dir: Path, adr: ArchitectADR) -> Path:
    """Write the approved ADR exactly once, never overwriting an existing file."""
    try:
        decisions_dir.mkdir(parents=True, exist_ok=True)
        number = next_adr_number(decisions_dir)
        filename = f"{number:04d}-{slugify(adr.title)}.md"
        target = decisions_dir / filename

        if target.exists():
            raise DecisionError(f"ADR path {target} already exists")

        try:
            with target.open("x") as handle:
                handle.write(render_adr(adr))
        except FileExistsError as exc:
            raise DecisionError(f"ADR path {target} already exists") from exc
    except OSError as exc:
        raise DecisionError(f"filesystem error writing ADR: {exc}") from exc

    return target


def write_adr_idempotent(
    decisions_dir: Path,
    adr: ArchitectADR,
    *,
    worktree_root: Path,
    target_path: str | None = None,
    expected_hash: str | None = None,
) -> Path:
    """Write the approved ADR with idempotent reconciliation.

    If `target_path` and `expected_hash` are provided (durable intent was
    already persisted), this operation reconciles against that intent:

    - The target is validated to be exactly a direct child of
      `decisions_dir`, itself contained in `worktree_root`, matching the
      generated filename form, and not an existing symlink.
    - `expected_hash` must match the hash of a freshly rendered ADR from
      the (already-approved) `adr` content; the persisted hash is
      authoritative, so a caller-supplied `adr` that renders to different
      bytes than what was recorded is rejected rather than silently
      accepted or silently rewritten.
    - Missing target: create it.
    - Existing target whose exact bytes hash to `expected_hash`: treat as
      already completed and return the path without re-writing.
    - Existing target with different content: fail closed (DecisionError).

    If `target_path` is None, falls back to allocating a new number and
    writing exactly once (non-idempotent; used when recording a fresh decision
    before intent has been persisted).

    Any `OSError` encountered while touching the filesystem (permission
    errors, disk-full, transient I/O failures, etc.) is wrapped as a
    `DecisionError` rather than escaping unclassified: callers rely on
    `DecisionError` being one of the exception types `Supervisor.advance()`
    converts into a durable, retryable operational failure."""
    try:
        decisions_dir.mkdir(parents=True, exist_ok=True)
        content = render_adr(adr)

        if target_path is not None:
            if expected_hash is None:
                raise DecisionError("target_path was provided without expected_hash")
            rendered_hash = adr_content_hash(content)
            if rendered_hash != expected_hash:
                raise DecisionError(
                    f"rendered ADR content hash {rendered_hash!r} does not match the "
                    f"persisted expected hash {expected_hash!r}; refusing to write "
                    "content that diverges from the recorded intent"
                )

            target = validate_adr_target(
                worktree_root=worktree_root,
                decisions_dir=decisions_dir,
                target_path=Path(target_path),
            )
            if target.exists():
                actual_content = target.read_text()
                actual_hash = adr_content_hash(actual_content)
                if actual_hash == expected_hash:
                    return target
                raise DecisionError(
                    f"ADR path {target} already exists with different content "
                    f"(expected hash {expected_hash!r}, got {actual_hash!r})"
                )
            try:
                with target.open("x") as handle:
                    handle.write(content)
            except FileExistsError as exc:
                raise DecisionError(f"ADR path {target} already exists") from exc
            return target

        number = next_adr_number(decisions_dir)
        filename = f"{number:04d}-{slugify(adr.title)}.md"
        target = decisions_dir / filename

        if target.exists():
            raise DecisionError(f"ADR path {target} already exists")

        try:
            with target.open("x") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise DecisionError(f"ADR path {target} already exists") from exc
    except OSError as exc:
        raise DecisionError(f"filesystem error writing ADR: {exc}") from exc

    return target
