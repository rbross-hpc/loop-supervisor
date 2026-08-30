"""`loop-supervisor skill show|export` -- show or export the bundled
`adopt-loop-supervisor` Agent Skill.

Mirrors the `wake skill`/`ref-checker skill` convention (see
wake/cli/skill.py): the skill ships as package data resolved via
`importlib.resources`, so this works identically whether
loop-supervisor is an editable install or a real wheel install, and
`export` is the supported way a user gets the skill into their own
project's `.opencode/skills/` (or another harness's equivalent
directory) -- there is deliberately no default destination path, since
that differs per harness.
"""

from __future__ import annotations

import shutil
import sys
from importlib.resources import as_file, files
from pathlib import Path

_SKILL_DIR_NAME = "adopt-loop-supervisor"


def _skill_files():
    return files("loop_supervisor").joinpath(f"_skills/{_SKILL_DIR_NAME}")


def run_skill(args) -> int:
    action = args.skill_action
    if action == "show":
        return _run_show()
    if action == "export":
        return _run_export(args)
    print(f"error: unknown skill action {action!r}", file=sys.stderr)
    return 1


def _run_show() -> int:
    skill_md = _skill_files().joinpath("SKILL.md")
    with as_file(skill_md) as p:
        print(p.read_text(encoding="utf-8"), end="")
    return 0


def _run_export(args) -> int:
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

    skill_root = _skill_files()
    with as_file(skill_root) as src:
        shutil.copytree(src, dest, dirs_exist_ok=True)

    print(f"skill exported to: {dest}")
    return 0
