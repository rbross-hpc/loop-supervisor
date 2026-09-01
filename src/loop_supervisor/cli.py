"""Command-line entry point for loop-supervisor."""

from __future__ import annotations

import argparse
import contextlib
import importlib.resources
import importlib.resources.abc
import json
import re
import shutil
import signal
import sys
from collections.abc import Iterator
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, ProjectConfig, load_project_config
from .doctor import validate_report
from .git import GitError
from .input_providers import StdinInputProvider
from .locking import LockError
from .phases import PHASE_OPERATIONAL_FAILURE, TERMINAL_PHASES
from .runtime import RuntimeError_, list_run_ids, load_run, run_new, run_resume
from .skill import run_skill
from .state import RunOptions, StateError
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

# The default source URL for a generated project's `loop-supervisor`
# dependency. This is this project's own origin, which is also the only
# fork this tool currently knows how to point a new project at; see ADR
# 0018. Override with `init --loop-supervisor-git-url`.
_DEFAULT_LOOP_SUPERVISOR_GIT_URL = "https://github.com/rbross-hpc/loop-tui-experiment.git"

_PROJECT_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


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


def _resolve_project_config(args: argparse.Namespace, project_root: Path) -> ProjectConfig:
    """Load `loop-supervisor.toml` (or the `--config` override) and apply
    CLI overrides on top of it.

    A `--provision-command`/`--verify-command` flag replaces the config
    file's corresponding command list entirely (it does not append to
    it); `--no-provision`/`--no-verify` force the corresponding list to
    empty regardless of what the config file says. Precedence is
    flag > config file > off, matching every other run-behavior setting
    in this project.
    """
    config_path = Path(args.config) if args.config else project_root / "loop-supervisor.toml"
    config = load_project_config(config_path)

    provision_commands = config.provision_commands
    if getattr(args, "no_provision", False):
        provision_commands = ()
    elif getattr(args, "provision_command", None):
        provision_commands = tuple(args.provision_command)

    verify_commands = config.verify_commands
    if getattr(args, "no_verify", False):
        verify_commands = ()
    elif getattr(args, "verify_command", None):
        verify_commands = tuple(args.verify_command)

    return ProjectConfig(
        provision_commands=provision_commands,
        provision_timeout=getattr(args, "provision_timeout", None) or config.provision_timeout,
        verify_commands=verify_commands,
        verify_timeout=getattr(args, "verify_timeout", None) or config.verify_timeout,
    )


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

    try:
        project_config = _resolve_project_config(args, project_root)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Route through RunOptions.from_dict() rather than constructing
    # RunOptions directly: from_dict() is the same validation resume()
    # relies on to load a persisted run, so this is the one place that
    # defines what a valid RunOptions is. Without this, argparse's
    # type=int/type=float coercion accepts values (e.g. a negative
    # --max-tasks, a zero or negative --role-timeout) that from_dict()
    # would reject -- so a run could start, persist that invalid state,
    # and then be permanently unresumable, since the very file its own
    # program wrote fails the check its own resume path performs.
    try:
        options = RunOptions.from_dict(
            {
                "max_accepted_tasks": args.max_tasks,
                "max_revisions_per_task": args.max_revisions,
                "max_replans_per_task": args.max_replans,
                "max_architect_retries": args.max_architect_retries,
                "max_builder_guidance_attempts": args.max_builder_guidance_attempts,
                "malformed_output_retries": 1,
                "role_timeout": args.role_timeout,
                "worktree_root": str(Path(args.worktree_root).resolve())
                if args.worktree_root
                else None,
                "require_decision_approval": args.require_decision_approval,
                "opencode_executable": args.opencode_executable,
                "opencode_startup_timeout": args.startup_timeout,
                "provision_commands": list(project_config.provision_commands),
                "provision_timeout": project_config.provision_timeout,
                "verify_commands": list(project_config.verify_commands),
                "verify_timeout": project_config.verify_timeout,
            }
        )
    except StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

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

    # Reject a terminal run before acquiring the lock or starting
    # OpenCode: resuming a finished/failed run is already guaranteed to
    # no-op (Supervisor.run()'s `while state.phase not in
    # _TERMINAL_PHASES` loop guard exits immediately), but without this
    # check the no-op is discovered only after a full server spawn and
    # teardown, and reported identically to a resume that did real work
    # ("final phase: done", exit 0). load_run() is lock-free, so this
    # adds no acquisition of its own before the real one below. This
    # does not relax ADR 0006's "no further resume is possible" -- a
    # terminal run still cannot be reopened; this only makes the
    # already-correct no-op cheap and legible instead of silent.
    try:
        existing = load_run(project_root, args.run_id)
    except RuntimeError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if existing.phase in TERMINAL_PHASES:
        print(
            f"error: run {args.run_id} is already {existing.phase} "
            f"({existing.accepted_task_count} tasks accepted); "
            "start a new run with 'loop-supervisor run'",
            file=sys.stderr,
        )
        return 1

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


