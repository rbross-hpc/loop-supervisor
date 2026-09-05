# Bind active runs to PID-reuse-resistant lock ownership

## Status

Accepted

## Context

ADR 0009 makes `supervisor.lock` the repository mutation lease and keeps it for
its entire mutating lifecycle, but its PID-only liveness check is vulnerable to
PID reuse and a new run initially writes `run_id` as null. ADR 0036 therefore
permits only conservative activity reporting and explicitly defers reliable
attribution.

A separate heartbeat file would require an age threshold to distinguish a live
writer from a crashed one. That would make `running` depend on write frequency,
scheduling delay, filesystem delay, and clock assumptions. It would also
duplicate the lock's lifecycle and introduce windows in which the lock and
heartbeat disagree.

## Decision

Make `supervisor.lock` the sole authoritative active-run artifact. Do not add a
separate heartbeat file or periodic heartbeat writes. A newly written version
of the lock schema must include immutable `owner_boot_id` and
`owner_process_start` fields alongside `pid`. On Linux these are the kernel boot
ID and the process start-time ticks from `/proc/<pid>/stat`, compared as opaque
exact values rather than converted to wall-clock time. Lock acquisition must
obtain this identity for the acquiring supervisor itself and fail closed if it
cannot do so. Readers must continue to recognize schema-version-1 records as
legacy evidence, but those records can never prove a run is running.

For a new run, acquire the lock before state creation with `run_id` null. Then,
immediately after `start_new_run` has durably created and returned the validated
`RunState`, and before OpenCode can start, bind that run ID into the same lock
record. The binding operation must run under the existing guard lock, securely
reread the record, verify the in-memory ownership token and immutable owner
identity, permit only null-to-run-ID or idempotent same-ID binding, and publish
the replacement atomically at mode 0600. Failure to bind aborts entry and
follows the existing safe lock-release path. Resume continues to place its
validated run ID in the initial lock record.

At inspection time, a run is running only if the lock is securely read and
validated, its hostname is local, its `owner_boot_id` equals the current kernel
boot ID, the named PID exists, its current kernel process-start value exactly
equals `owner_process_start`, its integration path matches the selected
repository, and its run ID matches a loadable validated `RunState` for that
repository. A boot mismatch, absent PID, or process-start mismatch is stale
evidence, including PID reuse. Inability to read or compare local kernel
identity is unverifiable evidence, not running and not proof of staleness.
Remote and legacy locks are likewise not evidence that a particular run is
running. This is an observation at inspection time, not a guarantee that the
process remains alive afterward.

The read model may validate the ownership token as part of the on-disk schema,
but must discard it before constructing any typed observation, diagnostic, log
message, or presentation value. The TUI remains strictly read-only: it performs
no lock update, recovery, heartbeat, polling, or other mutation.

ADR 0036's `local_live_associated` category and its `running` label are
superseded in implementation by this complete identity-chain test. Implementers
may add explicit `legacy_unverified` and `local_unverifiable` repository-level
observations so neither is misreported as malformed or stale. The ADR 0036
caveat remains: `running` means validated evidence observed at inspection, not a
heartbeat or proof that a particular phase is currently executing. No category
may be upgraded with recency or timestamp heuristics.

This ADR is a decision only. Writer-side schema and binding changes, read-model
plumbing, TUI label changes, and all implementation tests are explicitly
deferred to follow-on task or tasks. It changes no shipped explorer behavior.

## Consequences

- PID reuse and host reboot cannot make an old lock identify a successor process
  as the active supervisor; no wall-clock comparison or recency threshold is
  involved.
- Fresh-run attribution becomes available immediately after durable `RunState`
  creation. The small pre-creation interval remains repository activity with
  unknown run association and must not be attributed speculatively.
- There is no heartbeat write frequency, background updater, additional cleanup
  artifact, or ongoing filesystem write load. Lock-held lifetime and ADR 0009's
  confirmed-shutdown release rule remain unchanged.
- A crashed supervisor leaves a stale lock, and explicit recovery remains
  required. A retained lock whose supervisor identity is dead still warns of
  stale or unresolved repository activity; it does not label a run running
  merely because an OpenCode child might have survived.
- The lock schema advances for new records. Version-1 locks remain readable for
  conservative diagnostics and explicit recovery compatibility, but never
  satisfy the running proof.
- Reliable running classification requires access to stable kernel boot and
  process-start identity. Unsupported or temporarily unreadable identity sources
  fail closed for acquisition or produce unverifiable read-only observations,
  rather than falling back to PID-only or timestamp-age heuristics.
- Atomic token-verified run binding adds one lock-record replacement per newly
  created run. It reuses the existing guarded trust boundary and never copies
  ephemeral process identity into durable `RunState`.
- The TUI remains manually refreshed and strictly read-only. Its running claim
  means the complete identity chain was valid when that snapshot was inspected;
  ordinary process-exit races after inspection remain unavoidable.
- Implementation is intentionally deferred: a later task must update the lock
  writer, readers, TUI, and tests together while preserving ADR 0009's canonical
  lock ownership and release semantics.

## Implementation status (2026-09-05)

Writer-side schema and binding were already shipped before this status note:
schema-2 acquisition records immutable `owner_boot_id`/`owner_process_start`
and fails closed if it cannot read them, and `bind_run_id` publishes the
run-ID replacement atomically at mode 0600 under the existing guard and
ownership-token check. Stale-lock recovery (`_inspect_existing_lock`) is now
also schema-aware: a schema-2 record's staleness is decided by the complete
identity chain (`classify_local_owner_identity`), so a live PID whose
recorded boot ID or process-start ticks no longer match the current kernel
state -- PID reuse -- is stale and recoverable, exactly as this ADR's Decision
section requires; a schema-2 record whose identity cannot be read or compared
is unverifiable and acquisition/recovery fails closed rather than guessing.
Schema-1 records are unaffected: their staleness is still decided by PID
liveness alone, since they carry no immutable identity to compare.

Read-model activity classification (`observe_lock`'s `running` label), the
TUI, and their tests remain deferred to a follow-on task, per this ADR's
original Decision section. Until that lands, the read-only explorer still
classifies a live-PID schema-1 or schema-2 lock as `local_live_associated`
without the full identity-chain comparison described above -- the writer-side
identity fields exist and are exposed in `LockObservation`, but the reader
does not yet compare them against current kernel state.
