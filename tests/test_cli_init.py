import difflib
import subprocess
import sys
import tomllib
from pathlib import Path

from loop_supervisor.cli import _DEFAULT_LOOP_SUPERVISOR_GIT_URL, build_parser, cmd_init_copy

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIVE_AGENTS_DIR = _REPO_ROOT / ".opencode" / "agents"
_SKELETON_AGENTS_DIR = _REPO_ROOT / "src" / "loop_supervisor" / "_skeleton" / ".opencode" / "agents"

# This repository's own live agent prompts are allowed to name
# repository-specific content the generic skeleton (a new project's
# starting point, per ADR 0018) must not reference -- most notably the
# "Testing discipline" section of this repository's own README.md,
# which assumes this project's own language/tooling (pytest, ruff,
# mypy) and would be actively wrong advice pasted into an arbitrary
# new project. Divergence in loop-builder.md and loop-auditor.md is
# therefore intentional, not drift.
#
# Every live agent also pins this repository's own development model
# choice (its own Argo/OpenCode setup, not something generic to every
# loop-supervisor project -- ADR 0023) with a `model:` frontmatter
# line. The generic skeleton must stay provider-neutral, so that line
# is expected to be absent from every skeleton copy and is stripped
# before comparison in every test below. loop-planner.md has no other
# repository-specific content and is expected to match exactly once
# that one line is set aside. loop-architect.md is additionally
# templated (its `model:` pin is user-overridable at init time via
# `--architect-model`) and is compared separately. This allowlist
# exists so a *future*, unintended divergence in the two
# allowed-to-differ files is still caught if it stops being about
# "Testing discipline" specifically -- see
# test_skeleton_agent_divergence_is_limited_to_testing_discipline
# below.
_EXPECTED_IDENTICAL = ("loop-planner.md",)
_EXPECTED_TO_DIVERGE = ("loop-builder.md", "loop-auditor.md")


