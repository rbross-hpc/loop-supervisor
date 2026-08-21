"""ADR (architecture decision record) persistence.

The architect agent is read-only and only proposes decision content. The
supervisor is the sole writer of ADR files, so the exact approved text is
what lands in the repository — never text re-typed or reinterpreted by a
model after approval.
"""

from __future__ import annotations

import re
from pathlib import Path

from .contracts import ArchitectADR

_SLUG_RE = re.compile(r"[^a-z0-9]+")


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


class DecisionError(RuntimeError):
    pass


def next_adr_number(decisions_dir: Path) -> int:
    if not decisions_dir.exists():
        return 1
    numbers = []
    for path in decisions_dir.glob("[0-9][0-9][0-9][0-9]-*.md"):
        try:
            numbers.append(int(path.name[:4]))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


def write_adr(decisions_dir: Path, adr: ArchitectADR) -> Path:
    """Write the approved ADR exactly once, never overwriting an existing file."""
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

    return target
