# Re-read bounded verification content to detect mutation

## Status

Accepted

## Context

ADR 0036 defines explicit verification-log opening as a bounded,
descriptor-relative, no-follow read and describes mutation reporting as
best-effort. The shipped post-read check compares the opened descriptor's
inode, size, and modification time before and after its first bounded read,
then reopens the authorized name to detect replacement by a different inode.

That check detects an inode-replacing rewrite that is present when the name
is reopened, including a replacement with the same size and modification
time. It also detects an in-place rewrite of the opened inode that changes
its size or `mtime_ns` before the post-read descriptor observation. It does
not detect a genuine same-size, same-`mtime_ns` in-place rewrite of the
opened inode. A supported filesystem can provide timestamp behavior coarse
enough to miss an ordinary concurrent rewrite in that shape, and a rewrite
that restores the original modification timestamp is also missed. Nor does
this finite observation detect a mutation that occurs only after the relevant
final observation.

The existing test changes the file size and therefore overstates the scope
implied by its in-place-mutation name.

## Decision

Do not accept the metadata-only gap as the finished contract. Keep this item
limited to recording the scope and correcting overstated documentation and
test naming, then implement stronger detection as a separate follow-on slice.
That slice will perform a second read, bounded by the same 1 MiB-plus-sentinel
limit, from the already-open regular-file descriptor and compare it with the
bytes obtained by the first read. A byte difference marks the log as changed
even when inode, size, and modification time are unchanged.

The follow-on metadata comparisons will cover device, inode, size,
modification time, and change time at the available observation points, and
the post-read reopen-by-name check will compare device and inode. The
operation remains read-only, descriptor-relative, no-follow, bounded, and
non-retrying. The returned text remains the first bounded read with a change
warning. Detection remains observational rather than a transactional snapshot
guarantee: mutations outside the bounded region, mutations fully reverted
between observations, and changes after the final observation need not be
detected. A mutation warning is diagnostic only and never changes current
`RunState`'s authority.

## Consequences

- A follow-on implementation task is required, with a failing-first test that
  performs a genuine same-inode, same-size rewrite, restores the original
  `mtime_ns`, and proves the byte comparison reports the change.
- Opening a verification log may read at most twice the existing bounded
  input, but this work occurs only after an explicit user action and remains
  independent of total file size.
- Replacement, metadata-changing, and stable content-changing races are
  detected more reliably without introducing writes, retries, path-based
  reopening for content, or an unbounded consistency protocol.
- The `changed_during_read` flag continues to be a conservative warning based
  on finite observations, not proof that the returned bytes form an atomic
  filesystem snapshot or that no undetected mutation occurred.
