# Disk-backed, read-only TUI data boundary

## Status

Accepted

## Context

ADR 0034 made outcome-only phase history durable under
`runs/<run_id>/NNNN-<phase>.json`. ADR 0035 then retired the TUI that owned a
`RunSession` and consumed in-process SSE events, leaving `tui` as a stub pending a
disk-backed replacement. ADRs 0027 and 0028 locate verification output under the
Git common directory, keyed by run and commit. ADR 0009 defines the repository lock
and permits lock-free reads.

The replacement is an explorer of concurrently changing, supervisor-owned files.
Those files can be absent, pruned, malformed, from an unsupported schema, or
partially written. Names and contents are untrusted path and display input. The
current lock can provide limited evidence of activity, but it is not durable run
state and has no heartbeat or protection against PID reuse. Without one shared
contract, presentation code could follow unsafe paths, disagree about which record
is authoritative, infer activity from timestamps, or let one bad artifact hide all
other runs.

## Decision

The replacement TUI is a strictly read-only view over a fresh disk snapshot. It does
not import or construct `RunSession`, acquire or recover the supervisor lock, start
or resume work, consume SSE, read OpenCode private storage, or write any repository
or supervisor artifact. There is no polling, tailing, file watching, or
recency-based activity inference.

### Module boundary and typed results

A presentation-independent read-model package owns the complete disk boundary and
returns immutable typed values rather than filesystem paths or unchecked JSON. Its
responsibilities are separated as follows:

- project resolution accepts the optional project path (the current directory when
  omitted), resolves it through `GitRepo`, and returns the canonical integration
  root, Git common directory, and supervisor-owned roots; failure is reported before
  Textual starts;
- run discovery securely enumerates candidate `<run_id>.json` leaves, validates each
  filename with `validate_run_id`, and returns one `RunSummary` per valid candidate,
  including a degraded summary when its state cannot be loaded;
- current-state loading delegates exclusively to `state.load_state` and maps a valid
  `RunState` into a typed `CurrentRun`; it does not parse unsupported schemas itself;
- history loading returns ordered `HistoryEntry` values plus typed per-artifact
  diagnostics and completeness metadata;
- verification discovery returns typed attempt and log metadata, while a separate,
  explicit bounded-log operation returns `LogContent`;
- lock observation returns a repository-level `LockObservation` and derived
  per-run activity labels without ever returning the ownership token; and
- a snapshot coordinator returns one immutable `ProjectSnapshot` containing the
  runs, diagnostics, and lock observation consumed by the presentation layer.

The package has no Textual, Rich, or `RunSession` dependency. The UI does not list,
open, parse, or classify files itself. Existing validators may be promoted to public
helpers, but the reader must not depend on private mutation routines. Discovery and
all descendant opens use descriptor-relative, no-follow operations and regular-file
or real-directory checks equivalent to `state.load_state`; the current
`state.list_runs` glob is not by itself a sufficient security boundary because it
can follow a symlinked `runs` directory or expose symlink leaves.

### Durable state and activity

Durable workflow state and observed activity are independent axes. A loadable run's
durable state is its validated current `RunState.phase`, presented with explicit
terminal `done`, terminal `failed`, `awaiting_input`, and `operational_failure`
variants where applicable. A candidate whose current snapshot is unreadable,
missing during its load, symlinked, non-regular, malformed, identity-mismatched, or
uses an unsupported schema is `unloadable`, with an actionable diagnostic and no
invented phase or timestamp. History never changes this classification.

Repository activity has exactly these observation variants:

- `absent`: no lock existed at inspection time;
- `local_live_associated`: a strictly validated lock names a run, its hostname is
  local, its PID responds to the existing liveness check, its canonical integration
  path identifies the selected project, and the named loadable `RunState` has the
  same run ID and integration path;
- `fresh_run_unassociated`: a validated local live lock has `run_id = null`; this is
  repository activity with unknown run association;
- `unassociated`: a validated local live lock names no discovered, loadable run;
- `mismatched`: a validated lock's integration path, named run identity, or loaded
  run integration path conflicts with the selected project;
