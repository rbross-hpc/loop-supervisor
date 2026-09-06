# Accept metadata-only verification mutation detection

## Status

Accepted

## Context

ADR 0040 found that the shipped post-read mutation check does not detect a
genuine same-size, same-`mtime_ns` in-place rewrite of an opened verification
log, and decided against accepting that gap as the finished contract. It
committed to a follow-on slice that re-reads the same bounded region from the
already-open descriptor and compares bytes, so a same-size in-place rewrite is
still reported as `changed_during_read` even when its metadata is unchanged.

That decision was made without visibility into how verification logs are
written. `_summarize_verification` (`supervisor.py`) writes each command's
log exactly once, with `Path.write_text`, to a fresh path
(`verification/<run_id>/<commit>/NN.log`) that is unique to that commit and
that command's position among the commands run for it, then `chmod`s the file
to `0o600`. No code path in this project reopens, appends to, or rewrites a
verification log after that single write. A same-size, same-content-length,
different-content in-place rewrite of an already-written verification log is
therefore not a mutation shape the writer of this project can produce.

`changed_during_read` has exactly one consumer: one warning line in the
run-detail log viewer (`tui/browser.py`), shown only when a user explicitly
opens a specific log. It is a diagnostic, not a correctness mechanism --
nothing in this project branches on it, repairs state from it, or treats its
absence as proof a log is intact.

The one mutation shape with a plausible real cause in this project is
replacement: pruning a run's verification logs (`loop-supervisor prune
--include-verification`) while the TUI has that run's log open. Replacement
is already detected today, including a same-size, same-timestamp
replacement, by reopening the authorized name after the read and comparing
its inode number against the pinned descriptor's.

## Decision

Supersede ADR 0040's follow-on-slice decision. Metadata-only mutation
detection -- comparing the pinned descriptor's inode, size, and modification
time before and after the bounded read, then reopening the name and comparing
its inode number -- is accepted as the finished contract for
`changed_during_read`. The byte-comparison slice ADR 0040 committed to is not
scheduled.

This does not reverse ADR 0040's factual findings about what the shipped
check does and does not detect; those remain accurate. It reverses only the
judgment that the undetected shape must be closed, given that the writer
this project ships cannot produce that shape, and given the cost of guarding
against it -- a second bounded read on every log open, in service of a single
warning line -- is disproportionate to a mutation source that does not exist
in this codebase.

`docs/OBJECTIVE.md`'s item 23 already reflects this scope (recording ADR 0040
and correcting overstated wording); no further objective change is needed.
The existing in-place-mutation test's name, which ADR 0040 flagged as
overstating coverage by exercising a size-changing rewrite rather than a
genuine same-size one, is left as-is: it is an accurate test of the shipped
metadata comparison, and the metadata comparison is now the accepted
contract.

## Consequences

- No further verification read-model change is required by this decision;
  `read_log`'s existing single-read implementation stands.
- `changed_during_read` remains a conservative warning based on finite
  metadata observations, not proof that the returned bytes form an atomic
  filesystem snapshot.
- If a future change introduces a code path that rewrites a verification log
  in place after its initial write, this decision's premise no longer holds
  and the gap ADR 0040 identified should be reconsidered against the writer
  behavior in effect at that time.
- Replacement detection, including same-size and same-timestamp replacement,
  is unaffected and remains the primary defense against the one plausible
  real-world race (log pruning against a concurrent TUI read).
