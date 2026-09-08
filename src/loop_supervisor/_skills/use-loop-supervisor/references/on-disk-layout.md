# The on-disk layout: the durable, pull-based view

Everything `loop-supervisor` persists lives under one directory:

```
<git-common-dir>/loop-supervisor/
  runs/<run_id>.json                       current state (authoritative)
  runs/<run_id>/NNNN-<phase>.json          per-phase history (append-only)
  verification/<run_id>/<commit>/NN.log    verification command output
  supervisor.lock                          present only while a run holds it
```

`<git-common-dir>` is `git rev-parse --git-common-dir` run against the
integration checkout — a path inside `.git`, not inside the tracked
worktree, and not inside any task worktree either. If you don't have
the `run_id` yet, `runs/*.json` (excluding the `runs/<run_id>/`
subdirectories) lists every run for that repository; the one with the
newest `updated_at` is the most recently active.

This is the same information `-v`/`-vv` prints live (see
`verbose-output.md`), restated as files: useful when you didn't start
the run with `-v`, when it's someone else's run, or when you need
history predating whenever you started watching.

**Read-only.** Never hand-edit any file here — see
`recovering-an-interrupted-run.md` for what goes wrong. A record can
also be mid-write when you read it (the supervisor's own writes are
atomic-rename, but a read at exactly the wrong instant can still see a
transient absence); treat a parse failure as "read again," not
"corrupted."

## `runs/<run_id>.json` — current state, authoritative

The single source of truth for what phase the run is in right now. It
has 42 top-level fields in total (see `state.py`'s `RunState` dataclass
for the complete, authoritative list); the ones worth reading directly
are:

- **`phase`** — the current phase name. The full vocabulary (from
  `phases.py`) is `planning`, `architecting`, `building`, `verifying`,
  `auditing`, `creating_worktree`, `recording_decision`, `merging`,
  `cleanup_worktree`, `cleanup_branch`, `awaiting_input`,
  `operational_failure`, `done`, `failed`. Only `done` and `failed` are
  terminal; `awaiting_input` and `operational_failure` are the two that
  mean "stop polling, a human decision is needed" (the former always,
  the latter unless the paired `last_error.retryable` is true and
  `resume` is expected to retry it automatically).
- **`original_task_id`**, **`task_worktree_path`**, **`task_branch`**,
  **`task_expected_head`** — which task is active and where its
  worktree/branch/checkpoint commit are, when one is in flight.
- **The six loop counters** — `accepted_task_count`, `revision_count`,
  `replan_count`, `architect_retry_count`, `builder_guidance_count`,
  `operational_retry_count`. Each (other than `accepted_task_count`,
  which only ever grows) measures distance to its own bounded-loop
  limit (`--max-revisions`, `--max-replans`,
  `--max-architect-retries`, `--max-builder-guidance-attempts`,
  `--max-operational-retries` respectively).
- **`pending_question`** — non-null only in `awaiting_input`. Contains
  `kind` (one of `architect_input`, `decision_approval`,
  `builder_guidance`, `builder_escalation`), a human-readable
  `message`, and a `context` object shaped by `kind` (e.g.
  `decision_approval` carries `title`/`decision`; `builder_guidance`
  and `builder_escalation` carry `status` of `BLOCKED` or
  `INCOMPLETE`). `context` is **required**, not optional, whenever
  `pending_question` is non-null.
- **`last_error`** — non-null in `operational_failure` (and left in
  place through a subsequent `failed`). Fields include `message`,
  `retryable`, `retry_phase`, `failed_phase`, `kind`, and `occurred_at`;
  `retryable` is what tells you whether an unattended `resume` (or the
  supervisor's own automatic retry, under `--max-operational-retries`)
  is expected to make progress on its own, versus needing a human.
- **`created_at`** / **`updated_at`** — ISO-8601 timestamps.
  `updated_at` is a *recording* time, not a heartbeat: a long gap since
  the last update is expected mid-invocation and is not by itself
  evidence of a stuck or dead process (see the process/lock checks
  below).
- **`integration_branch`** — the branch this run is merging into; set
  once at run start from whatever was checked out in `--project`.

Everything else on the object — the raw `planner_result`/
`architect_result`/`builder_result`/`verification_result`/
`auditor_result` payloads, worktree/merge checkpoint bookkeeping
(`pending_worktree_*`, `pending_adr_*`, `merge_*`), and schema/identity
fields (`schema_version`, `run_id`, `git_common_dir`,
`integration_path`, ...) — exists mainly for the supervisor's own
resume logic rather than at-a-glance observation. Read them if you need
one specifically; they're safe to inspect, just not usually where
you'd start.

## `runs/<run_id>/NNNN-<phase>.json` — per-phase history, append-only

One immutable record per completed `advance()` call, named
`<seq>-<phase_before>.json` with `seq` zero-padded to at least 4 digits
(e.g. `0004-verifying.json`). **Order by the numeric `seq` field, not
the filename string or any timestamp** — sequence gaps mean an
individual record was excluded as malformed or a duplicate conflict,
never that a phase transition didn't happen.

Each record has exactly these keys: `seq`, `run_id`, `phase`,
`phase_after`, `status` (one of `advanced`, `input_required`,
`input_unavailable`, `operational_failure`, `terminal`), `recorded_at`,
`original_task_id`, `counters`, `result`, `error`.

**Counter-shape caveat, confirmed on real run data:** `counters` has
carried five keys since history capture first shipped
(`accepted_task_count`, `revision_count`, `replan_count`,
`architect_retry_count`, `builder_guidance_count`) and a sixth
(`operational_retry_count`) since the supervisor started retrying
operational failures automatically. History records are immutable —
existing five-key records on disk are not rewritten when the writer
starts emitting six — so **both shapes are permanent and both are
valid**. A missing `operational_retry_count` on an older record means
exactly that: an older record, not a malformed or corrupted one.

History is observational evidence of what already happened; it is
never replayed to reconstruct or repair current state, and it is never
authoritative over `runs/<run_id>.json` if the two ever disagree.

## `verification/<run_id>/<commit>/NN.log` — verification output

Verification command output, one `NN.log` per command (`01.log`,
`02.log`, ...), keyed by **both** `run_id` and the specific commit
verified — not by `run_id` alone. A single run accepts many tasks in
sequence under one `run_id`, and a `REVISE` disposition re-verifies a
new commit within the same task; keying by commit as well as run keeps
each attempt's logs from silently overwriting a previous attempt's
(see ADR 0028 for the incident that made this necessary).

These logs are **unredacted and potentially sensitive** — the same
warning the TUI shows before opening one applies here. Treat their
contents accordingly before pasting them anywhere.

## `supervisor.lock` — repository-level mutation lease

Present only while some `run`/`resume` invocation holds it; absence
means no supervisor process currently claims to be mutating this
repository. Do not infer more than that from presence alone — see
`observing-a-run.md` for how to tell a live holder from a stale one
(PID reuse is a real, handled case, not a hypothetical).
