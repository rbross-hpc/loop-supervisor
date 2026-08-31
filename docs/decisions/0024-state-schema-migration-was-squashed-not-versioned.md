# State schema migration was squashed, not carried forward

## Status

Accepted

## Context

`RunState.from_dict` (`state.py`) enforced an exact field set in both
directions: any unknown field or any missing field was fatal. Adding a
single new field to `RunState` or its nested `RunOptions` therefore
required bumping `STATE_SCHEMA_VERSION`, writing a migration function
mirroring the existing v2->v3 machinery (`_migrate_v2_to_v3`,
`_V2_FIELDS`, `_V3_ONLY_FIELDS`, `phases.V2_PHASES`, plus the strict
v2-field-set/v2-phase-vocabulary enforcement around all of it), and
carrying that machinery forward indefinitely.

This project has no users and no production installs: every `RunState`
document that has ever existed was created by this same codebase,
during its own development. There was no real document anywhere
carrying schema v1 or v2 that a migration needed to keep loading. The
v2->v3 migration path was pure carrying cost — real code, real tests,
and a real audit surface, purchased for compatibility nobody used.

This was backlog item 30, filed after implementing item 27
(`PermissionDenier`): the natural place to persist denied-permission
counts would have been a new `RunState` field, but doing so would have
forced a v3->v4 migration. That cost was avoided at the time by
keeping the counts in-memory only, but the next legitimate field
addition — the provisioning and verification configuration this ADR's
sibling changes introduce — would face the identical tax, compounding
forever (v3->v4, then v4->v5, ...).

## Decision

The v1/v2/v3 migration history was deleted rather than carried
forward. `STATE_SCHEMA_VERSION` was reset to `1`, with a comment on
the constant explaining the reset and stating explicitly that there is
no migration path into the current schema: any persisted document
whose `schema_version` does not exactly equal the current value is
rejected, with the recovery instruction to start a new run. This
matches backlog item 30's own suggested resolution.

Removed: `_migrate_v2_to_v3`, `_V2_FIELDS`, `_V3_ONLY_FIELDS`
(`state.py`), `V2_PHASES` (`phases.py`), the v1-specific rejection
message and the v2-acceptance branch in `RunState.from_dict`, and the
`source_version`-conditional exception in `_validate_phase_invariants`
for a migrated-v2 `failed` state with no `last_error` (a case that no
longer exists once there is no migration path).

## Consequences

- Every run-state file that existed anywhere on disk before this
  change — including in `test-run` and `test-run-2`'s
  `.git/loop-supervisor/runs/` — is now permanently unloadable and was
  deleted rather than migrated, per the backlog item's own guidance.
  Any run in flight at the time of this change must be finished (or
  abandoned) before merging, not resumed afterward.
- The next `RunOptions`/`RunState` field addition (immediately
  following, for worktree provisioning and supervisor-run
  verification) is now a plain field addition with no migration
  machinery required.
- This policy is explicitly revisited only if the project ever
  acquires a real installed user base whose in-flight run state must
  survive an upgrade — at that point, schema migrations should be
  taken seriously again rather than squashed on the next convenient
  occasion.
</content>
