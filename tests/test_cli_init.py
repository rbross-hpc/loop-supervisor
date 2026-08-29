import difflib
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
# therefore intentional, not drift; loop-planner.md and
# loop-architect.md have no such repository-specific content and are
# expected to match exactly. This allowlist exists so a *future*,
# unintended divergence in the two allowed-to-differ files is still
# caught if it stops being about "Testing discipline" specifically --
# see test_skeleton_agent_divergence_is_limited_to_testing_discipline
# below.
_EXPECTED_IDENTICAL = ("loop-planner.md", "loop-architect.md")
_EXPECTED_TO_DIVERGE = ("loop-builder.md", "loop-auditor.md")


def test_skeleton_agents_planner_and_architect_match_live_exactly():
    """loop-planner.md and loop-architect.md have no repository-
    specific content, so the packaged skeleton copy `init` ships to
    every new project must be byte-identical to this repository's own
    -- any difference here is unintended drift (see backlog item 35),
    not an intentional divergence."""
    for name in _EXPECTED_IDENTICAL:
        live = (_LIVE_AGENTS_DIR / name).read_text()
        skeleton = (_SKELETON_AGENTS_DIR / name).read_text()
        assert skeleton == live, (
            f"{name}: skeleton copy has drifted from the live agent prompt; "
            "sync src/loop_supervisor/_skeleton/.opencode/agents/ from "
            ".opencode/agents/ (or update this test if the divergence is "
            "now intentional)"
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
        live_lines = (_LIVE_AGENTS_DIR / name).read_text().splitlines()
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
    ):
        assert (destination / relative).exists(), relative


def test_init_does_not_vendor_the_supervisor(tmp_path):
    """The new project depends on loop-supervisor as an installed
    package; it must never receive the supervisor's own source, tests,
    or this repository's own history/decisions (backlog item 25)."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0

    assert not (destination / "src").exists()
    assert not (destination / "tests").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "docs" / "plans").exists()
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


def test_init_parameterizes_external_directory_to_the_destination_s_parent(tmp_path):
    """`external_directory` must allow the destination's *parent*, not
    the destination itself: task worktrees are created as siblings one
    directory above the project root by default (see README's "Sibling
    task worktrees"), so that is the path OpenCode actually needs
    permission to reach."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    import json

    config = json.loads((destination / "opencode.json").read_text())
    assert config["permission"]["external_directory"] == {
        "*": "deny",
        str(destination.parent): "allow",
    }


def test_init_falda_tenant_uses_env_interpolation_not_a_literal(tmp_path):
    """Regression guard for the hardcoded `X-Falda-Tenant` literal this
    repo's own opencode.json carries; a generated project must never
    inherit another tenant's identifier."""
    rc, destination = _run_init(tmp_path)
    assert rc == 0
    import json

    config = json.loads((destination / "opencode.json").read_text())
    assert config["mcp"]["falda"]["headers"]["X-Falda-Tenant"] == "{env:FALDA_TENANT}"


def test_init_pins_loop_supervisor_dependency_to_default_git_url(tmp_path):
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
