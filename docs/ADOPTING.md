# Adopting loop-supervisor into an existing project

`loop-supervisor init` (see [`docs/INSTALLING.md`](INSTALLING.md))
requires an empty destination directory — it's the right tool for
starting something brand new, but it has no built-in way to bring the
loop into a repository that already has code, history, and its own
conventions.

For that, use the bundled `adopt-loop-supervisor` Agent Skill instead
of trying to do it by hand. Have your agent export and read it:

```bash
loop-supervisor skill export .opencode/skills/adopt-loop-supervisor
```

(For a harness other than OpenCode, check that harness's own
documentation for where it looks for skills — `skill export` takes any
destination path.)

The skill walks through the whole process: checking prerequisites with
`loop-supervisor config validate`, generating a reference skeleton into
a temporary directory, writing `docs/OBJECTIVE.md` and seed ADRs from
the existing codebase, retargeting the builder/auditor toolchain away
from the Python defaults, configuring `opencode.json` and permissions,
and a `--max-steps 1` smoke test before handing off to normal
operation. It also documents two specific configuration mistakes that
produce a run that hangs or fails with no obvious cause — both are
easy to hit by hand and neither is obvious from `opencode.json` alone.

This is intentionally agent-driven rather than a `loop-supervisor
adopt` command: the hardest part of adoption — writing an accurate
objective and inferring a new project's already-existing, unwritten
design decisions — is a judgment call best made by an agent reading
the actual codebase, not a mechanical file copy.

## Co-developing `loop-supervisor` itself alongside a project

`init` and the adoption skill both make a project depend on
`loop-supervisor` as an installed package (`loop-supervisor @
git+<url>` in `pyproject.toml`), not a vendored copy — see
[ADR 0018](decisions/0018-bootstrap-generates-a-dependent-skeleton-not-a-vendored-copy.md).
That means a generated or adopted project has no `loop-supervisor`
source of its own to edit. If you also want to make changes to
`loop-supervisor` while working on such a project (as this repository
does for itself), clone `loop-supervisor` separately and point your
project's dependency at that local checkout instead of the Git URL,
e.g. with an editable install:

```bash
git clone <loop-supervisor-repo-url> ../loop-supervisor
pip install -e ../loop-supervisor
```

There is currently no dedicated `init --fork` mode for this — it was
considered and deliberately not built (see the resolution note on
backlog item 34 in
`docs/plans/2026-08-22-post-lifecycle-fix-backlog.md`); this manual
clone-and-editable-install workaround is the supported path.
