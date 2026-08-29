# Bootstrap generates a small dependent skeleton, not a vendored copy of the supervisor

## Status

Accepted

## Context

ADR 0004 established `loop-supervisor init --destination <path>` as a
safe way to seed "new, unrelated projects" from this template. In
practice it copied every one of this checkout's ~75 Git-tracked
files — the entire `src/loop_supervisor/` package (16 modules,
~8,700 lines), the full `tests/` suite, ADRs 0001–0017 documenting the
supervisor's own internals, this project's own `docs/plans/`, and a
380-line `README.md` about the supervisor — of which perhaps 10
belonged in a new project. This was backlog item 25: "`init
--destination` bootstraps a fork of the supervisor, not a new
project," and is not cosmetic. All four agent roles read `README.md`
and `docs/decisions/` as canonical truth, so a freshly bootstrapped
project pointed its planner, builder, and auditor at the supervisor's
own design rather than the new project's; the auditor held `pytest *`
and would judge "test adequacy" against the supervisor's own ~860
tests; the builder held `edit: allow` over supervisor source entirely
unrelated to its actual task. This is the same failure class
independently observed and fixed for a single project's own README in
`test-run-2`'s original template fixture.

Item 25 also named the real blocker to fixing this: whether a
bootstrapped project **depends on** `loop-supervisor` as an installed
tool, or **vendors** it. Copy-mode implicitly vendored. Choosing the
dependency model requires the packaged-resource bootstrap mechanism
ADR 0007 explicitly deferred as future work (copy-mode's `git
ls-files`-based implementation could not run from an installed wheel
with no `.git` present), plus generated (not copied) `README.md` and
`pyproject.toml` scaffolds this codebase had no templating mechanism
for.

Separately, `init --in-place` (also from ADR 0004) only ever made
sense under the vendoring model: its entire purpose was stripping
`.git` from a checkout that already looked like the supervisor's own
source tree (`pyproject.toml` + `src/loop_supervisor` +
`.opencode/agents` all present), so it could be repurposed as a fresh
repo's first commit. Once bootstrapping stops vendoring the
supervisor, there is no longer a workflow that produces a directory
matching that shape for a new project to strip `.git` from.

## Decision

1. `loop-supervisor` is depended upon, not vendored. `init
   --destination <path>` writes a **packaged skeleton** — bundled as
   package data inside the `loop_supervisor` distribution itself
   (`src/loop_supervisor/_skeleton/`, shipped via
   `[tool.setuptools.package-data]`) and read with
   `importlib.resources.files("loop_supervisor").joinpath("_skeleton")`
   — into a fresh, empty destination directory. This needs no `.git`
   to be present anywhere, in the source or the destination, and works
   identically whether `loop_supervisor` is an editable install
   (resolves into this checkout's own `src/`) or a real wheel install
   (resolves into `site-packages/`). This directly closes the gap ADR
   0007 deferred.
2. The skeleton contains only: `.opencode/agents/*.md` (with
   `docs/OBJECTIVE.md` and `docs/plans/` already named per ADR 0017),
   `opencode.json`, `.gitignore`, `.env.example`, `pyrightconfig.json`,
   `docs/decisions/README.md` (the ADR format contract, not this
   project's own numbered decisions), a stub `docs/OBJECTIVE.md`, a
   generated `README.md` describing the loop mechanics generically
   (not this repository's own history), and a generated
   `pyproject.toml` declaring the new project's own name and a
   `loop-supervisor @ git+<url>` dependency. It contains no
   `src/loop_supervisor/`, no `tests/`, no ADRs 0001–0018, and no
   `docs/plans/` content.
3. Three files need per-project values that can't simply be copied,
   so they ship as `.tmpl` sources with `__LOOP_SUPERVISOR_..._`
   placeholders, substituted at `init` time and written without the
   `.tmpl` suffix:
   - `pyproject.toml.tmpl`: project name (`--project-name`, default:
     the destination directory's name) and the `loop-supervisor`
     dependency's Git URL (`--loop-supervisor-git-url`, default: this
     project's own origin).
   - `opencode.json.tmpl`: `external_directory`'s allow-path is set to
     the **destination's parent**, not the destination itself — task
     worktrees are created as siblings one directory above the
     project root by default (see README's "Sibling task
     worktrees"), so that is the path OpenCode actually needs
     permission to reach, matching this project's own
     `opencode.json`.
   - The hardcoded `X-Falda-Tenant: "lte-project"` literal that this
     project's own `opencode.json` carries is **not** parameterized at
     all: it uses the same `{env:FALDA_TENANT}` interpolation already
     used for `ARGO_API_KEY` and `FALDA_TOKEN`, resolved by OpenCode
     from the environment rather than baked in at generation time.
     This avoids introducing a second templating mechanism for a value
     that fits the existing one.
4. `init --in-place` is removed outright rather than kept or
   repurposed. Its precondition (a checkout that looks like the
   supervisor's own source tree) can no longer arise from any
   bootstrap workflow once copy-mode stops vendoring; keeping it would
   be dead code advertising a workflow nothing produces. A future
   `init --fork` mode, if the self-hosting regression below is ever
   judged to need a fix, would be a new decision on its own merits, not
   a repurposing of this one.
5. `tests/test_cli_init.py` is rewritten rather than incrementally
   patched: every test in it exercised the removed Git-checkout-based
   mechanism (`git ls-files`, fake tracked/untracked fixtures,
   `--in-place`'s confirmation and dirty-tree flows), none of which
   exist anymore.

This supersedes ADR 0007's tracked-files-only copy mechanism (its
safety property — never copying anything Git doesn't track, i.e.
`.env`, is preserved by construction here: the skeleton is fixed
package data with no `.env` in it at all, not filtered from a live
checkout) and narrows ADR 0004's two-bootstrap-mode design to one.

## Consequences

- A new project's planner, builder, and auditor now read only that
  project's own `docs/OBJECTIVE.md`, `README.md`, and
  `docs/decisions/` — never the supervisor's own design documents or
  test suite. Verified live: `loop-supervisor init --destination`,
  writing a distinctive `docs/OBJECTIVE.md`, then `run --max-steps 1`
  produced a `task-001` whose `objective`/`rationale` matched exactly
  what was written and cited `docs/OBJECTIVE.md` by name — the same
  verification method used for ADR 0017.
- **Self-hosting regression** (flagged, not fixed here): this
  repository improves itself via its own loop today. Nothing in the
  dependency model provides a supported way for a new project to also
  hack on the supervisor's own source the way this repository does;
  that would require its own `init --fork`-style mode, filed as a
  backlog item rather than solved here.
- **Versioning** (flagged, not fixed here): a generated project's
  agent definitions are a point-in-time copy, and its `pyproject.toml`
  pins `loop-supervisor` to a Git URL with an explicit
  "pin this to a released version or tag" TODO, not a version
  constraint — there is no released version yet. Agent-definition
  compatibility with whatever `loop-supervisor` version a project
  later upgrades to is unenforced. Filed as a backlog item.
- `opencode.json.tmpl`'s `external_directory` parameterization directly
  fixes one of the two "silently-wrong hardcoded values" identified in
  `docs/plans/2026-08-29-objective-channel-and-bootstrap.md`; the other
  (`X-Falda-Tenant`) is fixed by removing the hardcoding entirely
  rather than parameterizing it, per point 3 above.
- `--in-place` is gone; anyone relying on it to seed a project from a
  pre-existing clone of this repository must switch to `--destination`
  into a fresh directory instead. There is no deprecation period —
  this project has no external users yet.