- `stale`: a validated local lock names a PID that is demonstrably not live;
- `remote`: a validated lock names another hostname, so local liveness is unknown;
  and
- `malformed`: the lock path or record cannot be safely opened and strictly
  validated, including a symlink or non-regular file.

A run may be labeled `running` only for `local_live_associated`, and only the named
run receives that label. Every other run is `not evidenced running`; with an absent
lock it may additionally say `inactive at inspection time`. Fresh-run, unassociated,
mismatched, stale, remote, and malformed observations remain repository-level
warnings and never make a particular run `running`. No category is upgraded using
`updated_at`, `recorded_at`, lock age, a nonterminal phase, or other recency.
`started_at`, hostname, PID, operation, association, and safe diagnostics may be
shown; the ownership token is discarded inside the lock reader before any typed
value is constructed. Because PID reuse remains possible, `running` means that the
specified lock evidence was observed, not a heartbeat or proof of current phase
execution.

### Snapshot and manual-refresh semantics

Initial load and each explicit refresh build a new `ProjectSnapshot` from disk in
isolation, with no reuse of prior artifact values. Once the scan finishes, the UI
replaces the prior snapshot as one unit and reconciles selection by run ID. Thus an
added run appears, a removed run disappears, changed state/history/verification or
lock data replaces its old value, and a newly malformed artifact replaces its old
valid value with a degraded value and diagnostic. A selected run that disappears
returns the user to an explicit removed/unavailable state rather than retaining
stale details. Open log content is closed or replaced; it is never silently carried
across refresh as though still current.

This is a bounded best-effort scan, not a transaction across files. Every operation
handles `ENOENT` after enumeration as concurrent disappearance. A disappearing run
candidate is omitted with a scan diagnostic; a disappearing history or verification
artifact is marked unavailable on its run. A project-level failure that prevents a
safe scan leaves the already displayed snapshot intact and reports refresh failure;
a completed scan, including degraded rows, replaces it. The observation time is UI
metadata only. The reader never retries until files become consistent and never
turns timestamps into phase starts, durations, heartbeat, stall, or workflow claims.

### History discovery and validation

History is discovered only in the real, non-symlink
`runs/<validated-run-id>/` directory associated with a discovered run. A candidate
filename must match `(?P<seq>[0-9]{4,})-(?P<phase>[a-z_]+).json`; temporary files,
invalid names, symlinks, directories, and other non-regular leaves are excluded with
per-artifact diagnostics. The phase must be a known supervisor phase and sequence
must be a positive integer.

Each bounded JSON object must contain exactly the ADR 0034 fields: `seq`, `run_id`,
`phase`, `phase_after`, `status`, `recorded_at`, `original_task_id`, `counters`,
`result`, and `error`. Scalar types are strict; `recorded_at` is timezone-aware
ISO-8601; both phases and status use their current enums; all five counters are
present as non-negative integers; and optional task identity is null or a non-empty
string. The embedded run ID, phase, and sequence must equal the selected run,
filename phase, and filename sequence. A non-null result must validate against the
contract for the producing phase (including the compact verification-result
contract), and a non-null error must validate as `OperationalErrorRecord`; phases
that produce neither cannot smuggle in a result.

Valid records are ordered by numeric sequence, not timestamp or lexical filename.
Sequence gaps are preserved and reported as incomplete; they are never filled or
interpreted as proof that no transition occurred. Multiple filenames for one numeric
sequence are a duplicate conflict: all records at that sequence are excluded and a
diagnostic names the conflict, rather than choosing one nondeterministically.
Malformed records are excluded individually and leave a visible diagnostic at their
sequence when one can be recovered from the filename. This includes truncated or
partially written JSON; a malformed newest record is not fatal or hidden, and all
earlier valid records remain available. Concurrent disappearance is handled the
same way. History can therefore be `absent`, `complete`, or `incomplete` with
reasons, but `absent` never means a phase did not run.

The validated current `RunState` is authoritative for current phase, task, counters,
latest result, pending question, and error. History is immutable observational
evidence of completed `advance()` calls. It is not replayed to reconstruct or
repair state, and a disagreement is reported without overriding current state.
`recorded_at` is completion/recording time, not phase-start time.

