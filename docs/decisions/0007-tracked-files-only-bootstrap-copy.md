# Copy-mode bootstrap copies only Git-tracked files, not a denylist-filtered checkout

## Status

Accepted

## Context

ADR 0004 established `loop-supervisor init --destination <path>` as a
safe, non-destructive way to bootstrap a new project from this template,
promising that it "copies all tracked template files" while excluding
`.git`, `.env`, and local caches/build artifacts. The implementation did
not actually match that promise: it walked every entry in the source
checkout and excluded only a fixed set of names (`.git`, `.env`,
`__pycache__`, and similar). Any other untracked file — a stray secret,
a locally-generated credentials file, an ignored file whose name wasn't
on the list — would have been copied into the new project along with the
genuine template content.

`.gitignore` is repository policy for what `git` itself tracks and
diffs; it was never meant to be, and should not be treated as, a runtime
security boundary for what a bootstrap command is allowed to copy.

## Decision

- Copy-mode bootstrap (`cmd_init_copy`) now lists the source checkout's
  Git-tracked files with `git ls-files -z` and copies exactly that set,
  preserving relative paths. This is a positive allowlist: only files
  Git actually tracks are ever copied, regardless of what untracked
  files happen to exist alongside them.
- The source must be a real Git checkout with a readable index; if
  `git ls-files` fails (not a Git repository, corrupted index, etc.) or
  reports no tracked files, the command fails closed with an explicit
  error rather than falling back to scanning the filesystem.
- If copying fails partway through, a destination directory this
  invocation created is removed rather than left half-populated.
- This means copy-mode bootstrap currently only works from a source
  checkout with `.git` present — not from an installed wheel with no
  Git metadata. Supporting a wheel-based bootstrap (e.g. via packaged
  template resources) is left as future work; this decision only fixes
  the safety of the source-checkout path documented in ADR 0004.

## Consequences

- An untracked secret, credential file, or other sensitive local content
  sitting in the source checkout can never be copied into a new project,
  no matter what it's named — the allowlist is "tracked by Git," not
  "not on this denylist."
- Copy-mode bootstrap is unavailable for installed-package use until a
  packaged-resource-based approach is added; the README documents this
  limitation rather than implying broader support than exists.
- `--in-place` bootstrap is unaffected: it only removes `.git` from an
  existing checkout and never copies files at all.
