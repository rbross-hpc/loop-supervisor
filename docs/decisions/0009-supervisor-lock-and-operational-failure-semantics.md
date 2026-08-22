# 0009 — Supervisor lock and operational failure semantics

## Status

Accepted

## Context

Multiple operator sessions (CLI run, CLI resume, TUI) could attempt to
mutate the same Git repository simultaneously. Concurrent mutation could
corrupt the integration branch or produce inconsistent run state. The
supervisor also needed a well-defined policy for transient failures that
occur mid-phase (network timeouts, Git errors, merge conflicts) so that
operators could safely restart without re-doing completed work or losing
diagnosable context.

## Decision

### Repository execution lock

A per-repository lock is stored at:

    <git-common-dir>/loop-supervisor/supervisor.lock

The lock is a mode-0600 JSON file with `schema_version`, `token`, `pid`,
`hostname`, `started_at`, `operation`, `run_id`, and `integration_path`.

Acquisition uses an atomic link(2)-based create-if-absent strategy. The
owner token prevents one process from releasing a successor's lock. The
lock is held for the entire mutating lifecycle including OpenCode shutdown.
Read-only operations (listing runs, loading a run for display) are
lock-free.

Stale-lock recovery is always explicit: the operator passes
`--recover-stale-lock`. A demonstrably dead local PID may be recovered
with this flag. Remote-hostname and malformed locks are never auto-recovered.

**Lock release requires confirmed OpenCode shutdown.** The lock is
released only after `OpenCodeServer.stop()` (or the TUI's equivalent
ownership-preserving cleanup) has been *confirmed* to succeed — not
merely attempted. OpenCode runs beneath a private Python launcher that is
the verified session/process-group leader and remains alive after the
direct server child exits. Startup verifies the anchor before permitting
it to spawn OpenCode. Shutdown sends TERM and KILL commands through an
owned pipe while that anchor is live; successful completion requires the
launcher to be reaped with a SIGKILL return status. The parent never
signals or probes a numeric PGID after anchor loss. A `stop()` that raises
(the process group, control client, launcher pipes, or stdout pump thread
could not be confirmed released) means the lock is deliberately retained
on disk, in both the headless runtime (`run_new()` / `run_resume()` in
`runtime.py`, via the `_LockLease` released only when cleanup is confirmed)
and the TUI (`RunScreen._cleanup_resources()`, which only releases the
lock once `self._server is None`). This is intentional: releasing the lock
while an OpenCode process may still be alive and mutating the working tree
would let a successor process start concurrently mutating the same
repository. An operator who encounters a retained lock after a failed
cleanup must verify no OpenCode process survives before passing
`--recover-stale-lock`.

**Primary errors take precedence over cleanup errors.** Whenever a
primary operation (a run/resume failure, a startup failure) and a
secondary cleanup step (`server.stop()`) both fail, the primary error is
what propagates; the cleanup failure is attached as additional context
(e.g. appended to the exception message) rather than replacing it. This
holds in both the headless runtime and the TUI.

**TUI app-level exit retries indefinitely until cleanup is confirmed
clean.** `LoopSupervisorApp._on_exit_app()` requests shutdown on every
active `RunScreen`, awaits completion, and — if any screen's
`shutdown_clean` is still False — retries automatically on a fixed
interval, forever. There is deliberately no overall exit timeout: the
underlying Textual shutdown sequence (and therefore process exit) must
never be allowed to proceed while the repository lock is held or an
OpenCode process may still be alive. The same retry is available
interactively via "q" / the "Return to runs" button after a failed
attempt.

### Operational failure vs terminal failure

Failures are classified into two categories:

**Operational failures** are transient and resumable. The supervisor
persists an `OperationalErrorRecord` (without tracebacks or secrets) into
`last_error` on `RunState`, sets `phase = "operational_failure"`, and
exits. The next `resume` or TUI retry replays from the `retry_phase`
recorded in the error record. Examples: OpenCode network/timeout errors,
ordinary Git/filesystem errors, merge conflicts requiring operator repair.

**Terminal failures** are non-recoverable policy violations. The supervisor
sets `phase = "failed"` and exits. Examples: revision/replan/architect
retry limits, internal invariant violations, unknown phases.

Tampered or schema-mismatched state is rejected on load without overwriting
the last good state.

### Durable side-effect phases

Before every non-idempotent mutation, the supervisor persists a durable
intent phase:

- `creating_worktree` — intent path/branch/base persisted before `git worktree add`
- `recording_decision` — target path and content hash persisted before ADR write
- `merging` — integration and task HEADs persisted before merge
- `cleanup_worktree` — entered after merge commit is recorded
- `cleanup_branch` — entered after worktree is removed

Each side-effect operation is written to be idempotent: resuming from a
partially completed phase reconciles against the persisted intent rather
than re-executing blindly.

## Consequences

- No two mutating supervisor processes can act on the same repository
  simultaneously.
- Stale locks from crashes require explicit operator acknowledgement to
  remove.
- Transient failures are recoverable by resume; operators see a clear
  error record with a recovery hint.
- Non-recoverable failures produce a clear `failed` state; no further
  resume is possible without starting a new run.
- The `OperationalErrorRecord` never contains tracebacks, HTTP headers,
  environment variables, or secrets.
- State schema v3 (additive migration from v2) carries the new fields
  without breaking existing runs.
