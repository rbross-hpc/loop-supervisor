import tomllib

from loop_supervisor.cli import _DEFAULT_LOOP_SUPERVISOR_GIT_URL, build_parser, cmd_init_copy


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
