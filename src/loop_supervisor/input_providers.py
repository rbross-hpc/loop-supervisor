"""Concrete `InputProvider` implementations.

Kept separate from both `cli.py` (a presentation module) and `runtime.py`
(the shared application-level controller) so that `runtime.py` never needs
to import from `cli.py` to obtain a default provider. See
`supervisor.InputProvider` for the protocol these implement.
"""

from __future__ import annotations

import sys


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
