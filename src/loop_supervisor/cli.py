"""Command-line entry point for loop-supervisor."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from dotenv import load_dotenv

from .git import GitError, GitRepo
from .input_providers import StdinInputProvider
from .locking import LockError
from .phases import PHASE_OPERATIONAL_FAILURE, TERMINAL_PHASES
from .runtime import RuntimeError_, list_run_ids, run_new, run_resume
from .state import RunOptions
from .supervisor import FailurePersistenceError, LoopError

# Expected application-level failures that a normal `run`/`resume`
# invocation should report as a single sanitized error line and exit 1,
# rather than letting a traceback escape to the user. KeyboardInterrupt
# and SystemExit are deliberately never included here.
_EXPECTED_CLI_ERRORS: tuple[type[Exception], ...] = (
    RuntimeError_,
    LockError,
    GitError,
    LoopError,
    FailurePersistenceError,
)

_TEMPLATE_MARKERS = ("pyproject.toml", "src/loop_supervisor", ".opencode/agents")


@contextlib.contextmanager
def _bridge_sigterm_to_keyboard_interrupt() -> Iterator[None]:
    """Make SIGTERM drive the same cleanup path SIGINT already does.

    Every cleanup obligation in this codebase -- stopping the OpenCode
    process group, stopping the permission denier, releasing the
    supervisor lock -- is reached exclusively through Python exception
    unwinding (`RunSession.__exit__` / `OpenCodeServer.__exit__`; see ADR
    0015). SIGINT's default disposition already raises
    `KeyboardInterrupt` in the main thread, so a plain Ctrl-C gets that
    cleanup for free. SIGTERM's default disposition is immediate process
    termination with no Python-level unwinding at all, so absent this
    handler a bare `kill <pid>` strands the lock file and orphans the
    OpenCode process group.

    The handler is deliberately one-shot: on first delivery it restores
    the platform default disposition (`SIG_DFL`) before raising, so a
    *second* SIGTERM -- e.g. a process supervisor escalating after a
    grace period -- kills immediately rather than raising a second
    `KeyboardInterrupt` into whatever cleanup retry loop the first one
    triggered. This mirrors the existing double-Ctrl-C behavior
    documented in runtime.py (a second interrupt during cleanup aborts
    the retry loop rather than being ignored).

    Scoped to `cmd_run`/`cmd_resume` (the headless entry points) only --
    never installed in library code such as `RunSession` or
    `OpenCodeServer`, so importing loop_supervisor does not silently
    change a host application's signal disposition, and never wrapped
    around `cmd_tui`: Textual's Linux driver already strips the
    terminal's ISIG flag in raw mode (Ctrl-C is read as an ordinary
    keypress, not delivered as SIGINT) and injecting an externally
    raised KeyboardInterrupt into Textual's running asyncio event loop is
    untested and could leave the terminal stuck in raw mode. TUI-side
    signal handling needs its own UX decision and is deliberately out of
    scope here (see ADR 0015).
    """
    previous = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum: int, frame: object) -> None:
        signal.signal(signal.SIGTERM, previous)
        print(
            "loop-supervisor: received SIGTERM, shutting down "
            "(a second SIGTERM will terminate immediately)",
            file=sys.stderr,
        )
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _project_root(path: str | None) -> Path:
    return Path(path).resolve() if path else Path.cwd()


def _add_step_control_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the mutually exclusive --step/--max-steps session controls.

    These bound how many completed advance() calls a single invocation
    performs before stopping (even on a non-terminal phase); they are a
    per-invocation session control, not a run-behavior flag, so they are
    never persisted into RunOptions and are safe to accept on `resume`
    (see docs/decisions/0006-resumable-run-options-and-git-checkpoints.md).
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--step",
        action="store_true",
        help="Perform exactly one phase transition, then stop (shorthand for --max-steps 1)",
    )
    group.add_argument(
        "--max-steps",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N completed phase transitions, even if the run has not finished",
    )


def _resolve_max_steps(args: argparse.Namespace) -> int | None:
    if getattr(args, "step", False):
        return 1
    max_steps: int | None = getattr(args, "max_steps", None)
    if max_steps is not None and max_steps < 1:
        raise ValueError("--max-steps must be at least 1")
    return max_steps


# Phases that are terminal-in-effect for reporting purposes: a run stopped
# in one of these is not "paused" awaiting a resume, it is finished (done),
# genuinely failed (failed), or already reported its own failure line
# (operational_failure surfaces via the LoopError path when it carries an
# error; when it doesn't, the phase alone is not something to resume from
# in the ordinary sense, but it is also not a pause -- it is an unretryable
# stop). Neither category should be announced as "paused".
_NON_PAUSE_PHASES = TERMINAL_PHASES | {PHASE_OPERATIONAL_FAILURE}


def _paused_phase_message(phase: str) -> str | None:
    """Return the "paused at phase X" line for a non-terminal, non-failure
    stop, or None if the run finished, failed, or hit an unretryable
    operational failure.

    This does not depend on whether --max-steps/--step was supplied: a run
    that stops at, e.g., awaiting_input because input was unavailable is
    just as much a pause as one that stops because its step budget ran
    out, and both should be reported the same way.
    """
    if phase in _NON_PAUSE_PHASES:
        return None
    return f"paused at phase {phase}"


def cmd_run(args: argparse.Namespace) -> int:
    project_root = _project_root(args.project)
    load_dotenv(project_root / ".env")

    try:
        max_steps = _resolve_max_steps(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    options = RunOptions(
        max_accepted_tasks=args.max_tasks,
        max_revisions_per_task=args.max_revisions,
        max_replans_per_task=args.max_replans,
        max_architect_retries=args.max_architect_retries,
        malformed_output_retries=1,
        role_timeout=args.role_timeout,
        worktree_root=str(Path(args.worktree_root).resolve()) if args.worktree_root else None,
        require_decision_approval=args.require_decision_approval,
        opencode_executable=args.opencode_executable,
        opencode_startup_timeout=args.startup_timeout,
    )

    try:
        with _bridge_sigterm_to_keyboard_interrupt():
            final = run_new(
                project_root,
                options,
                input_provider=StdinInputProvider(),
                recover_stale_lock=getattr(args, "recover_stale_lock", False),
                max_steps=max_steps,
            )
    except _EXPECTED_CLI_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"run_id: {final.run_id}")
    print(f"final phase: {final.phase}")
    paused = _paused_phase_message(final.phase)
    if paused is not None:
        print(paused)
    return 0 if final.phase == "done" else 1


def cmd_resume(args: argparse.Namespace) -> int:
    project_root = _project_root(args.project)
    load_dotenv(project_root / ".env")

    try:
        max_steps = _resolve_max_steps(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.run_id is None:
        try:
            runs = list_run_ids(project_root)
        except RuntimeError_ as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not runs:
            print("no saved runs found", file=sys.stderr)
            return 1
        print("available runs:")
        for run_id in runs:
            print(f"  {run_id}")
        return 0

    try:
        with _bridge_sigterm_to_keyboard_interrupt():
            final = run_resume(
                project_root,
                args.run_id,
                input_provider=StdinInputProvider(),
                recover_stale_lock=getattr(args, "recover_stale_lock", False),
                max_steps=max_steps,
            )
    except _EXPECTED_CLI_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"final phase: {final.phase}")
    paused = _paused_phase_message(final.phase)
    if paused is not None:
        print(paused)
    return 0 if final.phase == "done" else 1


def _looks_like_template(source: Path) -> bool:
    return all((source / marker).exists() for marker in _TEMPLATE_MARKERS)


def _template_source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _tracked_files(source: Path) -> list[str]:
    """List Git-tracked files in `source`, the only files copy-mode
    bootstrap is allowed to touch. This is a positive allowlist, not a
    denylist: untracked files (secrets, local caches, ignored artifacts)
    are never copied, regardless of name."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(source),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(
            f"could not list tracked files in {source} "
            f"(is it a git checkout with a clean index?): {result.stderr.strip()}"
        )
    return [name for name in result.stdout.split("\0") if name]


