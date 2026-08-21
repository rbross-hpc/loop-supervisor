# Supervisor owns orchestration; agents have narrow, role-specific permissions

## Status

Accepted

## Context

An OpenCode agent loop needs four kinds of work — planning, occasional
design decisions, implementation, and review — and each has very
different trust requirements. If a single agent (or a single broad
permission set) did all of this, either the review step would not be
independent of the implementation step, or every step would need
dangerous permissions (merge, push, unrestricted shell) just because one
step legitimately needs them.

## Decision

Use four distinct OpenCode agents, each with the narrowest permission set
its job requires, and put all cross-cutting orchestration, Git mutation,
and structured-output validation in a Python supervisor outside any of
them:

- `loop-planner`: read-only, no edits, only `git status/log/diff`.
- `loop-architect`: read-only, invoked only for escalated design
  questions, uses a stronger/more expensive model.
- `loop-builder`: read/write with full shell access, but `git merge*` and
  `git push*` are explicitly denied.
- `loop-auditor`: read-only plus an allowlist of inspection commands
  (`git status/diff/log/show/branch/rev-parse/merge-base`) and `pytest`.
- The Python supervisor: the only actor that creates/removes worktrees,
  verifies builder commits against real Git state, merges accepted task
  branches, and persists resumable state. It never edits application
  code itself.

Every agent returns exactly one JSON object matching a strict, versioned
schema (see `contracts.py`); the supervisor validates this locally rather
than trusting free-form text.

## Consequences

- The auditor's independence is structural, not just a matter of prompt
  wording: it cannot merge, push, or edit even if instructed to.
- The supervisor is a single choke point for merge safety, which makes it
  easier to test (via a fake `AgentRunner`) without needing a real model
  or real OpenCode process for state-machine tests.
- Adding a new role (e.g. a security reviewer) means adding a new agent
  file plus a new contract and phase, not renegotiating existing agents'
  permissions.