def _without_model_line(text: str) -> str:
    """Drop this repository's own `model:` frontmatter pin so live/skeleton
    comparisons focus on prompt content, not this project's own model
    choice (ADR 0023: generated projects ship no provider configuration,
    so the skeleton never pins a model)."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("model: ")) + "\n"


def test_skeleton_agents_planner_matches_live_exactly():
    """loop-planner.md has no repository-specific content beyond its own
    `model:` pin, so the packaged skeleton copy `init` ships to every new
    project must match this repository's own byte-for-byte once that pin
    is set aside -- any other difference here is unintended drift (see
    backlog item 35), not an intentional divergence."""
    for name in _EXPECTED_IDENTICAL:
        live = _without_model_line((_LIVE_AGENTS_DIR / name).read_text())
        skeleton = (_SKELETON_AGENTS_DIR / name).read_text()
        assert skeleton == live, (
            f"{name}: skeleton copy has drifted from the live agent prompt; "
            "sync src/loop_supervisor/_skeleton/.opencode/agents/ from "
            ".opencode/agents/ (ignoring the live-only model: pin), or "
            "update this test if the divergence is now intentional"
        )


def test_skeleton_architect_matches_live_modulo_model_line():
    """loop-architect.md.tmpl differs from the live loop-architect.md in
    exactly one respect: the live file pins `model: argo/GPT-5.6 Sol`
    (this project's own development choice, per ADR 0023), while the
    skeleton substitutes `__LOOP_SUPERVISOR_ARCHITECT_MODEL_LINE__` --
    empty by default, or an `--architect-model`-supplied line at init
    time. Any other difference is unintended drift."""
    live_lines = (_LIVE_AGENTS_DIR / "loop-architect.md").read_text().splitlines()
    skeleton_lines = (_SKELETON_AGENTS_DIR / "loop-architect.md.tmpl").read_text().splitlines()

    matcher = difflib.SequenceMatcher(a=skeleton_lines, b=live_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        skeleton_only = skeleton_lines[i1:i2]
        live_only = live_lines[j1:j2]
        assert (
            tag == "replace"
            and skeleton_only == ["__LOOP_SUPERVISOR_ARCHITECT_MODEL_LINE__temperature: 0.1"]
            and live_only == ["model: argo/GPT-5.6 Sol", "temperature: 0.1"]
        ), (
            "loop-architect.md.tmpl differs from the live agent prompt in a way "
            "not accounted for by the model-line placeholder "
            f"(op={tag!r}, skeleton={skeleton_only!r}, live={live_only!r})"
        )


def test_skeleton_agent_divergence_is_limited_to_testing_discipline():
    """loop-builder.md and loop-auditor.md are allowed to differ from
    their skeleton copies, but only by the deliberate reference to
    this repository's own README "Testing discipline" section --
    anything else differing (including an unrelated line dropped from
    the skeleton copy, which a pure "every extra live line is
    accounted for" check would miss) is unintended drift.

    Approach: delete every *contiguous run* of live-only lines that
    mentions "Testing discipline" from the live text, then require the
    result to match the skeleton exactly. This catches both extra
    unaccounted-for lines in live and any line present in skeleton but
    missing from live, unlike a one-directional line-membership diff.
    """
    for name in _EXPECTED_TO_DIVERGE:
        live_lines = _without_model_line((_LIVE_AGENTS_DIR / name).read_text()).splitlines()
        skeleton_lines = (_SKELETON_AGENTS_DIR / name).read_text().splitlines()

        matcher = difflib.SequenceMatcher(a=skeleton_lines, b=live_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            live_only = live_lines[j1:j2]
            assert tag in ("insert", "replace") and any(
                "Testing discipline" in line for line in live_only
            ), (
                f"{name}: skeleton copy differs from the live agent prompt in a way "
                f"not accounted for by the Testing-discipline reference "
                f"(op={tag!r}, skeleton={skeleton_lines[i1:i2]!r}, live={live_only!r})"
            )


def _run_init(tmp_path, destination_name="new-project", **extra_args):
    destination = tmp_path / destination_name
    argv = ["init", "--destination", str(destination)]
    for flag, value in extra_args.items():
        argv.extend([f"--{flag.replace('_', '-')}", value])
    args = build_parser().parse_args(argv)
    rc = cmd_init_copy(args)
    return rc, destination


def test_init_writes_expected_skeleton_files(tmp_path):
    rc, destination = _run_init(tmp_path)
    assert rc == 0

    for relative in (
        ".env.example",
        ".gitignore",
        ".opencode/agents/loop-planner.md",
        ".opencode/agents/loop-architect.md",
        ".opencode/agents/loop-builder.md",
        ".opencode/agents/loop-auditor.md",
        "docs/OBJECTIVE.md",
        "docs/decisions/README.md",
        "opencode.json",
        "pyproject.toml",
        "pyrightconfig.json",
        "README.md",
        "tests/test_placeholder.py",
    ):
        assert (destination / relative).exists(), relative


def test_init_does_not_vendor_the_supervisor(tmp_path):
    """The new project depends on loop-supervisor as an installed
    package; it must never receive the supervisor's own source or this
    repository's own test suite/history/decisions (backlog item 25).
    `tests/` itself does exist -- see
    test_init_generated_project_pytest_collects_and_passes -- but must
    contain only the generic placeholder, never anything from this
    project's own ~900-test suite."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0

    assert not (destination / "src").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "docs" / "plans").exists()
    tests = list((destination / "tests").iterdir())
    assert [p.name for p in tests] == ["test_placeholder.py"]
    # Only the ADR-format contract is carried over, not this project's
    # own numbered decision history.
    decisions = list((destination / "docs" / "decisions").iterdir())
    assert [p.name for p in decisions] == ["README.md"]


def test_init_never_copies_env(tmp_path):
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    assert not (destination / ".env").exists()


def test_init_rejects_nonempty_destination(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "file.txt").write_text("already here\n")

    args = build_parser().parse_args(["init", "--destination", str(destination)])
    rc = cmd_init_copy(args)
    assert rc == 1


def test_init_defaults_project_name_to_destination_directory_name(tmp_path):
    rc, destination = _run_init(tmp_path, destination_name="my-cool-app")
    assert rc == 0
    with (destination / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    assert data["project"]["name"] == "my-cool-app"


def test_init_accepts_explicit_project_name(tmp_path):
    rc, destination = _run_init(tmp_path, project_name="totally_different")
    assert rc == 0
    with (destination / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    assert data["project"]["name"] == "totally_different"


def test_init_rejects_invalid_project_name(tmp_path):
    destination = tmp_path / "new-project"
    args = build_parser().parse_args(
        ["init", "--destination", str(destination), "--project-name", "1 not valid!"]
    )
    rc = cmd_init_copy(args)
    assert rc == 1
    assert not destination.exists()


def test_init_parameterizes_external_directory_to_parent_and_descendants(tmp_path):
    """`external_directory` must allow both the destination's parent
    itself and descendants beneath it. Task worktrees are siblings one
    directory above the project root, so allowing only the exact parent
    does not match paths inside those worktrees."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    import json

    parent = str(destination.parent)
    config = json.loads((destination / "opencode.json").read_text())
    assert config["permission"]["external_directory"] == {
        "*": "deny",
        parent: "allow",
        f"{parent}/**": "allow",
    }


def test_init_generates_no_provider_or_mcp_configuration(tmp_path):
    """ADR 0023: a generated project ships no provider block and no MCP
    servers -- those are this repository's own development environment
    (Argo, Falda), not something generic to every loop-supervisor
    project. Models resolve from the new project's own global OpenCode
    config, same as any other OpenCode project. Supersedes the old
    Falda-tenant-interpolation regression guard, which is moot now that
    no Falda config is generated at all."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    import json

    config = json.loads((destination / "opencode.json").read_text())
    assert "provider" not in config
    assert "mcp" not in config


def test_init_architect_has_no_model_pin_by_default(tmp_path):
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    text = (destination / ".opencode" / "agents" / "loop-architect.md").read_text()
    assert "model:" not in text
    assert "temperature: 0.1" in text


def test_init_architect_model_flag_pins_the_given_model(tmp_path):
    rc, destination = _run_init(tmp_path, **{"architect_model": "anthropic/claude-opus-4"})
    assert rc == 0
    text = (destination / ".opencode" / "agents" / "loop-architect.md").read_text()
    assert "model: anthropic/claude-opus-4\n" in text
    lines = text.splitlines()
    model_index = lines.index("model: anthropic/claude-opus-4")
    assert lines[model_index + 1] == "temperature: 0.1"


def test_init_pins_loop_supervisor_dependency_to_default_git_url(tmp_path):
    assert _DEFAULT_LOOP_SUPERVISOR_GIT_URL == ("https://github.com/rbross-hpc/loop-supervisor.git")
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    with (destination / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    (dependency,) = data["project"]["dependencies"]
    assert _DEFAULT_LOOP_SUPERVISOR_GIT_URL in dependency


def test_init_accepts_explicit_loop_supervisor_git_url(tmp_path):
    rc, destination = _run_init(
        tmp_path, loop_supervisor_git_url="https://example.invalid/fork.git"
    )
    assert rc == 0
    with (destination / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    (dependency,) = data["project"]["dependencies"]
    assert "https://example.invalid/fork.git" in dependency


def test_init_does_not_leave_a_partial_destination_on_invalid_project_name(tmp_path):
    destination = tmp_path / "should-not-exist"
    args = build_parser().parse_args(
        ["init", "--destination", str(destination), "--project-name", "!!!"]
    )
    rc = cmd_init_copy(args)
    assert rc == 1
    assert not destination.exists()


def test_live_agents_deny_the_skill_tool():
    """Every live agent role must deny the `skill` permission, so the
    planner/architect/builder/auditor never read the bundled
    adopt-loop-supervisor Agent Skill (or any other skill) as live
    instruction -- the exact failure mode ADR 0018 exists to prevent,
    since a skill's guidance is written for the human's interactive
    session, not for an autonomous loop role."""
    for name in ("loop-planner.md", "loop-architect.md", "loop-builder.md", "loop-auditor.md"):
        text = (_LIVE_AGENTS_DIR / name).read_text()
        assert "skill: deny" in text, f"{name}: missing 'skill: deny' in permission block"


def test_skeleton_agents_deny_the_skill_tool(tmp_path):
    """Same guarantee as test_live_agents_deny_the_skill_tool, but for
    what init actually ships to a new project."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    for name in ("loop-planner.md", "loop-architect.md", "loop-builder.md", "loop-auditor.md"):
        text = (destination / ".opencode" / "agents" / name).read_text()
        assert "skill: deny" in text, f"{name}: missing 'skill: deny' in permission block"


def test_init_generated_project_pytest_collects_and_passes(tmp_path):
    """Regression guard: the skeleton's pyproject.toml declares
    `testpaths = ["tests"]`, so a generated project with no tests/
    directory at all fails `pytest` immediately with 'no tests ran'
    before a user has written a single line of their own code. Confirm
    the placeholder test both exists and is collected/passed by a real
    pytest invocation against the generated project, not just that the
    file is present."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(destination),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