def cmd_init_copy(args: argparse.Namespace, *, source: Path | None = None) -> int:
    if source is None:
        source = _template_source_root()
    destination = Path(args.destination).resolve()

    if not _looks_like_template(source):
        print(f"error: {source} does not look like the loop-supervisor template", file=sys.stderr)
        return 1

    try:
        tracked = _tracked_files(source)
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not tracked:
        print(f"error: {source} has no git-tracked files to copy", file=sys.stderr)
        return 1

    if destination.exists() and any(destination.iterdir()):
        print(f"error: destination {destination} already exists and is not empty", file=sys.stderr)
        return 1

    created_destination = not destination.exists()
    destination.mkdir(parents=True, exist_ok=True)

    try:
        for relative_name in tracked:
            src_path = source / relative_name
            dst_path = destination / relative_name
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
    except OSError as exc:
        if created_destination:
            shutil.rmtree(destination, ignore_errors=True)
        print(f"error: failed to copy template files: {exc}", file=sys.stderr)
        return 1

    print(f"template copied to {destination}")
    print("next steps:")
    print(f"  cd {destination}")
    print("  cp .env.example .env   # then fill in credentials")
    print("  git init && git add -A && git commit -m 'Initial commit'")
    return 0


def cmd_init_in_place(args: argparse.Namespace) -> int:
    project_root = _project_root(args.project)

    if not _looks_like_template(project_root):
        print(
            f"error: {project_root} does not look like the loop-supervisor template root",
            file=sys.stderr,
        )
        return 1

    git_dir = project_root / ".git"
    if not git_dir.exists():
        print("error: no .git directory found; nothing to remove", file=sys.stderr)
        return 1

    try:
        repo = GitRepo(project_root)
        dirty = not repo.is_clean()
    except GitError:
        dirty = True

    if dirty and not args.force:
        print(
            "error: repository has uncommitted changes; commit or discard them, "
            "or pass --yes to proceed anyway",
            file=sys.stderr,
        )
        return 1

    print(f"This will permanently remove {git_dir} (all Git history and remotes).")
    print("Tracked files, .env, and other local content will be left in place.")
    if not args.yes:
        answer = input("Type 'delete-git-history' to confirm: ")
        if answer.strip() != "delete-git-history":
            print("aborted")
            return 1

    if git_dir.is_dir():
        shutil.rmtree(git_dir)
    else:
        git_dir.unlink()

    print(f"removed {git_dir}; this is now a plain directory with no Git repository")
    print("next: git init && git add -A && git commit -m 'Initial commit'")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    project_root = _project_root(args.project)
    load_dotenv(project_root / ".env")

    from .tui.app import LoopSupervisorApp

    try:
        app = LoopSupervisorApp(
            project_root,
            recover_stale_lock=getattr(args, "recover_stale_lock", False),
        )
    except _EXPECTED_CLI_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    app.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loop-supervisor")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Start a new loop run")
    run_parser.add_argument("--project", default=None, help="Path to the integration repo")
    run_parser.add_argument("--opencode-executable", default="opencode")
    run_parser.add_argument("--startup-timeout", type=float, default=30.0)
    run_parser.add_argument(
        "--require-decision-approval",
        action="store_true",
        help="Pause for interactive approval of DECIDED architect proposals (default: auto-accept)",
    )
    run_parser.add_argument("--worktree-root", default=None)
    run_parser.add_argument("--max-tasks", type=int, default=20)
    run_parser.add_argument("--max-revisions", type=int, default=5)
    run_parser.add_argument("--max-replans", type=int, default=3)
    run_parser.add_argument("--max-architect-retries", type=int, default=3)
    run_parser.add_argument("--role-timeout", type=float, default=1800.0)
    run_parser.add_argument(
        "--recover-stale-lock",
        action="store_true",
        help="Remove a stale lock from a dead local process and retry",
    )
    _add_step_control_arguments(run_parser)
    run_parser.set_defaults(func=cmd_run)

    # `resume` deliberately does not accept run-behavior flags (limits,
    # worktree root, approval policy, executable, timeouts): those are
    # immutable and persisted in RunState.options at `start_new_run()`,
    # and resume reconstructs the supervisor's behavior entirely from
    # that persisted state, never from CLI arguments supplied at resume
    # time. --step/--max-steps are a per-invocation session control, not
    # a run-behavior flag, and are exempt from that rule (see ADR 0006).
    resume_parser = sub.add_parser("resume", help="Resume a paused run (omit run_id to list)")
    resume_parser.add_argument("--project", default=None, help="Path to the integration repo")
    resume_parser.add_argument("run_id", nargs="?", default=None)
    resume_parser.add_argument(
        "--recover-stale-lock",
        action="store_true",
        help="Remove a stale lock from a dead local process and retry",
    )
    _add_step_control_arguments(resume_parser)
    resume_parser.set_defaults(func=cmd_resume)

    tui_parser = sub.add_parser("tui", help="Open the Textual TUI")
    tui_parser.add_argument("--project", default=None, help="Path to the integration repo")
    tui_parser.add_argument(
        "--recover-stale-lock",
        action="store_true",
        help="Remove a stale lock from a dead local process and retry",
    )
    tui_parser.set_defaults(func=cmd_tui)

    init_parser = sub.add_parser("init", help="Bootstrap a new project from this template")
    init_mode = init_parser.add_mutually_exclusive_group(required=True)
    init_mode.add_argument(
        "--destination", default=None, help="Copy the template to a new directory"
    )
    init_mode.add_argument(
        "--in-place", action="store_true", help="Remove Git history from the current checkout"
    )
    init_parser.add_argument("--project", default=None, help="Template root (for --in-place)")
    init_parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    init_parser.add_argument(
        "--force", action="store_true", help="Proceed with --in-place even if the tree is dirty"
    )

    def _dispatch_init(args: argparse.Namespace) -> int:
        if args.destination is not None:
            return cmd_init_copy(args)
        return cmd_init_in_place(args)

    init_parser.set_defaults(func=_dispatch_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