def _skeleton_root() -> importlib.resources.abc.Traversable:
    """The packaged project skeleton bundled inside the installed
    `loop_supervisor` distribution (source tree in `_skeleton/`, shipped
    as package data; see `pyproject.toml`'s `[tool.setuptools.package-
    data]`). Resolves correctly whether `loop_supervisor` is an editable
    install (points into this checkout's own `src/`) or a real wheel
    install (points into `site-packages/`) -- unlike the old Git-
    checkout-only template mechanism it replaces, this needs no `.git`
    to be present at all. See ADR 0018."""
    return importlib.resources.files("loop_supervisor").joinpath("_skeleton")


def _iter_skeleton_files(
    root: importlib.resources.abc.Traversable,
) -> Iterator[tuple[str, importlib.resources.abc.Traversable]]:
    """Yield (relative_posix_path, file) for every file under `root`,
    recursively, including dotfiles and dotdirs (e.g. `.gitignore`,
    `.opencode/agents/`) -- `Traversable.iterdir()` does not skip these
    the way a shell glob would."""
    stack: list[tuple[str, importlib.resources.abc.Traversable]] = [("", root)]
    while stack:
        prefix, entry = stack.pop()
        for child in entry.iterdir():
            relative = f"{prefix}{child.name}" if not prefix else f"{prefix}/{child.name}"
            if child.is_dir():
                stack.append((relative, child))
            else:
                yield relative, child


# Files copied verbatim; every other name in this map has its
# `__LOOP_SUPERVISOR_..._` placeholders substituted as it is copied, and
# has its `.tmpl` suffix stripped from the destination name.
_SKELETON_PLACEHOLDER_FILES = {
    "README.md.tmpl": "README.md",
    "opencode.json.tmpl": "opencode.json",
    "pyproject.toml.tmpl": "pyproject.toml",
    ".opencode/agents/loop-architect.md.tmpl": ".opencode/agents/loop-architect.md",
}


def cmd_init_copy(args: argparse.Namespace) -> int:
    destination = Path(args.destination).resolve()
    project_name = args.project_name or destination.name
    if not _PROJECT_NAME_PATTERN.fullmatch(project_name):
        print(
            f"error: {project_name!r} is not a valid project name "
            "(use --project-name to set one explicitly)",
            file=sys.stderr,
        )
        return 1

    if destination.exists() and any(destination.iterdir()):
        print(f"error: destination {destination} already exists and is not empty", file=sys.stderr)
        return 1

    # The `external_directory` permission is scoped to sibling task
    # worktrees, which the supervisor creates one directory level above
    # the project root by default (README: "Sibling task worktrees") --
    # so the path that must be allowed is the destination's *parent*,
    # not the destination itself.
    allowed_external_directory = str(destination.parent)

    # loop-architect.md.tmpl's frontmatter is `<placeholder>temperature:
    # ...`, so this substitutes either a `model: ...\n` line (with its own
    # trailing newline) or an empty string, never leaving a blank line or
    # broken YAML behind either way.
    architect_model = getattr(args, "architect_model", None)
    architect_model_line = f"model: {architect_model}\n" if architect_model else ""

    substitutions = {
        "__LOOP_SUPERVISOR_PROJECT_NAME__": project_name,
        "__LOOP_SUPERVISOR_PROJECT_ROOT__": allowed_external_directory,
        "__LOOP_SUPERVISOR_GIT_URL__": args.loop_supervisor_git_url,
        "__LOOP_SUPERVISOR_ARCHITECT_MODEL_LINE__": architect_model_line,
    }

    created_destination = not destination.exists()
    destination.mkdir(parents=True, exist_ok=True)

    try:
        root = _skeleton_root()
        for relative_name, entry in _iter_skeleton_files(root):
            dst_name = _SKELETON_PLACEHOLDER_FILES.get(relative_name)
            if dst_name is not None:
                text = entry.read_text(encoding="utf-8")
                for placeholder, value in substitutions.items():
                    text = text.replace(placeholder, value)
                dst_path = destination / dst_name
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                dst_path.write_text(text, encoding="utf-8")
            else:
                dst_path = destination / relative_name
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                dst_path.write_bytes(entry.read_bytes())
    except OSError as exc:
        if created_destination:
            shutil.rmtree(destination, ignore_errors=True)
        print(f"error: failed to write project skeleton: {exc}", file=sys.stderr)
        return 1

    print(f"project skeleton written to {destination}")
    print("next steps:")
    print(f"  cd {destination}")
    print("  cp .env.example .env   # then fill in credentials")
    print("  git init && git add -A && git commit -m 'Initial commit'")
    print("  edit docs/OBJECTIVE.md and README.md to describe your actual project")
    return 0