### Verification discovery and bounded logs

Verification is rooted only at
`<git-common-dir>/loop-supervisor/verification/<run_id>/<commit>/`. The run component
uses `validate_run_id`; commit directory names use ADR 0028's bare hexadecimal
7-to-64-character validator. Only real, non-symlink directories reached beneath an
open, verified repository-owned verification root are traversed. Log leaves must
have a positive decimal command ordinal and the exact form `[0-9]{2,}.log`; symlink,
non-regular, invalid, duplicate-ordinal, and unexpected entries become diagnostics
and are never opened as logs.

Persisted `output_path` is evidence metadata, not authority for opening an arbitrary
absolute path. A verification command is associated with a log only when its
validated run ID, validated commit identity, and ordinal derive the exact expected
path beneath the selected verification root. Any absolute path is normalized only
for comparison; it is never followed. A summary that names another run, commit,
ordinal, or path is `mismatched` and has no openable log. Orphaned attempts may be
listed as unreferenced evidence, but never attributed to a task or command without a
matching validated state or history summary.

Discovery reads metadata and bounded structured summaries only; it never loads full
log bodies. Opening a selected log is an explicit action that warns that output is
unredacted and potentially sensitive. The reader opens the leaf descriptor-relative
with `O_NOFOLLOW`, verifies a regular file with `fstat`, reads at most 1 MiB plus one
sentinel byte, and decodes UTF-8 with replacement for invalid bytes. The returned
content is then bounded to at most 256 KiB and 10,000 lines, with explicit byte and
render truncation flags and a visible truncation marker. These are hard upper bounds,
not defaults controlled by file contents.

Pruning or disappearance before or during open returns `unavailable`, not an empty
successful log. Metadata changes observed around the bounded read produce a
`changed_during_read` warning; no unbounded retry occurs. At every component and
leaf, containment comes from validated single components plus descriptor-relative
no-follow traversal, not string-prefix checks or `Path.resolve()` followed by an
ordinary open.

### Degradation and safe rendering

Errors are scoped to the narrowest artifact. A malformed current snapshot degrades
one run; a malformed history record or verification attempt degrades one entry; a
bad lock degrades activity only. Other runs and valid sibling records continue to
load. Diagnostics contain the safe logical artifact name and reason, not unchecked
file contents, tracebacks, secrets, or the lock token.

All repository-, agent-, command-, path-, JSON-, and error-controlled strings are
untrusted plain text. The read model never produces Rich markup. The presentation
layer must insert these values through `Text`/text-node APIs or with markup disabled;
if markup interpolation is unavoidable it applies Rich escaping at that final
boundary. Raw JSON is generated from the already bounded record and displayed by
the same literal-text path. No untrusted value is concatenated into a Rich markup
string, widget identifier, selector, or format string. Missing or malformed evidence
is displayed as unavailable/incomplete and never converted into fabricated timing,
activity, phase execution, verification, or workflow claims.

## Consequences

- Read-model and filesystem-security behavior can be tested without Textual; later
  UI work consumes stable typed snapshots and remains responsible only for safe
  literal rendering, navigation, and explicit user actions.
- The browser remains useful when one run, history record, verification log, or lock
  is bad, while preserving diagnostics instead of silently inventing replacements.
- `running` is deliberately conservative and may under-report real work when a lock
  is fresh, remote, mismatched, stale-looking, or unassociated. Reliable attribution
  or heartbeat persistence remains a separate future decision.
- Manual refresh can expose a snapshot/history boundary created by concurrent
  mutation. Current state remains authoritative and incompleteness remains visible;
  polling and reconciliation heuristics are not introduced.
- Verification viewing is bounded and opt-in. Large or changing logs are truncated
  or warned about, and a persisted absolute `output_path` can never redirect the
  explorer outside repository-owned storage.
- Implementing the boundary requires secure discovery and read APIs beyond the
  existing writer-oriented `history.py`, private verification path helper, and
  coarse lock helpers. Those APIs and the Textual interface are deferred to later
  units; this decision changes documentation only.
