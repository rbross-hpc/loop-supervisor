"""`loop-supervisor skill list|show|export` -- list, show, or export the
bundled Agent Skills.

Mirrors the `wake skill`/`ref-checker skill` convention (see
wake/cli/skill.py): each skill ships as package data resolved via
`importlib.resources`, so this works identically whether
loop-supervisor is an editable install or a real wheel install, and
`export` is the supported way a user gets a skill into their own
project's `.opencode/skills/` (or another harness's equivalent
directory) -- there is deliberately no default destination path, since
that differs per harness.

`adopt-loop-supervisor` is the default skill for `show`/`export` when
no name is given, preserving the exact behavior these subcommands had
before a second skill (`use-loop-supervisor`) was added.
"""

from __future__ import annotations

import shutil
import sys
from importlib.resources import as_file, files
from pathlib import Path

_DEFAULT_SKILL_NAME = "adopt-loop-supervisor"


def _skills_root():
    return files("loop_supervisor").joinpath("_skills")


def _skill_names() -> list[str]:
    root = _skills_root()
    with as_file(root) as p:
        return sorted(child.name for child in Path(p).iterdir() if child.is_dir())


def _skill_files(name: str):
    return _skills_root().joinpath(name)


def run_skill(args) -> int:
    action = args.skill_action
    if action == "list":
        return _run_list()
    if action == "show":
        return _run_show(args)
    if action == "export":
        return _run_export(args)
    print(f"error: unknown skill action {action!r}", file=sys.stderr)
    return 1


def _resolve_skill_name(raw: str | None) -> str | None:
    name = raw or _DEFAULT_SKILL_NAME
    if name not in _skill_names():
        print(
            f"error: unknown skill {name!r}; available skills: {', '.join(_skill_names())}",
            file=sys.stderr,
        )
        return None
    return name


def _run_list() -> int:
    for name in _skill_names():
        print(name)
    return 0


def _run_show(args) -> int:
    name = _resolve_skill_name(getattr(args, "name", None))
    if name is None:
        return 1
    skill_md = _skill_files(name).joinpath("SKILL.md")
    with as_file(skill_md) as p:
        print(p.read_text(encoding="utf-8"), end="")
    return 0


def _run_export(args) -> int:
    name = _resolve_skill_name(getattr(args, "name", None))
    if name is None:
        return 1

    dest = Path(args.path).resolve()

    if dest.exists() and any(dest.iterdir()):
        if not args.force:
            print(
                f"error: destination already exists and is non-empty: {dest}\n"
                "use --force to overwrite",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(dest)

    skill_root = _skill_files(name)
    with as_file(skill_root) as src:
        shutil.copytree(src, dest, dirs_exist_ok=True)

    print(f"skill exported to: {dest}")
    return 0