def cmd_config_validate(args: argparse.Namespace) -> int:
    """Fast, offline preflight: is this project plausibly set up for a
    loop-supervisor run. See `doctor.py` for what's checked and why
    reachability of the configured model provider is deliberately out of
    scope. Exits 1 (not an exception) when any check fails, mirroring
    `cmd_run`/`cmd_resume`'s "error: ..." convention rather than raising.
    """
    project_root = _project_root(args.project)
    report = validate_report(project_root, opencode_executable=args.opencode_executable)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for name, result in report["checks"].items():
            status = "OK  " if result["ok"] else "FAIL"
            print(f"[{status}] {name}: {result['detail']}")
        if report["ok"]:
            print("all checks passed")
        else:
            print("one or more checks failed; see above", file=sys.stderr)

    return 0 if report["ok"] else 1


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
    run_parser.add_argument("--max-builder-guidance-attempts", type=int, default=3)
    run_parser.add_argument("--role-timeout", type=float, default=1800.0)
    run_parser.add_argument(
        "--recover-stale-lock",
        action="store_true",
        help="Remove a stale lock from a dead local process and retry",
    )
    run_parser.add_argument(
        "--config",
        default=None,
        help="Path to loop-supervisor.toml (default: <project>/loop-supervisor.toml)",
    )
    provision_group = run_parser.add_mutually_exclusive_group()
    provision_group.add_argument(
        "--provision-command",
        action="append",
        default=None,
        metavar="CMD",
        help="Command to run in a new task worktree before building "
        "(repeatable; replaces [provision].commands from the config file "
        "entirely, does not append to it)",
    )
    provision_group.add_argument(
        "--no-provision",
        action="store_true",
        help="Disable worktree provisioning even if the config file configures it",
    )
    run_parser.add_argument("--provision-timeout", type=float, default=None)
    verify_group = run_parser.add_mutually_exclusive_group()
    verify_group.add_argument(
        "--verify-command",
        action="append",
        default=None,
        metavar="CMD",
        help="Command to run after building and before auditing, with results "
        "shown to the auditor (repeatable; replaces [verify].commands from "
        "the config file entirely, does not append to it)",
    )
    verify_group.add_argument(
        "--no-verify",
        action="store_true",
        help="Disable supervisor-run verification even if the config file configures it",
    )
    run_parser.add_argument("--verify-timeout", type=float, default=None)
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

    init_parser = sub.add_parser(
        "init", help="Write a new project skeleton that depends on loop-supervisor"
    )
    init_parser.add_argument(
        "--destination", required=True, help="Directory to write the new project skeleton into"
    )
    init_parser.add_argument(
        "--project-name",
        default=None,
        help="Name for the new project (default: the destination directory's name)",
    )
    init_parser.add_argument(
        "--loop-supervisor-git-url",
        default=_DEFAULT_LOOP_SUPERVISOR_GIT_URL,
        help="Git URL the generated pyproject.toml pins loop-supervisor to",
    )
    init_parser.add_argument(
        "--architect-model",
        default=None,
        help=(
            "provider/model-id to pin the architect agent to (e.g. "
            "'anthropic/claude-opus-4'); default: no pin, inherit the "
            "project's own default model"
        ),
    )
    init_parser.set_defaults(func=cmd_init_copy)

    config_parser = sub.add_parser("config", help="Inspect or validate project configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_sub.add_parser(
        "validate", help="Check whether this project is set up for a loop-supervisor run"
    )
    validate_parser.add_argument("--project", default=None, help="Path to the integration repo")
    validate_parser.add_argument("--opencode-executable", default="opencode")
    validate_parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON report"
    )
    validate_parser.set_defaults(func=cmd_config_validate)

    skill_parser = sub.add_parser(
        "skill", help="Show or export the bundled adopt-loop-supervisor Agent Skill"
    )
    skill_sub = skill_parser.add_subparsers(dest="skill_action", required=True)
    skill_sub.add_parser("show", help="Print the bundled SKILL.md to stdout")
    skill_export_parser = skill_sub.add_parser("export", help="Copy the skill directory to PATH")
    skill_export_parser.add_argument("path", metavar="PATH", help="Destination directory")
    skill_export_parser.add_argument(
        "--force", action="store_true", help="Overwrite the destination if non-empty"
    )
    skill_parser.set_defaults(func=lambda args: run_skill(args))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
