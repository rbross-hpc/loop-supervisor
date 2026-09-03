# Prerequisites (`config validate`)

`loop-supervisor config validate --project . --json` runs nine
independent checks and reports each one's `ok`/`detail` separately, so
you can fix the one thing that's actually wrong rather than guessing
from a single opaque error. It is deliberately offline — it never
makes a network call or confirms your model provider actually
responds (see `loop-supervisor`'s ADR 0022 if you want the rationale).

| Check | If it fails |
|---|---|
| `python_version` | The environment running loop-supervisor itself needs Python >= 3.11. This is about loop-supervisor's own interpreter, not the target project's. |
| `git_executable` | Install git; loop-supervisor shells out to it directly. |
| `opencode_executable` | OpenCode itself isn't installed or isn't on `PATH`. Install it (https://opencode.ai) or pass `--opencode-executable /path/to/opencode` to every `run`/`resume`/`config validate` invocation. |
| `git_repository` | `--project` doesn't point at a git repository. Adoption requires an existing repo — if there isn't one yet, `git init` it first. |
| `git_clean_worktree` | Commit or stash outstanding changes, and make sure you're on a real branch, not detached HEAD. loop-supervisor refuses to start a run on a dirty or detached integration worktree. Note: an `opencode.json` you've chosen not to track counts as a dirty tree unless it's *gitignored* (see `config-and-permissions.md`). |
| `opencode_json` | `opencode.json` is missing or isn't valid JSON. You'll write this in step 4 of `SKILL.md`; before that, this check failing is expected and not a problem to fix yet. |
| `external_directory_permission` | See `config-and-permissions.md` — both the task-worktree parent and its `/**` subtree must be allowed. |
| `agent_definitions` | One or more of `.opencode/agents/loop-{planner,architect,builder,auditor}.md` is missing. You'll copy these in step 3. |
| `dotenv_file` | No `.env` in the project root. Copy `.env.example` to `.env` and fill in whatever credentials your configured provider needs. |

The report also includes an `env` section listing whether a small set
of provider-related variable names loop-supervisor's own project
happens to use (`ARGO_API_KEY`, `FALDA_TOKEN`, `FALDA_TENANT`) are set
— **values are never included**, only whether each is set. If the
target project uses a different provider, these will correctly show as
unset and that is not itself a failure; only the named `checks` gate
`"ok"`.

Steps 0 and 5 of `SKILL.md` are the only two points where you should
run `config validate`. It's normal for several checks to fail at step
0 (before `opencode.json`/agents/`.env` exist) — that's exactly what
tells you which of steps 1-4 still need doing. By step 5, after
completing steps 1-4, every check should pass.
