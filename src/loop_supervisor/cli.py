"""Command-line entry point for loop-supervisor."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from .git import GitError, GitRepo
from .locking import LockError
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


class StdinInputProvider:
    """Interactive input provider that reads from stdin when attached to a TTY."""

    def request(self, *, kind: str, message: str, context: dict) -> str | None:
        if not sys.stdin.isatty():
            return None
        print(f"\n[{kind}] {message}")
        try:
            return input("> ")
        except EOFError:
            return None


def _project_root(path: str | None) -> Path:
    return Path(path).resolve() if path else Path.cwd()


def cmd_run(args: argparse.Namespace) -> int:
    project_root = _project_root(args.project)
    load_dotenv(project_root / ".env")

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
        final = run_new(
            project_root,
            options,
            recover_stale_lock=getattr(args, "recover_stale_lock", False),
        )
    except _EXPECTED_CLI_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"run_id: {final.run_id}")
    print(f"final phase: {final.phase}")
    return 0 if final.phase == "done" else 1


def cmd_resume(args: argparse.Namespace) -> int:
    project_root = _project_root(args.project)
    load_dotenv(project_root / ".env")

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
        final = run_resume(
            project_root,
            args.run_id,
            recover_stale_lock=getattr(args, "recover_stale_lock", False),
        )
    except _EXPECTED_CLI_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"final phase: {final.phase}")
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
    run_parser.set_defaults(func=cmd_run)

    # `resume` deliberately does not accept any run-behavior flags
    # (limits, worktree root, approval policy, executable, timeouts):
    # those are immutable and persisted in RunState.options at
    # `start_new_run()`, and resume reconstructs the supervisor's
    # behavior entirely from that persisted state, never from CLI
    # arguments supplied at resume time.
    resume_parser = sub.add_parser("resume", help="Resume a paused run (omit run_id to list)")
    resume_parser.add_argument("--project", default=None, help="Path to the integration repo")
    resume_parser.add_argument("run_id", nargs="?", default=None)
    resume_parser.add_argument(
        "--recover-stale-lock",
        action="store_true",
        help="Remove a stale lock from a dead local process and retry",
    )
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
